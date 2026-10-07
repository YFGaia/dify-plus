# Dedicated runtime role acceptance

Candidate API: difyplus-acceptance-api:953cf1c-uuid-quota-3c632717, image sha256:9b175d34b4e1bfde92a453e1baae4e6e306ce70e17562c0155734ba249fba429. Web: difyplus-smoke-web:970b704e351f, image sha256:412b2cad11badfcfa25a3e19490aa329059245c7c07842b9c7d0230eeb4c8569. Installed lock/source context evidence is maintained independently by runtime. Browser Ego Space 9 p2, localhost:23010, API localhost:25442. Only synthetic example.invalid accounts in the fresh workspace, disabled mail. Owner + admin + 9 members legally invited gives 11 quota rows; owner/admin/member01 activated through actual product UI. Other members remain pending invited accounts used for list pagination; no claim all 11 have interactive role coverage.

| Scene/subcheck | Actual result |
| --- | --- |
| F03 owner navigation/direct/API | Passed: system management visible and editable; management GET 200 |
| F03 admin navigation/direct/API | Passed existing contract: no system-management nav, direct quota page shows 无权限访问; five management API GET 200, role admin 200 |
| F03 member navigation/direct/API | Passed: no system-management nav, direct quota page 无权限访问, role normal 200, five management API GET 403 |
| F04 admin write/readback | Passed: synthetic member02 quota POST 200, exact total 41.2345678 readback 200 |
| F04 member write denial | Passed: same target quota POST 403; subsequent owner readback remains 41.2345678, used 0 |
| F04 member personal balance | Passed GET /account/money 200, total23.1234567, used0 |
| F04 owner pagination/edit/search/page-size/token/code-control | Passed subchecks in owner-results.md; same-used stable pagination N07 pending final-image live recheck |
| B01/B03 identity | Actual first owner setup, legitimate admin/member password+invitation settings activation and final ordinary owner password sign-in passed; wrong-recipient invitation activation403 invitation_account_mismatch |
| N01 center UI | Both published installed stats-present and missing-stats cards display. Missing-stats app opens actual installed URL edfc85c1-c7d4-4f14-a64c-0009ded90b65, heading and 运行一次 form present. Two PNG and semantic snapshots attached; independent API/DB evidence owned by runtime |
| F05 UI/toolchain matrix | Role observations do not constitute full toolchain/build verification; separate runtime evidence required |
| External auth/SSO | Conditional: no real DingTalk/OAuth2 provider credentials/callback inputs; external integration not executed |

Admin response: admin-role-api.json; UI admin-direct-quota.txt. Member response: member-role-api.json; UI member-direct-quota.txt; denied-write immutable target proof member-denied-write-owner-readback.json. Center center-two-cards.txt/png and center-missing-stats-open.txt/png. Owner prior file used F05 label for invite tests; correct mapping here is B01/B03 identity, not whole F05.

Probe errors: first admin evidence write used a relative path in Ego Node process and failed ENOENT; that attempted request also used invalid quota field names, subsequently corrected to current account_id/quota and successful200. A transient owner GET omitted X-CSRF-Token and returned401; repeating once with standard header returned200. Runtime reported an older exported cookie file401 Invalid Authorization token; this is recorded separately with no unproven account-rotation attribution. Fresh canonical cookies exported through skill-supported page.cdp(Network.getCookies) and direct Node HTTP GET /console/api/apps plus CSRF returned200 count2, quota readback200. All cookie values/passwords/invite tokens remain private mode600 /private/tmp only; none in evidence.

Browser lock released to runtime after owner restored; no more role logout/login planned. N07 source change is later than this installed candidate and requires final-image revalidation. No production/user account, app, dataset, balance, or keys changed.
