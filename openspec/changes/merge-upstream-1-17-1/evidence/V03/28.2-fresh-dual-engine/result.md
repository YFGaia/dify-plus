# Independent migration acceptance

## Evidence boundary

This pass used the existing API image `sha256:5e159ddcab422a4a609e042eec9b17460e07024e03c821f93b7e06ea87647f6b` on linux/arm64 with the final `api/migrations_extend/versions` snapshot mounted read-only. The versions manifest SHA-256 is `ccd43abd29650fa05543d3a367a83b1f20a7d44c0e54452e77f4f20e67cb51d7`. The follow-up ORM pass additionally mounted the two model files below read-only. This is an image plus source overlays, not a rebuilt or published release image.

Docker Engine 28.2.2, Compose v2.36.2-desktop.1, host architecture aarch64. Both projects used new project-scoped internal networks, database volumes, and dedicated app-storage volumes. Databases had no published ports or host bind mounts. Both DB containers are stopped; named volumes and failure containers are preserved. The quota-selftest, earlier failed MySQL project, repository, and source evidence were not modified.

## Migration results

| Engine           | Base head      | Extension head             | Result                                                                                                                                                                                                                                                                                                                         |
| ---------------- | -------------- | -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| PostgreSQL 15.12 | `c3f1a9b2e6d4` | `020_workflow_run_account` | Pass after an initial harness setup failure. The first attempt completed the base chain, then extension app startup failed because the standard permission/key initialization jobs were skipped. After running those jobs on the new app-storage volume, only the extension chain was continued; the base chain was not rerun. |
| MySQL 8.0.46     | `c3f1a9b2e6d4` | `020_workflow_run_account` | Pass after an initial pre-DDL authentication failure. A read-only check showed zero tables. The temporary Compose environment was corrected to match the synthetic DB password, and the standard migration ran once.                                                                                                           |

Read back 17 extension ID defaults on each engine. PostgreSQL reports `uuid_generate_v4()` except sequence-backed `system_integration_extend.id`; MySQL reports `uuid()` except `system_integration_extend.id`, whose default is NULL. A separate MySQL SQL transaction inserted and selected two rows without IDs, observed server-generated UUIDs, then rolled back.

## Harness setup failures retained

Three setup failures are preserved with the evidence: the initial preflight could not access the Docker socket without escalation and started no container; the first PostgreSQL migration invocation skipped required init jobs, though the base chain completed, then recovered by standard init and extension-only continuation; and the first MySQL migration attempt hit 1045 before DDL, with a read-only table count of zero, then recovered after aligning the temporary synthetic credentials. The separate MySQL omitted-ID ORM failure below is a real runtime finding, not a harness setup failure.

## ORM results

With versions-only source overlay, omitted-ID synthetic ORM insert/read passed on PostgreSQL. It initially failed on MySQL with `FlushError` for `ApiTokenMoneyExtend` NULL identity key; the transaction rolled back. A supplemental explicit-ID ORM check passed but does not count as omitted-ID acceptance.

Sol's exact-hash model snapshot was then mounted read-only: `account_money_extend.py` SHA-256 `15fc8540874015d21b4ecd2df6d5d5c815805bc0bcb7e7ce8d83f3506707c714`; `api_token_money_extend.py` SHA-256 `c4fbac65df991a52ab23bfe1c677034b9a70e47c8507e04128fa47663f60cc26`. Without rerunning migrations, omitted-ID synthetic ORM inserts/readbacks passed on both PostgreSQL and MySQL, and all transactions rolled back. See the recorded synthetic IDs in `result.json`.

No user/business rows or generated key contents were read. The only key check reported a boolean. Full per-engine head/default readbacks, synthetic results, sanitized logs, container image IDs, and network/volume labels are listed in `result.json` and the adjacent evidence files.
