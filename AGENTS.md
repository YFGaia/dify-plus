# AGENTS.md

Dify-Plus is a fork of Dify, an open-source platform for building LLM applications, agentic workflows, and RAG pipelines. This monorepo contains the backend API (`api/`), frontend application (`web/`), deployment assets (`docker/`), standalone agent backend (`dify-agent/`), CLI (`cli/`), and end-to-end suite (`e2e/`). Follow the nearest scoped `AGENTS.md` for the files being changed. Apply its guidance within the user's requested scope; explicit user instructions take precedence over workflow defaults.

## Repository Gotchas

- Run backend commands through `uv run --project api <command>`.
- Backend integration tests are CI-only and are not expected to run locally.
- Keep `docker/.env.example` limited to variables required for a default Docker Compose deployment to start. Put optional and provider-specific settings in the matching `docker/envs/*.env.example` file; `docker/.env` overrides those service-specific env files.

## Dify-Plus feature boundaries

- Start with [README](README.md) and the [current documentation index](docs/dify-plus/README.md). Current capability references are the frontend, backend, database and deployment guides linked there. Historical release notes and OpenSpec proposals are evidence, not the current product contract.
- Dify-Plus uses native Dify Console/API system management. There is no standalone GVA `admin/` service. Current management pages are `/system-manage-extend/system-integration`, `/system-manage-extend/quota-management`, and `/system-manage-extend/code-execution-control`.
- Global system management requires the current workspace to be the database-bound instance initialization workspace and the real membership role to be `owner` or `admin`. Enforce this on the API as well as the navigation/page. Workspace names, fixed UUIDs, `CASDOOR_CONFIG_ADMIN_ACCOUNT_IDS`, Casdoor login targets and RBAC settings do not grant this permission.
- Casdoor product scope is SSO integration: configuration, validation, automatic Discovery/JWKS verification, login callback, local session and required workspace role mapping. Do not expand current product documentation into additional Casdoor identity governance, lifecycle, avatar recovery or logout capabilities simply because related source files exist.
- Backend extensions include `api/controllers/console/*_extend.py`, `api/services/*_extend.py`, `api/models/*_extend.py`, `api/migrations_extend/`, and extension event/schedule modules. Casdoor frontend integration is under `web/features/casdoor/`; application distribution and quota UI also live in native Dify components.
- Account `total_quota` / `used_quota` is a Dify-Plus personal balance in USD, separate from upstream Cloud quota. Navigation may display converted RMB. App API keys have daily and monthly limits; `accumulated_quota` records cumulative usage, not a lifetime cap. Keep logged-in account, app and API-key attribution intact.
- WebApp login is configured per application and defaults to required. Anonymous access when disabled does not have personal-account fee attribution. Service API key authentication remains separate.
- Code execution authorization matches the current workspace owner's email. Matching workspaces use `sandbox-full`; others use the ordinary sandbox. Do not interpret it as an arbitrary member allowlist.
- Cross-workspace model synchronization structures are legacy and lack active callers. Configuration fields, unmounted UI components and commented routes must not be documented as available user features.

## Deployment and migrations

- The fork deployment entry is `docker/docker-compose.dify-plus.yaml`. `generate_docker_compose` generates the upstream Compose file, so review fork changes separately.
- Use matching fork API images for API, workers, WebSocket, migration and secret-key initialization, and a matching fork Web image. Upstream image defaults do not contain fork extensions.
- Execute both migration chains with one migration executor: `flask db upgrade`, then `flask extend_db upgrade`. Keep automatic migration disabled on business-service replicas. Preserve data and recovery artifacts before upgrades.
- Configure the required `DIFY_AGENT_SERVER_SECRET_KEY`. Optional examples under `docker/envs/` are references; verify actual Compose environment wiring rather than assuming all files are loaded by the fork entry.
- Follow the [current deployment guide](docs/dify-plus/二开部署配置与运维说明.md). Upgrade-specific evidence remains in the [runbook](openspec/changes/merge-upstream-1-17-1/runbook.md) and [execution graph](openspec/changes/merge-upstream-1-17-1/execution-graph.json). Source, tests, images, running services and production acceptance are separate forms of evidence.

## Documentation and screenshots

- README describes the final product and its complete core extensions. Keep implementation history, adjustment narratives and verification logs in historical records rather than current feature introductions. The native implementation replacing GVA may be stated as a product architecture fact.
- Verify features through production callers, routes, permissions and configuration. Preserve conditional capabilities and accurate limits; do not infer availability from filenames or old plans.
- Keep current frontend/backend/database/deployment guides, the documentation index and README consistent. Simplified Chinese is the maintained fork overview; other locale READMEs describe upstream Dify unless explicitly updated.
- Store real feature screenshots in `docs/dify-plus/images/`. Mask configuration URLs, emails, account identifiers, passwords, client credentials, tokens, avatars and sensitive business content before saving. Do not commit raw screenshots, credentials or browser/session data. Document screenshot context in [the screenshot guide](docs/dify-plus/截图说明.md).
- Documentation-only updates require link/path checks, stale-feature checks and visual privacy review. Do not use documentation changes to imply a new runtime deployment or live external-provider acceptance.
