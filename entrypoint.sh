#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Pearl (PRL) miner entrypoint for SaladCloud Container Engine.
#
# What it does, in order:
#   1. validates WALLET, derives a worker name from SALAD_MACHINE_ID
#   2. prints GPU / driver info (nvidia-smi) and the Salad identifiers
#   3. probes the Kryptex regional endpoints and picks the lowest latency
#   4. starts krig-miner (default) or SRBMiner-MULTI (MINER=srb)
#   5. timestamps every miner line, counts accepted / rejected shares
#   6. prints a STATUS line every STATUS_EVERY seconds (for reconciliation
#      against the SaladCloud bill and the pool's per-worker stats)
#   7. exits with code 3 when the node stops producing (no accepted shares,
#      or GPU utilisation stays low), so SaladCloud restarts / reallocates the
#      instance instead of billing a GPU that is doing nothing.
#
# A "running" container is not proof of production. The STATUS lines and the
# FIRST ACCEPTED SHARE line are what you reconcile against the bill.
# ---------------------------------------------------------------------------
set -uo pipefail
shopt -s nocasematch

# ---------- configuration (environment variables) ----------
WALLET="${WALLET:-}"                       # required: prl1p... (or solo:prl1p... for SOLO)
MINER="${MINER:-krig}"                     # krig | srb
POOL="${POOL:-}"                           # explicit pool URL; empty = choose automatically
POOL_AUTO="${POOL_AUTO:-1}"                # 1 = probe regions below and pick the fastest
POOL_REGIONS="${POOL_REGIONS:-prl prl-eu prl-us prl-br prl-sg prl-hk prl-ru prl-ae}"
POOL_DOMAIN="${POOL_DOMAIN:-kryptex.network}"
POOL_PASSWORD="${POOL_PASSWORD:-}"
WORKER="${WORKER:-}"                       # letters/digits only; default derived from SALAD_MACHINE_ID
LOG_LEVEL="${LOG_LEVEL:-debug}"            # krig: debug prints per-share lines (needed by the watchdog)
API_PORT="${API_PORT:-12000}"              # krig HTTP API, bound to 127.0.0.1 only
API_DUMP="${API_DUMP:-1}"                  # 1 = include a raw dump of the miner API in STATUS
EXTRA_ARGS="${EXTRA_ARGS:-}"               # appended verbatim to the miner command
WATCHDOG="${WATCHDOG:-1}"                  # 0 disables both watchdogs
STARTUP_GRACE="${STARTUP_GRACE:-600}"      # seconds allowed until the first accepted share
NO_SHARE_TIMEOUT="${NO_SHARE_TIMEOUT:-900}" # seconds without an accepted share afterwards
SHARE_REGEX="${SHARE_REGEX:-accept}"       # ERE, case-insensitive, matched against each miner line
REJECT_REGEX="${REJECT_REGEX:-reject|stale|invalid}"
SHARE_COUNTER_REGEX="${SHARE_COUNTER_REGEX:-shares=([0-9]+)}"   # krig [stats] line: cumulative share counter
HASHRATE_REGEX="${HASHRATE_REGEX:-hashes/s=([0-9.]+) TH/s}"      # krig [stats] line: current hashrate
MIN_GPU_UTIL="${MIN_GPU_UTIL:-20}"         # percent; GPU below this for NO_SHARE_TIMEOUT -> exit (0 = off)
STATUS_EVERY="${STATUS_EVERY:-600}"        # seconds between STATUS lines (0 = off)
MAX_RUNTIME="${MAX_RUNTIME:-0}"            # seconds; 0 = unlimited. Exits 0 when reached.

TICK="${TICK:-30}"                                    # seconds between periodic checks

ts()  { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { printf '%s [entry] %s\n' "$(ts)" "$*"; }
die() { log "FATAL: $*"; exit 2; }
sanitize() { printf '%s' "$1" | tr -cd 'A-Za-z0-9' | cut -c1-24; }

# ---------- 1. identity ----------
[[ -n "$WALLET" ]] || die "WALLET is not set. Expected a Pearl address starting with prl1 (see README)."
if [[ ! "$WALLET" =~ ^(solo:)?prl1[a-z0-9]{20,}$ ]]; then
  log "WARNING: WALLET does not look like a Pearl mainnet address (prl1p...). Continuing, but double-check it."
fi

if [[ -z "$WORKER" ]]; then
  if [[ -n "${SALAD_MACHINE_ID:-}" ]]; then
    WORKER="s$(sanitize "$SALAD_MACHINE_ID" | cut -c1-10)"
  else
    WORKER="$(sanitize "$(hostname)")"
  fi
  [[ -n "$WORKER" ]] || WORKER="worker"
else
  WORKER="$(sanitize "$WORKER")"
fi

# ---------- 2. environment report ----------
log "pearl-salad-miner starting (miner=$MINER worker=$WORKER)"
log "salad: machine=${SALAD_MACHINE_ID:-n/a} instance=${SALAD_INSTANCE_ID:-n/a} group=${SALAD_CONTAINER_GROUP_NAME:-n/a} project=${SALAD_PROJECT_NAME:-n/a}"

HAVE_SMI=0
if command -v nvidia-smi >/dev/null 2>&1; then
  if gpu_info=$(nvidia-smi --query-gpu=name,driver_version,memory.total,power.limit --format=csv,noheader 2>&1); then
    HAVE_SMI=1
    while IFS= read -r l; do log "gpu: $l"; done <<<"$gpu_info"
  else
    log "nvidia-smi present but failed: $gpu_info"
  fi
else
  log "nvidia-smi not found in container (CUDA may still work through libcuda.so.1)"
fi

gpu_util() {  # prints integer utilisation % of GPU 0, or nothing
  local u
  u=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits -i 0 2>/dev/null | head -n1 | tr -cd '0-9')
  [[ -n "$u" ]] && echo "$u"
}

