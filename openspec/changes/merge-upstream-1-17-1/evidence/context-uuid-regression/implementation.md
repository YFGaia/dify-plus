# N09 context marker text/UUID compatibility

Base HEAD `953cf1c1ef88258197c72b98e89fcd1dab85ac39`; two owned source paths frozen in source.sha256. Complete 18-path overlay manifest in latest-source-manifest.json inherits N08, replaces the shared recommendation service hash and adds the new type-contract test. Earlier N01/N08 evidence remains historical and is not overwritten.

## Actual first failure

Installed complete N08 image `difyplus-acceptance-api:953cf1c-n08-d431d0a6` returned 500 for the valid synthetic admin reading an existing own-tenant Conversation/Message/Context fixture. Actor workspace summary and its AppB detail each returned 200 before this request. Filtered API stack frames locate `RecommendedAppService.message_context` line210; PostgreSQL reports `UndefinedFunction: operator does not exist: character varying = uuid`. See before-http-failure.json.

The marker extension retains a `varchar(36)` conversation ID; the owner subquery selects upstream `Conversation.id` as PostgreSQL UUID. Thus a legitimate authorized read fails at the SQL comparison. The same owner subquery is shared by marker deletion. This acceptance failure is not a guessed missing-data or authentication problem.

## Fixture provenance and ownership

Installed normal workspace creation policy is false. It was checked and not bypassed through setup/dashboard flags. The coordinator explicitly authorized a synthetic ORM Tenant plus standard `TenantService.create_tenant_member` owner binding as a negative-boundary fixture. The ACTIVE synthetic admin retains its original TenantA admin membership; canonical owner TenantA membership is untouched. The admin legitimately logs in independently and switches to TenantB using the product API.

AppB and metadata-only DatasetB were created through normal product APIs; their own-tenant details return200. AppB key list initially has0 keys. ConversationB, MessageB, ContextB, EndUserB/account join and NULL-auth AppExtend are explicit authorized ORM fixtures, not real LLM outputs or UI-created workspaces. All IDs and chain/readback controls are in fixture-owner-chain.json. No production data, center fixtures, existing datasets, quotas or model/provider credentials were changed. Synthetic admin must be restored using product workspace switch after this batch; B fixtures are retained until audit's independent cross-tenant key checks finish, then cleanup only by their recorded IDs.

## Minimal correction

Cast only the owner subquery's selected Conversation.id to `String(36)` before comparing it with legacy marker text. Keep tenant ID, app ID, conversation ID and `is_deleted=False` predicates exactly; leave authorization, markers, schema, migration heads, sorting and transactions untouched. Read and delete use the same helper. The shared service retains all prior N01 app-center fixes.

## Focused verification and remaining image gates

80 existing context authorization/read/delete, app-center visibility and lifecycle checks plus the new two-dialect text identity contract passed in2.81s; targeted-tests.log. The new contract verifies PostgreSQL VARCHAR(36) and MySQL CHAR(36) projection with the marker column's text type. Existing functional tests protect all owner predicates and selected-marker deletion. Ruff0.16.6 focused check (unsupported historical B903 ignored), format check and diff check passed; no lint configuration changed.

probe-dual-engine.py is prepared for the complete N09 image on the two already migrated dedicated empty verification databases. It compares the original query's first failure with the corrected real query, verifies positive reads, foreign/app/deleted guards, selected deletion and outer transaction rollback. Service commits are isolated by an external transaction with `join_transaction_mode=create_savepoint`. Actual dual-engine and installed-image HTTP results are separate pending evidence; source tests do not replace them. The same live TenantB actor must return200 for its known context before foreign-tenant no-leak/no-delete closure can pass.
