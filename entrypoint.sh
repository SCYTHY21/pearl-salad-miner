#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Pearl (PRL) miner entrypoint for SaladCloud Container Engine.
#
# What it does, in order:
#   1. validates WALLET, derives a worker name from SALAD_MACHINE_ID
#   2. prints GPU / driver / OpenCL info and the Salad identifiers
#   3. picks the pool (explicit POOL, or the default for the miner)
#   4. runs the miners listed in MINER one after another: wildrig | srb | krig.
#      A miner that dies before its first accepted share is abandoned and the
#      next one is tried; the last failure exits the container so SaladCloud
#      reallocates the instance.
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
WALLET="${WALLET:-}"                       # required: prl1p... (or solo:prl1p... for SOLO on Kryptex)
MINER="${MINER:-wildrig srb}"              # ordered list: wildrig | srb | krig (space or comma separated)
POOL="${POOL:-}"                           # explicit pool URL for every miner; empty = miner default (see below)
PEARLHASH_POOL="${PEARLHASH_POOL:-pool.pearlhash.xyz:9000}"   # default for wildrig / srb
POOL_AUTO="${POOL_AUTO:-1}"                # krig only: probe Kryptex regions and pick the fastest
POOL_REGIONS="${POOL_REGIONS:-prl prl-eu prl-us prl-br prl-sg prl-hk prl-ru prl-ae}"
POOL_DOMAIN="${POOL_DOMAIN:-kryptex.network}"
POOL_PASSWORD="${POOL_PASSWORD:-}"
WORKER="${WORKER:-}"                       # letters/digits only; default derived from SALAD_MACHINE_ID
LOG_LEVEL="${LOG_LEVEL:-debug}"            # krig: debug prints per-share lines
API_PORT="${API_PORT:-12000}"              # miner HTTP API, bound to 127.0.0.1 only
API_DUMP="${API_DUMP:-1}"                  # 1 = include a raw dump of the miner API in STATUS (krig)
EXTRA_ARGS="${EXTRA_ARGS:-}"               # appended verbatim to the miner command
WATCHDOG="${WATCHDOG:-1}"                  # 0 disables both watchdogs
STARTUP_GRACE="${STARTUP_GRACE:-600}"      # seconds allowed until the first accepted share
NO_SHARE_TIMEOUT="${NO_SHARE_TIMEOUT:-900}" # seconds without an accepted share afterwards
SHARE_REGEX="${SHARE_REGEX:-accept}"       # ERE, case-insensitive, matched against each miner line
REJECT_REGEX="${REJECT_REGEX:-reject|stale|invalid}"
SHARE_COUNTER_REGEX="${SHARE_COUNTER_REGEX:-shares=([0-9]+)}"   # krig [stats] line: cumulative share counter
HASHRATE_REGEX="${HASHRATE_REGEX:-([0-9.]+) TH/s}"               # last number before " TH/s" (krig and wildrig)
MIN_GPU_UTIL="${MIN_GPU_UTIL:-20}"         # percent; GPU below this for NO_SHARE_TIMEOUT -> exit (0 = off)
STATUS_EVERY="${STATUS_EVERY:-600}"        # seconds between STATUS lines (0 = off)
MAX_RUNTIME="${MAX_RUNTIME:-0}"            # seconds; 0 = unlimited. Exits 0 when reached.
TICK="${TICK:-30}"                         # seconds between periodic checks

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

MINER_LIST="${MINER//,/ }"

# ---------- 2. environment report ----------
log "pearl-salad-miner starting (miners='$MINER_LIST' worker=$WORKER)"
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
if command -v clinfo >/dev/null 2>&1; then
  ocl=$(clinfo -l 2>&1 | head -n 6 | tr '\n' ' ')
  log "opencl: ${ocl:-no platforms found}"
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

