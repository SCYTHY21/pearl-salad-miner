#!/usr/bin/env bash
# Small helper around the SaladCloud public API for this mining test.
#
#   export SALAD_API_KEY=...      (Portal -> API Keys)
#   export SALAD_ORG=my-org       (organization name, lowercase)
#   export SALAD_PROJECT=my-proj  (project name)
#
#   ./salad-api.sh gpu-classes                 # names, UUIDs, price per priority
#   ./salad-api.sh availability <gpu-uuid> [country,country]
#   ./salad-api.sh quotas
#   ./salad-api.sh create container-group.json # POST the JSON (autostart false)
#   ./salad-api.sh start  <group-name>
#   ./salad-api.sh status <group-name>
#   ./salad-api.sh instances <group-name>
#   ./salad-api.sh stop   <group-name>
#
# Endpoints follow https://docs.salad.com/reference/saladcloud-api . Every write
# is followed by a read so you can see what the platform actually did.
set -euo pipefail

: "${SALAD_API_KEY:?set SALAD_API_KEY}"
: "${SALAD_ORG:?set SALAD_ORG}"
BASE="https://api.salad.com/api/public/organizations/${SALAD_ORG}"

pretty() {
  if command -v python >/dev/null 2>&1; then python -m json.tool 2>/dev/null || cat
  elif command -v python3 >/dev/null 2>&1; then python3 -m json.tool 2>/dev/null || cat
  else cat; fi
}
api() { # method path [json-body]
  local m=$1 p=$2 body=${3:-}
  if [[ -n "$body" ]]; then
    curl -sS -X "$m" "$BASE$p" -H "Salad-Api-Key: $SALAD_API_KEY" -H "Content-Type: application/json" --data "$body"
  else
    curl -sS -X "$m" "$BASE$p" -H "Salad-Api-Key: $SALAD_API_KEY"
  fi
  echo
}
need_project() { : "${SALAD_PROJECT:?set SALAD_PROJECT}"; }

cmd=${1:-help}
case "$cmd" in
  gpu-classes)
    api GET "/gpu-classes" | python - <<'PY' 2>/dev/null || api GET "/gpu-classes" | pretty
import sys, json
d = json.load(sys.stdin)
rows = d.get("items", d if isinstance(d, list) else [])
print(f"{'name':32} {'id':38} {'batch':>7} {'low':>7} {'medium':>7} {'high':>7}  demand")
for g in rows:
    prices = {p.get("priority"): p.get("price") for p in g.get("prices", [])}
    print(f"{g.get('name',''):32} {g.get('id',''):38} "
          f"{str(prices.get('batch','-')):>7} {str(prices.get('low','-')):>7} "
          f"{str(prices.get('medium','-')):>7} {str(prices.get('high','-')):>7}  "
          f"{'HIGH' if g.get('is_high_demand') else ''}")
PY
    ;;
  availability)
    gid=${2:?gpu class uuid}
    cc=${3:-}
    if [[ -n "$cc" ]]; then
      ccjson=$(printf '%s' "$cc" | tr ',' '\n' | sed 's/.*/"&"/' | paste -sd, -)
      body="{\"gpu_classes\":[\"$gid\"],\"country_codes\":[$ccjson],\"cpu\":2,\"memory\":4096}"
    else
      body="{\"gpu_classes\":[\"$gid\"],\"cpu\":2,\"memory\":4096}"
    fi
    api POST "/availability/sce-gpu-availability" "$body" | pretty
    ;;
  quotas)
    api GET "/quotas" | pretty
    ;;
  create)
    need_project
    f=${2:?path to container group json}
    api POST "/projects/${SALAD_PROJECT}/containers" "$(cat "$f")" | pretty
    name=$(python -c "import sys,json;print(json.load(open(sys.argv[1]))['name'])" "$f" 2>/dev/null || true)
    [[ -n "$name" ]] && api GET "/projects/${SALAD_PROJECT}/containers/${name}" | pretty
    ;;
  start|stop)
    need_project
    name=${2:?container group name}
    api POST "/projects/${SALAD_PROJECT}/containers/${name}/${cmd}"
    api GET "/projects/${SALAD_PROJECT}/containers/${name}" | pretty
    ;;
  status)
    need_project
    name=${2:?container group name}
    api GET "/projects/${SALAD_PROJECT}/containers/${name}" | pretty
    ;;
  instances)
    need_project
    name=${2:?container group name}
    api GET "/projects/${SALAD_PROJECT}/containers/${name}/instances" | pretty
    ;;
  *)
    sed -n '2,20p' "$0"
    ;;
esac
