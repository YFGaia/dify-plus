# Remaining ledger ownership and minimum external acceptance steps

Read-only ledger snapshot after N08/B04/B06 checks, before N09 installed closure. No credentials are read or included. This is a handoff checklist, not additional acceptance results.

## Local ownership

| Exact remaining ledger subchecks | Executor |
| --- | --- |
| B11.real_secondtenant_Conversation_context_GET_DELETE_reject | app_center: N09 positive own-tenant HTTP200, foreign GET/DELETE reject, markers unchanged; real PG/MySQL selected delete and rollback |
| F01.category_tag_search_sort_dedup; F01.default_home_no_redirect_explicit_destination | merge_audit: owns Space9/p2 after app_center release, own tagged published fixture |
| F02.sync_cancel_cache_refresh_member_reject | merge_audit: same own fixture, role scopes and UI cache refresh |
| D01.latest_fullimage_dualengine_4models_quota_write_read_rollback | runtime: final complete candidate identity and existing dual-engine regression probes; app_center supplies empty dedicated DBs and the N09 probe |
| D05.own_samples_delete_rebuild_file_consistency | runtime: its own economy dataset/docs, never our DatasetB/center samples |
| O01.plugin_enabled_operation_and_registry_identity | runtime: actual installed plugin runtime/registry identity and callable operation |
| O02.actual_disabled_flags_effective_no_dependency_startup_break | runtime coordination: actual effective disabled flags, request behavior and healthy startup; do not imply an enabled Agent/SSO dependency |
| O03.generic_allowhost_deniedhost_actual_proxy; O03.agent_specific_scope_no_generic_whitelist_expansion | runtime coordination: actual configured proxy allow/deny controls, dedicated targets; isolated Agent-specific scope must preserve generic allowlist |

O02/O03 ownership is explicitly raised with the coordinator/runtime because the ledger has no executor field; these checks must not disappear behind external LLM/SSO conditions. Local backup/restore and final original23000 candidate delivery are runtime-owned N06 gates outside these scene subcheck lists. N09 service/image advancement must preserve earlier validated source paths and lock.

## External inputs and smallest executable follow-up

- **B01/B03/F04 SSO/integration:** authorized external identity-provider app and callback endpoints, synthetic provider identity, and credentials entered by their owner. Configuration surfaces: Console system management integration for fork DingTalk/OAuth2/email API; deployment core-service auth env examples for GitHub/Google and provider-specific auth. Save through supported product UI, exercise the enabled provider's connection test and genuine callback with valid state/invitation scope, then verify session/current workspace/profile. Wrong/missing state and invitation recipient must reject. Do not enable a provider with placeholder secrets or turn on real outbound mail to example.invalid. For email API tests, the owner must authorize a real test endpoint/recipient and supply credentials privately.
- **B05/B07/B08/B09/B11 LLM:** authorized reachable model provider, its installed real plugin/model, and actual nonzero input/output pricing. Configuration surface: Console Integrations → Model Provider and each dedicated test app's model selection. No secret in an artifact or API response dump. First validate one small prompt with a low output limit, capture only provider/model identifiers, usage, effective prices/currency and request/run IDs. Before/after balances and key daily/monthly/cumulative counters must reconcile to actual priced tokens and trusted actor. Then run completion/chat/workflow auth combinations, Console/Explore/WebApp actor cases, five service modes and zero/day/month/balance boundaries, stop/retry/resume usage, and multi-turn retention with a short unique marker sequence. Zero-token native workflows prove tool/auth plumbing but do not prove nonzero billing or generated conversation retention.
- **D05 embedding/high-quality RAG:** authorized embedding provider/model and valid vector-store transport settings. Configure the provider in Console Integrations → Model Provider; use the dedicated dataset's high-quality indexing settings and deployment vector-store env example (Qdrant/Weaviate as actually installed). Build two short synthetic documents; verify real embedding/index completion, vector counts and retrieval, then delete/rebuild and compare DB/file/vector consistency. TLS/gRPC configuration must reflect actual server support; no fabricated provider or embedding responses and no silent disabling of TLS to manufacture success.

The ledger already marks historical database/data/credential upgrade, historical whole-version restore and production release scenes conditional-external. Their inputs are the authorized recoverable old-environment snapshot, encryption keys/provider credentials and production release responsibility described by the runbook; fresh isolated installation/source tests do not replace them. Preparation can remain complete while those external business/release gates remain conditional.
