#!/usr/bin/env bash
# Native image smoke only; use disposable infrastructure, never deployment data.
set -euo pipefail
component=${1:?component required}
image=${2:?image required}
architecture=${3:?architecture required}
revision=${4:?source revision required}
test_prefix="dify-plus-smoke-${component}-${architecture}-${RANDOM}"
network="${test_prefix}-net"
containers=()
cleanup() {
  local status=$?
  if [ "$status" -ne 0 ]; then
    for container in "${containers[@]}"; do docker logs --tail 100 "$container" 2>&1 || true; done
  fi
  for container in "${containers[@]}"; do docker rm -f "$container" >/dev/null 2>&1 || true; done
  docker network rm "$network" >/dev/null 2>&1 || true
  return "$status"
}
trap cleanup EXIT
test "$(docker image inspect "$image" --format '{{.Architecture}}')" = "$architecture"
test "$(docker image inspect "$image" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = "$revision"
docker image inspect "$image" --format '{{json .Config.Env}}' | python3 -c \
  'import json, sys; assert "COMMIT_SHA=" + sys.argv[1] in json.load(sys.stdin)' "$revision"
docker network create "$network" >/dev/null
# Synthetic CI-only values. No deployed credentials or host volumes are used.
secret_key=$(python3 -c 'import base64; print(base64.urlsafe_b64encode(bytes(range(32))).decode())')
common_env=(-e "DIFY_AGENT_SERVER_SECRET_KEY=$secret_key" -e SECRET_KEY=ci-image-smoke-only -e MIGRATION_ENABLED=false)
start_redis() {
  containers+=("${test_prefix}-redis")
  docker run -d --name "${test_prefix}-redis" --network "$network" --network-alias redis redis:6-alpine >/dev/null
  for _ in $(seq 1 30); do
    if docker exec "${test_prefix}-redis" redis-cli ping | grep -qx PONG; then return; fi
    sleep 1
  done
  return 1
}
wait_http() {
  local container=$1 port=$2 path=$3
  local host_port
  host_port=$(docker port "$container" "$port/tcp" | sed 's/.*://')
  python3 - "$host_port" "$path" <<'PY'
import json, sys, time, urllib.request
url = f"http://127.0.0.1:{sys.argv[1]}{sys.argv[2]}"
for attempt in range(120):
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            assert response.status == 200
            body = response.read()
            if sys.argv[2] == '/openapi.json':
                schema = json.loads(body)
                assert schema['info']['title'] == 'Dify Agent Run Server'
                assert '/runs' in schema['paths']
            print('HTTP image smoke passed:', sys.argv[2])
            break
    except Exception:
        if attempt == 119: raise
        time.sleep(1)
PY
}
case "$component" in
  api)
    docker run --rm "${common_env[@]}" --entrypoint python "$image" -c '
import importlib, importlib.metadata as m, pathlib, os
for module in ("casdoor", "jwt", "cryptography", "gevent", "psycopg2", "dify_agent"):
    importlib.import_module(module)
providers = [d for d in m.distributions() if d.metadata["Name"].startswith(("dify-vdb-", "dify-trace-"))]
assert len(providers) == 38, len(providers)
entries = list(m.entry_points(group="dify.vector_backends"))
assert len(entries) == 32, len(entries)
for entry in entries: entry.load()
assert pathlib.Path("migrations/versions").is_dir()
assert pathlib.Path("migrations_extend/versions").is_dir()
assert os.environ["COMMIT_SHA"]
print("API dependencies, 38 providers, 32 vector entrypoints and both migration chains passed")'
    docker run --rm --entrypoint node "$image" --version
    start_redis
    containers+=("${test_prefix}-postgres")
    docker run -d --name "${test_prefix}-postgres" --network "$network" --network-alias postgres \
      -e POSTGRES_PASSWORD=ci-smoke-only -e POSTGRES_DB=dify postgres:15-alpine >/dev/null
    postgres_ready=false
    for _ in $(seq 1 60); do
      if docker exec "${test_prefix}-postgres" pg_isready -h 127.0.0.1 -U postgres -d dify >/dev/null; then postgres_ready=true; break; fi
      sleep 1
    done
    test "$postgres_ready" = true
    api_env=(-e DB_TYPE=postgresql -e DB_HOST=postgres -e DB_PORT=5432 -e DB_USERNAME=postgres \
      -e DB_PASSWORD=ci-smoke-only -e DB_DATABASE=dify -e REDIS_HOST=redis -e REDIS_PORT=6379 \
      -e REDIS_PASSWORD= -e CELERY_BROKER_URL=redis://redis:6379/1 -e STORAGE_TYPE=opendal -e OPENDAL_SCHEME=fs \
      -e OPENDAL_FS_ROOT=/app/api/storage -e VECTOR_STORE=weaviate)
    docker run --rm --network "$network" "${common_env[@]}" "${api_env[@]}" --entrypoint /bin/bash "$image" \
      -ec 'flask db upgrade && flask extend_db upgrade && flask db current && flask extend_db current'
    containers+=("${test_prefix}-app")
    docker run -d --name "${test_prefix}-app" --network "$network" -p 127.0.0.1::5001 \
      "${common_env[@]}" "${api_env[@]}" -e MODE=api "$image" >/dev/null
    wait_http "${test_prefix}-app" 5001 /health
    wait_http "${test_prefix}-app" 5001 /console/api/system-features
    ;;
  web)
    containers+=("${test_prefix}-app")
    docker run -d --name "${test_prefix}-app" --network "$network" -p 127.0.0.1::3000 "$image" >/dev/null
    wait_http "${test_prefix}-app" 3000 /signin
    ;;
  agent-backend)
    start_redis
    containers+=("${test_prefix}-app")
    docker run -d --name "${test_prefix}-app" --network "$network" -p 127.0.0.1::5050 \
      "${common_env[@]}" -e DIFY_AGENT_REDIS_URL=redis://redis:6379/0 \
      -e DIFY_AGENT_LOCAL_SANDBOX_ENDPOINT=http://unused-sandbox:5004 \
      -e DIFY_AGENT_API_TOKEN=ci-smoke-only "$image" >/dev/null
    wait_http "${test_prefix}-app" 5050 /openapi.json
    agent_port=$(docker port "${test_prefix}-app" 5050/tcp | sed 's/.*://')
    python3 - "$agent_port" <<'PY'
import sys, urllib.error, urllib.request
url = f"http://127.0.0.1:{sys.argv[1]}/runs/ci-smoke-missing-run"
for headers, expected in (({}, 401), ({'Authorization': 'Bearer ci-smoke-only'}, 404)):
    try:
        urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10)
    except urllib.error.HTTPError as error:
        assert error.code == expected, (error.code, expected)
    else:
        raise AssertionError('Agent run boundary unexpectedly accepted request')
print('Agent authentication and live Redis missing-run read passed')
PY
    ;;
  agent-local-sandbox)
    docker run --rm --entrypoint sh "$image" -ec 'node --version; pnpm --version; uv --version; python --version; test -x /usr/local/bin/dify-agent'
    containers+=("${test_prefix}-app")
    docker run -d --name "${test_prefix}-app" --network "$network" -p 127.0.0.1::5004 "$image" >/dev/null
    wait_http "${test_prefix}-app" 5004 /healthz
    ;;
  *) echo "Unknown component: $component" >&2; exit 1 ;;
esac
echo "Native image smoke passed: $component $architecture $revision"
