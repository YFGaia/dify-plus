# AGENTS.md

Dify is an open-source platform for building LLM applications, agentic workflows, and RAG pipelines. This monorepo contains the backend API (`api/`), frontend application (`web/`), deployment assets (`docker/`), standalone agent backend (`dify-agent/`), CLI (`cli/`), and end-to-end suite (`e2e/`). Follow the nearest scoped `AGENTS.md` for the files being changed. Apply its guidance within the user's requested scope; explicit user instructions take precedence over workflow defaults.

## Repository Gotchas

- Run backend commands through `uv run --project api <command>`.
- Backend integration tests are CI-only and are not expected to run locally.
- Keep `docker/.env.example` limited to variables required for a default Docker Compose deployment to start. Put optional and provider-specific settings in the matching `docker/envs/*.env.example` file; `docker/.env` overrides those service-specific env files.

## Dify-Plus fork notes

- This repository carries Dify-Plus extensions alongside upstream Dify. Start with [the fork documentation index](docs/dify-plus/README.md) for the feature map and historical/current documentation boundaries.
- Backend extensions are concentrated in `api/controllers/console/*_extend.py`, `api/services/*_extend.py`, `api/models/*_extend.py`, `api/migrations_extend/`, and extension event/schedule modules. Console additions include `/system-manage-extend/*`; frontend extension surfaces include the app center and account/API-key quota views.
- Account personal `total_quota` / `used_quota` is a Dify-Plus balance. App API keys have daily and monthly limits; `accumulated_quota` records cumulative usage and is not a lifetime cap.
- For fork releases, `docker/docker-compose.dify-plus.yaml` provides the explicit migration service. It runs `flask db upgrade` and then `flask extend_db upgrade`; follow its single-executor guidance and the current [upgrade runbook](openspec/changes/merge-upstream-1-17-1/runbook.md). Do not treat a completed source merge as deployment or production acceptance.
- The active upstream merge status and source evidence are maintained in `openspec/changes/merge-upstream-1-17-1/execution-graph.json` and its linked evidence, rather than in older release snapshots.