kryptex_host() {  # prints the chosen Kryptex host (probes regions when POOL_AUTO=1)
  local host="prl.$POOL_DOMAIN" best_host="" best_ms=999999 r h ms
  if [[ "$POOL_AUTO" == "1" ]]; then
    for r in $POOL_REGIONS; do
      h="$r.$POOL_DOMAIN"
      ms=$(probe_ms "$h" 7048)
      if [[ -n "$ms" ]]; then
        log "probe $h ${ms} ms" >&2
        if (( ms < best_ms )); then best_ms=$ms; best_host=$h; fi
      else
        log "probe $h unreachable" >&2
      fi
    done
    if [[ -n "$best_host" ]]; then
      host=$best_host; log "selected pool host $host (${best_ms} ms)" >&2
    else
      log "all region probes failed: network state UNKNOWN (not 'no pool'); falling back to $host" >&2
    fi
  fi
  echo "$host"
}

pool_for() {  # miner -> pool URL
  local m=$1
  if [[ -n "$POOL" ]]; then echo "$POOL"; return; fi
  case "$m" in
    krig)    echo "stratum+ssl://$(kryptex_host):8048" ;;
    *)       echo "$PEARLHASH_POOL" ;;
  esac
}

build_cmd() {  # miner pool -> sets global array cmd
  local m=$1 p=$2
  case "$m" in
    wildrig)
      cmd=(/opt/miners/wildrig/wildrig-multi --algo pearlhash --url "$p" --user "$WALLET" --worker "$WORKER"
           --opencl-platforms nvidia --no-color --print-time 30 --api-port "$API_PORT")
      [[ -n "$POOL_PASSWORD" ]] && cmd+=(--pass "$POOL_PASSWORD")
      ;;
    srb)
      if [[ "$p" == *kryptex* ]]; then
        cmd=(/opt/miners/srb/SRBMiner-MULTI --disable-cpu --algorithm pearlhash --pool "$p" --wallet "$WALLET.$WORKER")
      else
        cmd=(/opt/miners/srb/SRBMiner-MULTI --disable-cpu --algorithm pearlhash --pool "$p" --wallet "$WALLET" --worker "$WORKER")
      fi
      [[ -n "$POOL_PASSWORD" ]] && cmd+=(--password "$POOL_PASSWORD")
      ;;
    krig)
      cmd=(/opt/miners/krig/krig-miner --coin pearl --url "$p" --user "$WALLET/$WORKER"
           --no-rocm --no-tui --log-level "$LOG_LEVEL" --api-port "$API_PORT" --api-host 127.0.0.1)
      [[ -n "$POOL_PASSWORD" ]] && cmd+=(--password "$POOL_PASSWORD")
      ;;
    *) die "unknown miner '$m' (use wildrig, srb or krig)" ;;
  esac
  # shellcheck disable=SC2206
  [[ -n "$EXTRA_ARGS" ]] && cmd+=($EXTRA_ARGS)
}

# ---------- shared state for the run loop ----------
START=$(date +%s)
accepted=0; rejected=0; last_accept=0; first_share=0
share_counter=0; last_hashrate="n/a"; low_util_since=0
exit_code=1; reason="miner exited on its own"; CURRENT_MINER=""; MINER_PID=""

summary() {
  local now age first util
  now=$(date +%s)
  if (( last_accept > 0 )); then age=$((now - last_accept)); else age="never"; fi
  if (( first_share > 0 )); then first=$((first_share - START)); else first="n/a"; fi
  log "STATUS miner=$CURRENT_MINER uptime=$((now - START))s accepted=$accepted rejected=$rejected hashrate_ths=$last_hashrate last_accept_age=${age}s first_share_after=${first}s machine=${SALAD_MACHINE_ID:-n/a} worker=$WORKER"
  if (( HAVE_SMI == 1 )); then
    util=$(nvidia-smi --query-gpu=utilization.gpu,power.draw,temperature.gpu,clocks.sm --format=csv,noheader 2>/dev/null | head -n1)
    [[ -n "$util" ]] && log "STATUS gpu util,power,temp,sm_clock: $util"
  fi
  if [[ "$API_DUMP" == "1" && "$CURRENT_MINER" == "krig" ]]; then
    local body
    body=$(curl -s -m 3 "http://127.0.0.1:$API_PORT/" 2>/dev/null | tr -d '\n' | head -c 1500)
    [[ -n "$body" ]] && log "STATUS api: $body"
  fi
}

