# O02 isolated Agent sidecar plan

Status: design only. No sidecar was started, no original API environment or service was changed, no Agent fixture was created, and no model call was made for this plan.

## Why a sidecar is needed

The original `23000` project has `AGENT_SHELL_ENABLED=true` and `AGENT_BACKEND_USE_FAKE=false`. Its backend target is configured but unreachable, its Agent API token is absent, and the project has no `agent_backend`, `local_sandbox`, or Agent-specific SSRF proxy service. The existing generic SSRF proxy is present. The protected published model schema reports tool-call, multi-tool-call, and streaming tool-call support, but that does not prove Agent execution.

The proposed API sidecar uses the accepted full image `difyplus-acceptance-api:953cf1c-n10-headlock-3cf5bfa6` (observed image ID `sha256:445d59dbbab0f377a63c0b6a731ca25b00c35a2ba4019f1846881ba247a65172`). It attaches to the original database/plugin networks and original API storage read-only so the existing tenant RSA private key and persisted Dify signing key remain available. The existing API storage is a named volume, `difyplus-upstream1171-quota-selftest-20260930_quota_selftest_storage`, mounted at `/app/api/storage`. Runtime configuration is `STORAGE_TYPE=opendal`, `OPENDAL_SCHEME=fs`, `OPENDAL_FS_ROOT=storage`, with working directory `/app/api`, so the effective root is `/app/api/storage`. The loaded `SECRET_KEY` env field is empty; `api/configs/secret_key.py` resolves the persisted `.dify_secret_key` file under this root. Read-only fixed-path checks found `.dify_secret_key`, `privkeys/`, and the protected tenant private-key file present; `upload_files/` did not exist at capture time.

The sidecar mounts the original named volume read-only at `/app/api/storage` and overlays a new owned volume read-write only at `/app/api/storage/upload_files`, matching `FileService`'s actual upload prefix. The nested mount avoids copying RSA or signing keys and gives test uploads an isolated backing store. Because Docker may materialize the absent nested mountpoint in the parent volume before applying the mount, startup acceptance must check the original volume before and after: allow at most creation of an empty `upload_files/` mountpoint, require no original files to change, and require any uploaded test object to exist only in the owned volume. Do not claim byte-for-byte unchanged storage without that readback.

The sidecar service is named `o02_api` and is attached to original networks without claiming `api` there. It gets the `api` alias only on the new private file-proxy network so the Agent proxy's existing `/files/` rule resolves correctly. It binds only `127.0.0.1:25452` and publishes no Agent, sandbox, Redis, or proxy ports.

The no-model package has exactly five services: API sidecar, Agent backend, local sandbox, restricted Agent SSRF proxy, and a dedicated Agent Redis. Agent runtime Redis has no connection to the original Redis/Celery queues. The API sidecar has no Celery worker, beat, migration process, or queue consumer. It uses `MODE=api`, `MIGRATION_ENABLED=false`, and `EXTEND_MIGRATION_ENABLED=false`. The API Agent backend URL and token are sidecar-only overrides. Provider credentials stay in the original tenant database and are resolved only by the API-owned ModelManager; they are not copied to the Agent or a new tenant.

The API request path is in-process: `api/core/app/apps/agent_app/app_generator.py` starts `AgentAppGenerator` in the API request thread (lines 195 and 326); `api/core/app/apps/agent_app/basequeue.py:64` uses an in-process `queue.Queue`. No Celery/Gaia worker is needed for a single Agent turn. This plan does not dispatch such a turn.

## Private inputs and equality checks

The fragment is not executable until a coordinator reviews current Compose labels, network names, storage volume identity, and mounts. Private environment files must be mode `0600` and contain only an explicit allowlist. Never print or persist their values in tracked evidence.

| Placeholder or private field | Source | Handling |
| --- | --- | --- |
| `O02_API_ENV_FILE` | New mode-0600 file assembled from an explicit whitelist of the current original API's loaded DB, Redis/session, signing, plugin-inner, plugin-daemon, storage, and required non-secret runtime fields | Do not copy a whole container environment or provider settings. Keep API DB/storage identity identical to the original runtime. |
| `O02_ORIGINAL_API_STORAGE_VOLUME` | Exact original API storage volume name from a fresh Docker label/mount inspection | Declare as an external Compose volume. Mount it read-only at `/app/api/storage`; overlay only a new owned volume at `/app/api/storage/upload_files`. |
| `o02_owned_upload_files` | New named volume in the dedicated sidecar Compose project | The only writable API storage mount in this package; capture original-volume state before/after to detect mountpoint side effects. |
| `O02_AGENT_ENV_FILE` | New mode-0600 file with generated local-only Agent API token, 32-byte server secret, sandbox auth token, and the exact API/plugin key matches listed below | Never reuse an external provider key. Match only loaded API/plugin internal keys in memory. |
| `O02_SANDBOX_ENV_FILE` | New mode-0600 file with the same generated sandbox auth token as the Agent backend | No user shell/session credential is used. |
| `O02_AGENT_API_TOKEN` | One generated high-entropy local-only token | Same value only between the sidecar API's backend caller and Agent backend. Never print it. |
| `O02_AGENT_SQUID_CONFIG` | Tracked `docker/ssrf_proxy/squid-agent.conf.template` | Pair it with tracked `squid-common.conf.template`, `docker-agent-entrypoint.sh`, and the standard entrypoint command. Preserve `/agent-stub/` and `/files/` scope; do not widen the generic proxy. |
| `O02_AGENT_PROXY_ENV_FILE` | Explicit mode-0600 file containing only the standard Agent proxy's non-secret fields (`COREDUMP_DIR`, `HTTP_PORT`, and the two private-domain/IP policy fields) | Copy exact reviewed defaults from `docker/docker-compose.dify-plus.yaml`; do not invent a broader allowlist. |
| `O02_DOCKER_ROOT` | Absolute repository `docker/` directory | Used only for the two standard tracked Agent proxy mount paths. |

