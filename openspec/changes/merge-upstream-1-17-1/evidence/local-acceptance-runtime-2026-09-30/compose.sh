#!/bin/bash
# Dedicated local project wrapper; first argument is a private untracked env file.
set -euo pipefail
acceptance_script_dir="$(cd "$(dirname "$0")" && pwd)"
acceptance_private_env="${1:?Usage: compose.sh /absolute/private.env <compose subcommand> [args]}"
shift
case "$acceptance_private_env" in
  /*) ;;
  *) echo "Private env must be an absolute path." >&2; exit 2 ;;
esac
[[ -f "$acceptance_private_env" ]] || { echo "Private env file missing." >&2; exit 2; }
# Values stay inside Docker Compose; never source or echo the env file.
exec docker compose --project-directory /Users/liuxingwang/go/src/dify-plus/docker \
  --env-file "$acceptance_private_env" --project-name difyplus-acceptance-20260930 \
  -f /Users/liuxingwang/go/src/dify-plus/docker/docker-compose.dify-plus.yaml \
  -f "$acceptance_script_dir/compose.acceptance.override.yaml" \
  --profile collaboration --profile qdrant --profile acceptance-mysql "$@"
