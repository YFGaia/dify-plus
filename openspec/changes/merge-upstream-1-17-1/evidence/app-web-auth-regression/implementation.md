# N08 WebApp request scope regression

Source HEAD: `953cf1c1ef88258197c72b98e89fcd1dab85ac39`, with the two owned uncommitted paths in `source.sha256`. Previous N07/K01 source and image evidence remains unchanged. No branch, staging, database edit, key creation, owner login/logout/refresh, or user data changes were performed for this fix.

## First broken transition

On isolated `localhost:25442`, legitimately issued anonymous-browser passports for two synthetic apps passed signature validation. `GET /api/site` with explicit `X-App-Code=B` and passport A returned 200 and A's app ID; the reverse also returned 200 and B's app ID. Same-app controls returned their own app IDs. See `before-cross-app.json`. This is request-scope confusion; these responses do not demonstrate access to B's data using A's token.

Baseline API image: `difyplus-acceptance-api:953cf1c-n07-k01-1a805b0e`, image ID `sha256:51347867bb65e505d37ba1730db71192ba30904035e2eece6d1c43bb2e108619`. Web: `difyplus-smoke-web:970b704e351f`, image ID `sha256:412b2cad11badfcfa25a3e19490aa329059245c7c07842b9c7d0230eeb4c8569`.

`decode_jwt_token` overwrote the caller's app code with the signed claim before resolving App/Site/EndUser. Protected resources pass the header; LoginStatus passes the validated query code. No controller body field is an app-scope input to this wrapper.

## Minimal fix

Retain both explicit sources before decoding. After signature validation, require each provided code to match the passport claim, before ORM or enterprise access dispatch. Raise existing `WebAppAuthAccessDeniedError` (401); it does not enter the `Unauthorized` enterprise fallback. Preserve existing cookie selection, signed header-passport calls without an explicit code, legitimate query calls and same-app dispatch. This does not alter fork generation authentication, enterprise modes, billing, persistence, or app/site/end-user ownership policy.

## Verification

`targeted-tests.log`: 78 passed in 2.36s, comprising the 26 new signed-JWT/real-ORM checks plus existing wrapper, passport-resource, generation-auth matrix and cryptographic passport tests. New checks cover completion/chat/workflow, header/query/cookie/no-explicit-code success, A-to-B and B-to-A mismatch, conflicting query/header, SELFHOST and enterprise mismatch rejection before either DB dispatch, missing token and wrong signature. The first new fixture omitted mandatory `App.enable_api`; its 26 setup errors are retained in `first-fixture-failure.log`, then corrected in the test fixture only. Existing suite issued 18 known warnings. Only whitespace formatting followed the passing run.

Ruff 0.16.6 focused lint and format checks passed, with `--ignore B903` because the repository's historical B903 rule is unsupported by this installed Ruff version. `git diff --check` passed. No repository lint configuration was changed.

Independent source review and complete candidate-image rebuild/live A-to-B rejection remain separate gates; these source tests do not certify deployed behavior or SSR live transport.

## Independent latest-image result

Subsequently, `../N08/independent-source-review.json` reported **No issues found**. Runtime acceptance independently built and deployed the complete N08 API image `difyplus-acceptance-api:953cf1c-n08-d431d0a6`, ID `sha256:0567cc1430de64cb194e086d52708a7f97cda9073f105f4fa54a3037d668792d`, context SHA256 `d431d0a6c0266667649cceb30bcf2139aa6e71b4c8f1f848f0918afc74a0d1f6`. The executor reused private existing legitimately issued passports and performed GET-only requests, without login or database writes. `../N08/runtime-passport-scope-2026-09-30.json` records 11/11 cases passed: both cross-app directions now 401, same-app/no-explicit-code completion/chat/workflow remain 200 for the expected app, and login-status query/header cases return the expected true/false login scope. API health 200 and direct WebSocket transport open were separately checked. Original 23000 deployment is a separate runtime-owned gate.