# ---------- 3. pool selection ----------
probe_ms() {  # host port -> prints connect time in ms, or nothing on failure
  local h=$1 p=$2 s e
  s=$(date +%s%N)
  if timeout 4 bash -c "exec 3<>/dev/tcp/$h/$p" 2>/dev/null; then
    e=$(date +%s%N)
    echo $(( (e - s) / 1000000 ))
  fi
}

if [[ -z "$POOL" ]]; then
  host="prl.$POOL_DOMAIN"
  if [[ "$POOL_AUTO" == "1" ]]; then
    best_host=""; best_ms=999999
    for r in $POOL_REGIONS; do
      h="$r.$POOL_DOMAIN"
      ms=$(probe_ms "$h" 7048)
      if [[ -n "$ms" ]]; then
        log "probe $h ${ms} ms"
        if (( ms < best_ms )); then best_ms=$ms; best_host=$h; fi
      else
        log "probe $h unreachable"
      fi
    done
    if [[ -n "$best_host" ]]; then
      host=$best_host
      log "selected pool host $host (${best_ms} ms)"
    else
      log "all region probes failed: network state UNKNOWN (not 'no pool'); falling back to $host"
    fi
  fi
  case "$MINER" in
    krig) POOL="stratum+ssl://$host:8048" ;;
    srb)  POOL="$host:7048" ;;
  esac
fi

# ---------- 4. miner command ----------
case "$MINER" in
  krig)
    cmd=(/opt/miners/krig/krig-miner --coin pearl --url "$POOL" --user "$WALLET/$WORKER"
         --no-rocm --no-tui --log-level "$LOG_LEVEL" --api-port "$API_PORT" --api-host 127.0.0.1)
    [[ -n "$POOL_PASSWORD" ]] && cmd+=(--password "$POOL_PASSWORD")
    ;;
  srb)
    # Kryptex documents the WALLET.WORKER form for SRBMiner-MULTI.
    cmd=(/opt/miners/srb/SRBMiner-MULTI --disable-cpu --algorithm pearlhash --pool "$POOL" --wallet "$WALLET.$WORKER")
    [[ -n "$POOL_PASSWORD" ]] && cmd+=(--password "$POOL_PASSWORD")
    ;;
  *) die "MINER must be 'krig' or 'srb' (got '$MINER')" ;;
esac
# shellcheck disable=SC2206
[[ -n "$EXTRA_ARGS" ]] && cmd+=($EXTRA_ARGS)

log "pool=$POOL"
log "cmd: ${cmd[*]}"

# ---------- 5. start miner, read its output through a FIFO ----------
FIFO=$(mktemp -u /tmp/miner.XXXXXX)
mkfifo "$FIFO" || die "cannot create FIFO"
cd "$(dirname "${cmd[0]}")" || die "miner directory missing"
"${cmd[@]}" >"$FIFO" 2>&1 &
MINER_PID=$!
exec 3<"$FIFO"
rm -f "$FIFO"

START=$(date +%s)
accepted=0; rejected=0; last_accept=0; first_share=0; last_status=$START
low_util_since=0; last_tick=$START
share_counter=0; last_hashrate="n/a"
exit_code=1; reason="miner exited on its own"

summary() {
  local now age first util
  now=$(date +%s)
  if (( last_accept > 0 )); then age=$((now - last_accept)); else age="never"; fi
  if (( first_share > 0 )); then first=$((first_share - START)); else first="n/a"; fi
  log "STATUS uptime=$((now - START))s accepted=$accepted rejected=$rejected hashrate_ths=$last_hashrate last_accept_age=${age}s first_share_after=${first}s machine=${SALAD_MACHINE_ID:-n/a} worker=$WORKER"
  if (( HAVE_SMI == 1 )); then
    util=$(nvidia-smi --query-gpu=utilization.gpu,power.draw,temperature.gpu,clocks.sm --format=csv,noheader 2>/dev/null | head -n1)
    [[ -n "$util" ]] && log "STATUS gpu util,power,temp,sm_clock: $util"
  fi
  if [[ "$API_DUMP" == "1" && "$MINER" == "krig" ]]; then
    local body
    body=$(curl -s -m 3 "http://127.0.0.1:$API_PORT/" 2>/dev/null | tr -d '\n' | head -c 1500)
    [[ -n "$body" ]] && log "STATUS api: $body"
  fi
}