stop_miner() {  # reason
  if [[ -n "$MINER_PID" ]] && kill -0 "$MINER_PID" 2>/dev/null; then
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

# ---------- run one miner: start it, read its output, apply watchdogs ----------
# Sets: exit_code, reason. Returns 0 when the run ended for a reason other than
# "the miner died before its first share" (i.e. do not try the next miner).
run_miner() {
  local m=$1 pool fifo line rc now last_tick last_status got_share c u mrc
  CURRENT_MINER=$m
  pool=$(pool_for "$m")
  build_cmd "$m" "$pool"
  log "miner=$m pool=$pool"
  log "cmd: ${cmd[*]}"

  fifo=$(mktemp -u /tmp/miner.XXXXXX)
  mkfifo "$fifo" || die "cannot create FIFO"
  cd "$(dirname "${cmd[0]}")" || die "miner directory missing"
  "${cmd[@]}" >"$fifo" 2>&1 &
  MINER_PID=$!
  exec 3<"$fifo"
  rm -f "$fifo"

  local run_start; run_start=$(date +%s)
  last_tick=$run_start; last_status=$run_start
  share_counter=0; low_util_since=0
  exit_code=1; reason="miner exited on its own"

  while :; do
    if IFS= read -r -t "$TICK" -u 3 line; then
      printf '%s %s\n' "$(ts)" "$line"
      got_share=0
      if [[ $line =~ $SHARE_COUNTER_REGEX ]]; then
        c=${BASH_REMATCH[1]}
        if (( c > share_counter )); then
          accepted=$((accepted + c - share_counter)); got_share=1
        fi
        share_counter=$c
      elif [[ $line =~ $SHARE_REGEX ]]; then
        accepted=$((accepted + 1)); got_share=1
      elif [[ $line =~ $REJECT_REGEX ]]; then
        rejected=$((rejected + 1))
      fi
      if [[ $line =~ $HASHRATE_REGEX ]]; then last_hashrate=${BASH_REMATCH[1]}; fi
      if (( got_share == 1 )); then
        last_accept=$(date +%s)
        if (( first_share == 0 )); then
          first_share=$last_accept
          log "FIRST ACCEPTED SHARE after $((first_share - START)) s (miner=$m)"
        fi
      fi
    else
      rc=$?
      if (( rc <= 128 )); then
        break   # EOF: the miner closed its output (it exited)
      fi
    fi

    now=$(date +%s)
    if (( now - last_tick < TICK )); then continue; fi
    last_tick=$now

    if (( STATUS_EVERY > 0 && now - last_status >= STATUS_EVERY )); then
      summary; last_status=$now
    fi

    if [[ "$WATCHDOG" == "1" ]]; then
      if (( first_share == 0 && now - run_start > STARTUP_GRACE )); then
        log "WATCHDOG: no accepted share within ${STARTUP_GRACE}s of starting $m (SHARE_REGEX='$SHARE_REGEX'). Exiting so SaladCloud reallocates."
        stop_miner "startup grace exceeded"; exit_code=3; reason="watchdog: no first share"; break
      fi
      if (( first_share > 0 && now - last_accept > NO_SHARE_TIMEOUT )); then
        log "WATCHDOG: no accepted share for ${NO_SHARE_TIMEOUT}s. Exiting so SaladCloud reallocates."
        stop_miner "no accepted shares"; exit_code=3; reason="watchdog: shares stopped"; break
      fi
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

  exec 3<&-
  if [[ "$reason" == "miner exited on its own" ]]; then
    wait "$MINER_PID"; mrc=$?
    log "miner $m exited with code $mrc after $(( $(date +%s) - run_start )) s (accepted so far: $accepted)"
    if (( first_share == 0 )); then
      return 1   # never produced: caller tries the next miner
    fi
  fi
  return 0
}

# ---------- 4. try the miners in order ----------
tried=0
for m in $MINER_LIST; do
  tried=$((tried + 1))
  if run_miner "$m"; then
    break
  fi
  log "miner $m did not produce a share; trying the next one"
  reason="all miners failed before first share"; exit_code=1
done
(( tried == 0 )) && die "MINER list is empty"

summary
log "exiting: $reason (exit code $exit_code)"
exit "$exit_code"