Before any future start, check these equalities in memory and record only booleans:

- API sidecar `AGENT_BACKEND_API_TOKEN` equals Agent backend `DIFY_AGENT_API_TOKEN`.
- Agent backend `DIFY_AGENT_INNER_API_KEY` equals the API sidecar's *loaded* `INNER_API_KEY_FOR_PLUGIN`; the loaded plugin daemon key is nonempty. Do not infer this from Compose alias names.
- Agent backend `DIFY_AGENT_PLUGIN_DAEMON_API_KEY` equals the plugin daemon's loaded `SERVER_KEY`.
- Sandbox and Agent backend auth tokens match. Agent backend server secret decodes to 32 bytes.
- Agent backend points to its dedicated Redis, private sandbox, dedicated Agent proxy, and `o02_api:5001` inner API. It must not connect to the original Celery Redis.
- The API sidecar mounts the original API storage read-only. Verify that this is the actual volume containing the tenant RSA key path; an empty storage volume cannot decrypt the tenant configuration.

Do not read, copy, rotate, or print provider credentials. Do not change the original API, original worker queues, plugin daemon, database, Redis, user application, pricing, or keys.

## Read-only acceptance sequence after separate approval

1. Capture preflight IDs and counts for only the protected owner, tenant, original app, original app config hash, and owner balance. Verify an existing legal owner session against the original API before starting the sidecar. Do not log in, log out, refresh, or inspect browser cookies from the sidecar.
2. Start only the five services from the reviewed fragment. Verify the exact API image ID and the three Agent image identities. Confirm no migration/worker/beat service started and no host port other than loopback `25452` is published. The Agent backend points directly to the sidecar API only for this no-model health/metadata package; no model-invoke request is sent. A reviewed invocation guard is a separate future gate, not part of this five-service startup.
3. Check API health on `127.0.0.1:25452`, then use the existing lawful owner session for read-only same-tenant summary/config reads. No account, app, or Agent creation is included in this phase.
4. If an already-existing product-created Agent and version still exist, mint a short-lived signed JWE only with the repository `AgentStubTokenCodec` and its recorded owner/tenant/Agent/version claims. Request the real `GET /agent-stub/config/manifest` through the dedicated Agent proxy. Require schema-level success from the API inner manifest route; an unauthenticated 401 or generic-proxy 403 is only boundary evidence, not a manifest pass. Send the identical request through the generic proxy and require its existing deny behavior.
5. Verify only existing, authorized file metadata/read paths. Do not upload, delete, or mutate a file in this phase. To test upload/preview/download/delete, create an explicitly owned disposable Agent and file only in a later approved package, record their exact IDs/hash before mutation, and clean up only those IDs after evidence is preserved.
6. Re-read the protected original app/config hash, owner balance, and scoped row counts. They must match the captured baseline. Stop only the sidecar services and remove only their newly created private volumes after a separate cleanup approval; preserve the original API/storage/DB/Redis/plugin and all rollback artifacts.

No model call, shell command, tool invocation, or generated Agent response is part of this plan. A future real Agent-turn package has no verified hard two-call guard: the current runner has a high request limit and SDK retry behavior. Do not claim a two-call cap based on prompt instructions or `max_tokens=8`; a real generation package needs an independently reviewed enforceable request/stop budget and its own approval.

## Current boundaries

- The original service topology lacks the Agent backend, local sandbox, and dedicated Agent proxy; the existing generic proxy is not a substitute.
- A second no-secret readback captured the original network names and aliases, Compose file inventory, actual named storage volume source, mounted path, effective OpenDAL root, and the persisted key files' existence. The five-service fragment passes `docker compose config --quiet` with temporary placeholder interpolation values. This proves Compose syntax/shape only; it does not prove startup, private equality checks, HTTP behavior, or data-write isolation.
- API and Agent backend share only a new private core network for the bounded no-model package. The Agent backend's inner API URL is `http://o02_api:5001`; there is no guard gateway in this five-service package. Do not invoke model-runtime endpoints. A separate gateway/forward-budget design needs its own implementation review and acceptance package.
- The isolated accepted image and the original API share the same source/image identity, but the sidecar composition and private key equality checks have not been executed.
- The earlier synthetic Agent fixture was removed during its exact cleanup. No current disposable Agent/version fixture is available to sign a real manifest request. Starting health-only services is separable from that fixture write; a real JWE manifest/file test requires a separately authorized, product-created owner fixture and an exact cleanup plan.
- A previous cleanup attempt for three disposable generation-test Apps was blocked by automatic review. It must remain untouched until the human user explicitly authorizes that exact cleanup. This is separate from the proposed sidecar and does not authorize fixture deletion.
- Manifest/file business checks are conditional on an already-existing legal Agent fixture. This plan does not create a fixture or bypass the currently required product owner chain.

## Files

- Review-only Compose fragment: `evidence/original-generation-runtime/o02-sidecar-compose.fragment.yaml`
- Runtime preflight and prior acceptance evidence: `evidence/original-generation-runtime/independent-actual-readback.json`
- Source and topology feasibility inventory: `evidence/remaining-runtime-feasibility-small-packages-2026-09-30.json`
