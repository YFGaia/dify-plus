# Owner partial live acceptance

2026-09-30. This is actual product UI and authenticated HTTP evidence on the isolated full API candidate; remaining admin/member roles are still running.

Exact API image: `difyplus-acceptance-api:953cf1c-uuid-quota-3c632717`, image ID `sha256:9b175d34b4e1bfde92a453e1baae4e6e306ce70e17562c0155734ba249fba429`. Web: `difyplus-smoke-web:970b704e351f`, image ID `sha256:412b2cad11badfcfa25a3e19490aa329059245c7c07842b9c7d0230eeb4c8569`. Ingress: `nginx:1.27-alpine`, ID `sha256:96868d9fa38f469a86d2f25787e43ee9ad330339d30be260aa9f5a338bb03751`. Candidate source/context and installed-lock verification are separately owned by the runtime verifier; no source overlay was used in this batch.

Web/API/WS endpoints are `localhost:23010/25442/25443`, Docker bound to `[::1]`. Ego TaskSpace 9/p2 owns the serial role test; the original 127.0.0.1 user tab/session was untouched. Dedicated owner was created through the actual `/install` form and auto-login. The initial 20-second navigation wait timed out, but the next observation showed the home page; setup/init status were both finished. No repeat setup mutation was attempted.

The actual profile and workspace APIs returned HTTP 200 and owner role. Test workspace: `f12143c1-4eab-48ec-92cf-0b4fb1f051d2`, owner: `b9f66aac-74db-4546-8858-8b43f36d3386`. Account money was 15 total/0 used. Runtime features confirmed COMMUNITY, password login enabled and email disabled. Ten actual owner-authorized invitations returned 201/success (one admin, nine normal), creating 11 test identities/11 initialized quota rows. All emails are example.invalid; no external mail was sent. Test passwords, activation tokens and cookies are excluded from durable evidence.

| Scene/subcheck | Actual result | Verdict |
| --- | --- | --- |
| F03 owner navigation / direct quota URL | System-management menu present; direct URL renders quota table | passed |
| F03 owner quota/integration/token/code-control APIs | six representative actual reads all 200, including current role owner | passed |
| F04 default pagination | 11 API rows; real UI 10 rows on page 1 and 1 row on page 2 | passed |
| F04 page-1 UI edit/readback | member01 saved 23.1234567; popup closed; precise API value matched | passed write; baseline tie-order issue below |
| F04 second-page UI edit/refetch | member09 first moved by edit, then actual page-2 edit saved 34.4567891 and auto-refreshed | passed write/refetch |
| F04 return page-1 and independent target persisted | member01 returned and displayed 23.1235; two precise target API values retained | passed |
| F04 search/edit/clear | exact member09 search, UI edit to 35.5678912, refreshed one result; clear restored all samples | passed |
| F04 page-size controls | actual UI 10→30→10; 30 view contains 11 distinct emails | passed |
| F04 forward-token CRUD | create 201, get 200 with created name, delete 200, get confirms removed | passed; secret value never printed |
| F04 code-execution-control CRUD/cache | create 201/read 200/delete 200; cache_synced true; read confirms removed | passed; dedicated whitelist entry cleaned |
| F05 invitation account mismatch | owner attempted actual admin invite activation: 403 invitation_account_mismatch | passed rejection; legitimate recipient flow follows |
| F04 external integrations / SSO | no real authorized external credentials/endpoints | pending external conditions; readonly config authorization only |

## Baseline pagination UX issue discovered live

All initial used_quota values were 0. Before editing, member01 was page 1 rank 3 and member09 was page 2 rank 11. After page-1 member01 update, its used_quota remained 0 but it disappeared from page 1 and moved to page 2; API keyword readback still returned its correct 23.1234567. The service query only orders by used_quota descending, with no stable tie-breaker; PostgreSQL upsert can change physical row order among ties. This is an actual baseline UX issue, not claimed newly introduced by the merge. The initial wait for the same target remaining in page 1 therefore timed out despite successful save.

The parent authorized a later minimal stable tie-order correction after role testing. N07 owns the next source candidate and its final-image rerun; this earlier candidate's write/readback evidence remains valid. These results do not claim N07 stable-pagination acceptance.

Raw sanitized UI snapshots and HTTP summaries are the adjacent evidence files. Private generated credentials/session files remain only in the protected temporary folder and are not copied here.
