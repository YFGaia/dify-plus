# Empty MySQL migration and static data-risk audit

**Result:** Empty MySQL 8.0.46 migration failed during the Dify-Plus extension chain. V03 remains blocked.

**Date:** 2026-09-30 (Asia/Shanghai)

## Isolated input

- Candidate commit: `970b704e351f8b98d1f0450e5dd50734b5d21e8c`
- Candidate tree: `ccb16e6692f53c68fe13e2ca51036649c707735d`
- API image: local candidate build `sha256:5e159ddcab422a4a609e042eec9b17460e07024e03c821f93b7e06ea87647f6b`, `linux/arm64`.
- Database: isolated `mysql:8.0`, server reported `8.0.46 MySQL Community Server - GPL`.
- Database image ID: `sha256:b7e118f56c5963e079252e0d6e2978c9c010eb7fc7aaaec100e367796b5dd08f` (`linux/arm64`).
- Compose project: `difyplus-upstream1171-mysql-empty-20260930`; a new empty project database was used.
- Rendered configuration used an internal-only network. The DB had no host port or bind mount; the one-shot migration service had no bind mount. Synthetic credentials and temporary override stayed outside the repository with mode `0600`.
- `docker compose ... config --quiet` exited 0. The MySQL service became healthy before the migration invocation.

## Migration result

The exact candidate's migration command was invoked once and exited 1. The main Alembic chain completed and read back as `c3f1a9b2e6d4`. The extension chain's initial revision (`001_recommended_list_sorted`, no down revision) failed in `api/migrations_extend/versions/06b18b329024_recommended_list_sorted_by_usage_.py` while creating `app_statistics_extend`:

```text
MySQL error 3770: Default value expression of column 'id' contains a disallowed function: uuid_generate_v4
DDL: id CHAR(36) NOT NULL DEFAULT (uuid_generate_v4())
```

After the failure, a read-only `information_schema` check found 145 base tables, zero rows in `alembic_version_extend`, and no `app_statistics_extend`, `account_money_extend`, or `api_token_money_extend` table. The only table matching the `_extend` suffix was `alembic_version_extend`; no extension business table had been created in this empty database. No business row values were read. The full stack trace was not retained; the error, DDL, and revision above are transcribed from the migration output and readback. No retry or source workaround was attempted. The isolated DB service was left running for read-only inspection.

This is evidence of an empty-MySQL migration incompatibility in the tested candidate. It does not establish the production database engine/version, the status of other MySQL versions, or a full supported-engine matrix. The separate empty-PostgreSQL pass remains successful and was not repeated here.

### Follow-on UUID-default source scan

A read-only source scan of `api/migrations_extend/versions` found the literal `uuid_generate_v4()` server-default expression in 13 migration files: revisions `001_recommended_list_sorted`, `002_recommended_apps_category`, `003_tenant_model_sync_extend`, `004_ai_billing_forwarding`, `005_account_money_extend`, `006_end_user_account_joins`, `007_account_money_monthly_stat`, `008_account_layover_record`, `009_api_token_money_extend`, `013_app_extend`, `014_message_context_extend`, `015_code_execution_control`, and `018_drop_recommended_cats`. Only revision 001 was reached in the MySQL run. The other 12 sites were not runtime-tested; they are follow-up review sites, not additional observed failures. Repairing the first DDL alone would not establish MySQL compatibility.

## Static migration-risk review

Read-only source inspection covered the candidate migrations below. These are risk findings to validate against a user-authorized old-database copy; they are not runtime data-audit results.

| Revision | Source file | Static behavior observed | What remains unverified |
| --- | --- | --- | --- |
| `f6e4c5686857` | `api/migrations/versions/2026_07_23_0203-f6e4c5686857_replace_agent_runtime_sessions_with_.py` | Creates Agent workspace/binding tables and drops `agent_runtime_sessions` without copying rows; downgrade recreates an empty old table. | Session-row counts, user disposition, and whether the source environment has rows. |
| `89919253ca7a` | `api/migrations/versions/2026_08_17_1740-89919253ca7a_remove_agent_drive.py` | Removes `files`/`drive_key` from JSON in several tables and drops `agent_drive_files`; downgrade recreates an empty table. | Affected-row counts, surviving references/assets, and approved disposition. |
| `fbdfcf5f5a6e` | `api/migrations/versions/2026_08_20_0938-fbdfcf5f5a6e_clean_legacy_agent_soul_files.py` | Removes legacy Agent `files` JSON values from snapshots/drafts with dialect-specific SQL; one-way cleanup. | Before/after counts and whether any live Agent configuration depends on those values. |
| `925e75620b69` | `api/migrations/versions/2026_08_21_1422-925e75620b69_simplify_agent_v2_output_contract.py` | Removes declared Agent output entries named `text`, `files`, and `json`; migration comments describe this as one-way because removed declarations cannot be reconstructed. | Affected workflow count and business approval of the changed output contract. |
| `9b7c6d5e4f3a` | `api/migrations/versions/2026_08_25_1200-9b7c6d5e4f3a_add_normalized_email_to_accounts.py` | Backfills normalized account email, including Gmail/Googlemail dot and plus-alias normalization; creates a nonunique index. | Duplicate/collision counts and resulting account resolution behavior for real rows. |
| `5578e028b2f2` | `api/migrations/versions/2026_08_27_1200-5578e028b2f2_migrate_legacy_model_types.py` | Canonicalizes model types, deletes duplicate model/credential rows using `updated_at` then `id` ordering, and rewrites provider-model/load-balancing credential references and encrypted config. | Winner correctness, reference closure, and decryption/credential usability with the environment key. |

The empty databases contained no legacy rows to compare. No source credential values, decryption keys, user-owned database copy, or business records were inspected. Do not infer migration-data safety from the successful PostgreSQL empty-schema pass or this static review.

## Gate status

- 18.1 remains incomplete: locally built images and isolation were exercised, but published image digest/provenance and the environment-specific fixed release override are not established.
- 18.2 remains incomplete: empty PostgreSQL dual migration passed in the prior report; empty MySQL extension migration failed; no old-database copy was available.
- 18.3 remains incomplete: static source review only; old-row before/after and credential decryptability checks are outstanding.
- 18.4 records a failure on empty MySQL 8.0.46. Production engine/version and any broader supported-engine claims remain unverified.
- 18.5 remains blocked: expected PostgreSQL heads were observed, but the MySQL failure and old-copy/data-disposition, image provenance, and target-environment evidence remain open.

See `result.json` and `execution.log` for the combined node record. V03 remains blocked; no downstream or production gate is accepted.
