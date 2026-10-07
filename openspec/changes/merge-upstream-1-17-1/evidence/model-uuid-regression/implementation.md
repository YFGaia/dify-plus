# N04 four fork-model UUID ORM regression

2026-09-30, Sol 6.1 implementation and targeted verification. Starting HEAD `953cf1c1ef88258197c72b98e89fcd1dab85ac39`. Final two-file source hashes are in `source.sha256`; no stage/commit performed.

## Prove the risk before changing source

The four classes in `api/models/model_extend.py` used a server-only `uuid_generate_v4()` ID default. Existing MySQL migrations correctly produce `UUID()` defaults, but SQLAlchemy/MySQL cannot obtain that noninteger generated PK for ORM identity after flush. This is different from DDL/migration success.

The exact unmodified `model_extend.py` was copied as a read-only source overlay into the existing API verification image (`sha256:5e159ddcab422a4a609e042eec9b17460e07024e03c821f93b7e06ea87647f6b`). No migration was run. The already-migrated, **dedicated** `difyplus-verify-postgres-20260930` and `difyplus-verify-mysql-20260930` databases were reused; these two database containers were stopped before this check. The probe refuses to insert if any Account or App exists in its database. Both empty-user/application guards passed.

`probe-before.py` instantiated each actual model with ID **omitted**, called ORM flush, read the ID/row, and always rolled back. Its JSON evidence intentionally omits credentials, request headers, generated test identifiers and business contents.

| Model | PostgreSQL before | MySQL before |
| --- | --- | --- |
| AppStatisticsExtend | flush/readback UUIDv4 passed | FlushError: NULL identity |
| EndUserAccountJoinsExtend | flush/readback UUIDv4 passed | FlushError: NULL identity |
| AppExtend | flush/readback UUIDv4 passed | FlushError: NULL identity |
| MessageContextExtend | flush/readback UUIDv4 passed | FlushError: NULL identity |

All before checks started and ended with zero rows in their target table. Original failure JSON is preserved in `mysql-before.jsonl`; PostgreSQL control is `postgres-before.jsonl`. Raw local process logs remain under `/private/tmp/dify-model-uuid-20260930/`.

## Minimal correction and actual re-verification

Only the four model ID columns gained `default=lambda: str(uuid4())`, preserving the server default, schema, revision chains, existing ID values and application behavior. No migration/head change is required. This fixes omitted-ID constructors used by first usage statistics, end-user account mapping, app settings and context records. The separate new-app event's explicit UUID alone could not repair those older constructors.

The corrected model file was frozen as a read-only overlay into the same verification image. `probe-after.py` repeats the same omitted-ID check and additionally tests explicit ID preservation and all supplied payload fields on a real database readback.

- PostgreSQL: all 4 omitted-ID UUIDv4 flush/readback checks and all 4 explicit-ID/payload checks passed.
- MySQL: all 4 omitted-ID UUIDv4 flush/readback checks and all 4 explicit-ID/payload checks passed.
- Every inserted synthetic row was rolled back; each table's pre/post count remained zero. No user database or model/provider data was accessed.
- This is real dual-engine **ORM acceptance on a source overlay**, not a claim that the final complete image has already been built or deployed. The runtime verifier was told to incrementally rebuild its final API image with the new source hash; its earlier build is only a cache/base.

## Unit/static regression

`test_model_extend_uuid_defaults.py` adds 8 tests: actual omitted-ID two-row unique UUIDv4 generation/flush/readback/rollback and explicit-ID preservation for each class. It checks the server default contract remains present. Together with N01 and existing related tests: **60 passed**, 2 existing deprecation warnings, 3.19 seconds. Exact log: `targeted-tests.log`.

Command:

```
UV_PROJECT_ENVIRONMENT=/private/tmp/dify-m02-python-complete uv run --project api --no-sync pytest -o addopts='' -p no:benchmark --timeout 60 \
  api/tests/unit_tests/models/test_model_extend_uuid_defaults.py \
  api/tests/unit_tests/services/test_app_center_extend.py \
  api/tests/unit_tests/events/test_app_statistics_extend.py \
  api/tests/unit_tests/services/test_recommended_app_service_extend.py \
  api/tests/unit_tests/events/test_app_event_signals.py \
  api/tests/unit_tests/controllers/console/explore/test_installed_app.py
```

Both new changed files pass Ruff 0.16.6 `check` and `format --check`; `git diff --check` passes. N01's six-file manifest remains unchanged. The dedicated database containers are restored to their prior stopped state after the probe; their volumes and historical first-failure evidence are retained.
