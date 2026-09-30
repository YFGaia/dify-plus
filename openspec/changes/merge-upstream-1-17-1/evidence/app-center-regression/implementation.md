# N01 application-center visibility regression

Date: 2026-09-30 (Asia/Shanghai). Implementer: Sol 6.1 sub-agent.

## Source and verification boundary

- Starting HEAD: `953cf1c1ef88258197c72b98e89fcd1dab85ac39`.
- Six implementation/test files are frozen in `source.sha256`. The files are uncommitted at this evidence point. No unrelated dirty paths or lockfiles were edited by this implementer.
- This report establishes source regression checks only. The running `23000` Web and `25432` API were still using images tagged `970b704e351f` at investigation time. Complete-image and real UI re-verification belong to the independent runtime verifier.
- No application, provider, credential, or statistics data in the running workspace was mutated for this fix. The read path remains read-only; no migration or backfill is required to show existing applications.

## First broken transition

Ego-browser TaskSpace 9 / p1 inspected `http://127.0.0.1:23000/explore/apps-center-extend`. The authenticated app center showed categories and search but no app card. `/apps` and the WEB APPS sidebar both displayed the existing **Quota smoke chat**.

The selected service is `difyplus-upstream1171-quota-selftest-20260930`. Read-only PostgreSQL evidence for the app (`d7af1af0-d92f-4d78-bbfa-8802de395199`):

| Attribute | Observed |
| --- | --- |
| mode | advanced-chat |
| published workflow reference | present |
| current-owner installed rows | 1 |
| app_statistics_extend rows | 0 |

The API logged HTTP 200 for `GET /console/api/installed/apps` with an empty list. `RecommendedAppService.installed_app_list()` selected App with an inner join to optional usage statistics, excluding a newly published application before its first counted use. The upstream app-creation refactor no longer initialized fork statistics.

A second compatibility break was hidden by that empty query: the service read `app.tags`, but upstream 1.17.1 only has `tags_with_session()`. A read-only `App()` accessor check reproduced `AttributeError: 'App' object has no attribute 'tags'`.

## Scoped implementation

- Query current-workspace installations with the complete App/InstalledApp tenant and owner chain. Preserve the workspace-owned app-center boundary; do not include uninstalled apps or expand to foreign-workspace installs.
- Reuse the installed WebApp publication-availability SQL predicate, excluding unpublished/missing publish targets and unsupported Agent WebApps.
- Left join optional per-app statistics; treat missing statistics as zero. Group duplicate historical statistics by maximum usage so ranking does not duplicate app-center cards. Keep usage-descending order and stable tie breaks.
- Read tags/model-config through explicit session accessors. Keep categories and published prompt description fallback.
- Register a fork app-created signal receiver that initializes statistics once in the caller's session, preserving existing counts, not committing, and preserving upstream owner commit/signal ordering. Use a client UUID for the new row so creation does not depend on a PostgreSQL-only ID default.

## Focused verification

The ordinary local venv had an older Ruff and lacked pytest coverage/timeout plugins. The already-existing complete environment `/private/tmp/dify-m02-python-complete` supplied Python 3.12.9, Ruff 0.16.6 and the required test plugins. No shared environment or lockfile change was made to resolve tooling.

```
UV_PROJECT_ENVIRONMENT=/private/tmp/dify-m02-python-complete uv run --project api --no-sync pytest -o addopts='' -p no:benchmark --timeout 60 \
  api/tests/unit_tests/services/test_app_center_extend.py \
  api/tests/unit_tests/events/test_app_statistics_extend.py \
  api/tests/unit_tests/services/test_recommended_app_service_extend.py \
  api/tests/unit_tests/events/test_app_event_signals.py \
  api/tests/unit_tests/controllers/console/explore/test_installed_app.py
```

Result: **52 passed**, 2 existing deprecation warnings, 2.78 seconds. Exact output: `targeted-tests.log`.

The 14 newly added tests cover missing statistics for published chat/chatflow/workflow, ranking of used/zero/missing/duplicate statistics, unpublished and Agent exclusions, missing publish targets, complete installation tenant/owner scope, explicit-session tenant-scoped tags/prompt fallback, event registration, no duplicate initialization/no commit/rollback, and preserving existing usage.

All six changed implementation/test files passed Ruff 0.16.6 `check` and `format --check`. `git diff --check` passed. The controller response schema and authentication decorators were unchanged; independent role/UI checks remain separate acceptance evidence.

## Runtime handoff

The verifier received the stable six-file manifest and test output before starting its build. The decisive real-world check is that **the same existing app with zero statistics rows becomes visible and opens successfully after updating the actual API image**, while the statistics count remains zero. New-app lifecycle, category/search and publication/sync controls should be exercised only in an authorized disposable acceptance workspace, not by changing user data to force this reproduction to pass.