stop_miner() {  # reason
  if kill -0 "$MINER_PID" 2>/dev/null; then
    log "stopping miner: $1"
    kill -TERM "$MINER_PID" 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$MINER_PID" 2>/dev/null || break
      sleep 1
    done
    kill -KILL "$MINER_PID" 2>/dev/null || true
  fi
}

on_signal() {
  stop_miner "signal received (SaladCloud stop or reallocation)"
  summary
  exit 143
}
trap on_signal TERM INT

# ---------- 6. main loop: log, count, watchdogs ----------
while :; do
  if IFS= read -r -t "$TICK" -u 3 line; then
    printf '%s %s\n' "$(ts)" "$line"
    got_share=0
    if [[ $line =~ $SHARE_COUNTER_REGEX ]]; then
      # cumulative counter from the miner's own stats line (krig: "shares=N")
      c=${BASH_REMATCH[1]}
      if (( c > share_counter )); then
        accepted=$((accepted + c - share_counter)); got_share=1
      fi
      share_counter=$c
      [[ $line =~ $HASHRATE_REGEX ]] && last_hashrate=${BASH_REMATCH[1]}
    elif [[ $line =~ $SHARE_REGEX ]]; then
      accepted=$((accepted + 1)); got_share=1
    elif [[ $line =~ $REJECT_REGEX ]]; then
      rejected=$((rejected + 1))
    fi
    if (( got_share == 1 )); then
      last_accept=$(date +%s)
      if (( first_share == 0 )); then
        first_share=$last_accept
        log "FIRST ACCEPTED SHARE after $((first_share - START)) s"
      fi
    fi
  else
    rc=$?
    if (( rc <= 128 )); then
      break   # EOF: the miner closed its output (it exited)
    fi
    # rc > 128: read timed out, fall through to the periodic checks
  fi

  now=$(date +%s)
  if (( now - last_tick < TICK )); then
    continue   # a burst of log lines; run the periodic checks at most once per TICK
  fi
  last_tick=$now

  if (( STATUS_EVERY > 0 && now - last_status >= STATUS_EVERY )); then
    summary; last_status=$now
  fi

  if [[ "$WATCHDOG" == "1" ]]; then
    # (a) share-based watchdog: depends on SHARE_REGEX matching the miner's log lines
    if (( first_share == 0 && now - START > STARTUP_GRACE )); then
      log "WATCHDOG: no line matched SHARE_REGEX='$SHARE_REGEX' within ${STARTUP_GRACE}s. Either this node produces nothing or the regex does not match this miner's output (check the log above). Exiting so SaladCloud reallocates."
      stop_miner "startup grace exceeded"; exit_code=3; reason="watchdog: no first share"; break
    fi
    if (( first_share > 0 && now - last_accept > NO_SHARE_TIMEOUT )); then
      log "WATCHDOG: no accepted share for ${NO_SHARE_TIMEOUT}s. Exiting so SaladCloud reallocates."
      stop_miner "no accepted shares"; exit_code=3; reason="watchdog: shares stopped"; break
    fi
    # (b) utilisation watchdog: independent of the log format
    if (( HAVE_SMI == 1 && MIN_GPU_UTIL > 0 )); then
      u=$(gpu_util)
      if [[ -n "$u" ]]; then
        if (( u < MIN_GPU_UTIL )); then
          (( low_util_since == 0 )) && low_util_since=$now
          if (( now - low_util_since > NO_SHARE_TIMEOUT )); then
            log "WATCHDOG: GPU utilisation below ${MIN_GPU_UTIL}% for ${NO_SHARE_TIMEOUT}s (last sample ${u}%). Exiting so SaladCloud reallocates."
            stop_miner "gpu idle"; exit_code=3; reason="watchdog: gpu idle"; break
          fi
        else
          low_util_since=0
        fi
      fi
    fi
  fi

  if (( MAX_RUNTIME > 0 && now - START >= MAX_RUNTIME )); then
    log "MAX_RUNTIME=${MAX_RUNTIME}s reached"
    stop_miner "max runtime"; exit_code=0; reason="max runtime reached"; break
  fi
done

# ---------- 7. exit ----------
if [[ "$reason" == "miner exited on its own" ]]; then
  wait "$MINER_PID"; mrc=$?
  log "miner process exited with code $mrc"
fi
summary
log "exiting: $reason (exit code $exit_code)"
exit "$exit_code"
