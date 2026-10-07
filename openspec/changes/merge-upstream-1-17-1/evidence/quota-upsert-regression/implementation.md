# Quota-management dialect upsert regression

2026-09-30. Starting HEAD `953cf1c1ef88258197c72b98e89fcd1dab85ac39`; the three owned final source hashes are frozen in `source.sha256`. No stage or commit performed. N01 and the four-model UUID manifests remain unchanged.

## First real failure

`QuotaManageService.set_user_quota` used PostgreSQL `insert(...).on_conflict_do_update` unconditionally. The actual service was invoked using a read-only source overlay on API verification image `sha256:5e159ddcab422a4a609e042eec9b17460e07024e03c821f93b7e06ea87647f6b` against the already-migrated dedicated `difyplus-verify-postgres-20260930` and `difyplus-verify-mysql-20260930` databases. No migration ran and no current user stack was accessed.

The probe requires zero Account, App and AccountMoney rows before execution. It then creates one synthetic `example.invalid` Account, calls the actual service (including its own commit), performs database readback, and in a `finally` block deletes only that synthetic Account's balance and Account by its recorded ID. It verifies both tables return to zero. This is a direct service check; it does not claim authenticated HTTP or UI authorization acceptance.

PostgreSQL before passed insertion and update; seven decimal places read back exactly and used quota survived the total update. MySQL before failed on the first call with `UnsupportedCompilationError`: its compiler cannot render PostgreSQL `OnConflictDoUpdate`. The original sanitized exception is preserved in `mysql-before.jsonl`. All before checks restored the empty tables.

## Minimal fix and dual-engine acceptance

Only `set_user_quota` now chooses PostgreSQL `ON CONFLICT (account_id) DO UPDATE` or MySQL/MariaDB `ON DUPLICATE KEY UPDATE`. Both atomically insert missing rows and update **only** total quota on conflict. The existing unique Account ID constraint, client UUID default, used quota, seven-decimal database scale, controller authorization and account-ownership semantics are retained. No schema or revision-head change is required. Unsupported engines explicitly raise `ValueError` before a write.

`probe-after.py` ran the corrected source against **both** real engines:

| Check | PostgreSQL | MySQL |
| --- | --- | --- |
| Insert missing row and zero usage | passed | passed |
| Seven-decimal quota readback | passed | passed |
| Update total preserving `0.1234567` used quota | passed | passed |
| Four concurrent service updates to same existing account | passed | passed |
| Four concurrent service inserts/upserts for same missing balance | passed | passed |
| Exactly one balance after concurrency; final total is one requested amount | passed | passed |
| Synthetic Account/balance cleanup restores zero rows | passed | passed |

The dedicated database containers were restored to their prior stopped state; volumes remain intact. JSON evidence excludes credentials and generated identifiers. Runtime was informed the final complete API image must be incrementally rebuilt with the new source hash; these probes are actual ORM/service evidence on a source overlay, not final-image business acceptance.

## Tests and static checks

Five new focused tests compile the actual emitted statement for PostgreSQL, MySQL and MariaDB, confirm only total quota changes on conflict, and cover negative quota and unsupported-engine rejection before write. The related preexisting controller suite initially had seven fixture errors because it patched removed `dify_config.EDITION`; its sole source edit replaces that with current `DEPLOYMENT_EDITION`. Both the old intent and new guard are CLOUD setup bypass, with no product authorization change.

Command:

```
UV_PROJECT_ENVIRONMENT=/private/tmp/dify-m02-python-complete uv run --project api --no-sync pytest -o addopts='' -p no:benchmark --timeout 60 \
  api/tests/unit_tests/services/test_quota_manage_upsert_extend.py \
  api/tests/unit_tests/controllers/console/test_system_manage_extend.py \
  api/tests/unit_tests/services/test_account_quota_service_extend.py
```

**27 passed**, two existing deprecation warnings, 2.11 seconds. Exact successful output is `targeted-tests.log`. New implementation and new test pass Ruff 0.16.6 check and format; owned diff passes `git diff --check`. The older controller test has nine preexisting `ARG002` fixture-argument findings; its HEAD baseline and current findings match exactly (`static-checks.json`), so no unrelated test refactor was performed. The initial fixture failure is recorded above without copying pytest's full configuration repr into persistent evidence.

Full-image owner/admin/member UI/API acceptance remains a separate runtime batch.
