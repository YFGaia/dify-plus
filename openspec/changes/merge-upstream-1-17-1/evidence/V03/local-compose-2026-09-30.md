# Dify-Plus 1.17.1 isolated local Compose rehearsal

**Status:** Supplemental local smoke completed. Full V03 remains blocked and is not accepted by this evidence.

**Date:** 2026-09-30 (Asia/Shanghai)

## Candidate and isolation

- Candidate commit: `970b704e351f8b98d1f0450e5dd50734b5d21e8c`
- Candidate tree: `ccb16e6692f53c68fe13e2ca51036649c707735d`
- Docker Engine: 28.2.2; Compose: v2.36.2-desktop.1; local platform: `linux/arm64`.
- Source was a `git archive` of the exact candidate. The main Compose file SHA-256 was `089311a9810bd98e84e495b49cb061a3b586eec38264503e3c4be45bfd35a072`; the temporary override SHA-256 was `ff461088c1448ff41ee6556dc995dcb200d75c3dc6c464507582cad11f7d4c72`.
- Project: `difyplus-upstream1171-smoke-20260930`. Compose received an explicit zero-byte env file under `env -i`; no project `.env` or real credentials/data were used.
- The services actually started (init jobs, PostgreSQL, Redis, migration, API, Web) had no published host ports or host bind mounts. Their networks were internal; data used only project-scoped named volumes. No workers, beat, plugin daemon, Agent, nginx, or vector database was started.
- Compose `config --quiet` exited 0. It emitted only blank-variable warnings for `CERTBOT_EMAIL` and `CERTBOT_DOMAIN`.

## Candidate images and Web build blocker

- API image built from the candidate `api/Dockerfile`, exit 0. Local image ID: `sha256:3f8404321e4c43939d5001b361c33052d41f122e7e49ef7dfecfd8ee0b953406` (`linux/arm64`). It had no registry `RepoDigest`; this was a local build, not published supply evidence.
- The unmodified Web Dockerfile first failed at npm registry downloads (`ERR_PNPM_META_FETCH_FAIL`). Enabling the repository's existing commented npm mirror in a temporary Dockerfile fetched the locked packages, then pnpm timed out downloading the pinned `node-v24.20.0-linux-arm64-musl.tar.gz` from `unofficial-builds.nodejs.org`.
- Setting `runtime_on_fail=warn` was not a valid workaround: pnpm omitted the locked `node@runtime:24.20.0` specifier, and `--frozen-lockfile` correctly rejected the mismatch.
- A final temporary Dockerfile (SHA-256 `e3fcd57669dbd77ff73849db84f5bb51c838d37e5be8946bbb39f42053fbf2e1`) enabled that npm mirror and added `--no-runtime` to the install command. The candidate base image already contains Node 24.20.0; both `node --version` and `pnpm exec node --version` returned `v24.20.0`. With the lockfile still frozen, pnpm verified its supply-chain policy and installed all 1,360 packages. This adjustment was confined to `/private/tmp`; candidate source and lockfile were unchanged.
- The full Web image then built successfully, exit 0 in 209.7 seconds. Next and Vinext builds both completed; Next's route table included `/system-manage-extend/quota-management`. The build logged non-fatal dynamic-render and bundle-size warnings. Local image ID: `sha256:0c01f449bae4b69862b06b5267192a89fe6490ce85f04f0aacf9ffda2146e628` (`linux/arm64`), with no registry `RepoDigest`.

The temporary `--no-runtime` build path proves a local workaround for this host's runtime-download timeout. It is not a source change or a published release procedure; other builders still need the locked runtime URL or an explicitly reviewed build configuration.

## Compose migration and runtime

- The first migration attempt in the Luna pass used a temporary override that accidentally removed migration dependencies. It exited before DB connection/DNS resolution; no database container was running and no schema was changed. That exact temporary project was cleaned, the override corrected, and the rehearsal rerun.
- On the final pass, PostgreSQL and Redis became healthy. The single migration service ran `flask db upgrade` followed by `flask extend_db upgrade`, then both `current` checks. Container exit was 0 (start `2026-09-30T03:01:57Z`, finish `2026-09-30T03:02:22Z`). Heads were `c3f1a9b2e6d4` and `020_workflow_run_account`.
- API started healthy on its configured `/health` check with restart count 0. Web started and logged Next.js ready with restart count 0.
- An unauthenticated request to the API quota-list route returned `401` JSON, consistent with the protected endpoint being mounted; no quota mutation or account creation was attempted. A request to the Web quota page returned `307 /install` because this was a fresh, uninitialized database. This confirms the Web server handled the route request; it does not test an authenticated quota workflow.

## Dify-Plus quota extension preservation

The exact candidate source and built outputs retain:

- Console routes `/system-manage-extend/quota-management` and `/system-manage-extend/quota-management/set` in `api/controllers/console/system_manage_extend.py`.
- Separate personal account balance fields `total_quota` / `used_quota` in `api/models/account_money_extend.py`.
- Separate API key fields `accumulated_quota`, day/month used quota, and day/month limits in `api/models/api_token_money_extend.py`; cumulative usage is not treated as a lifetime cap.
- The UI route `web/app/(commonLayout)/system-manage-extend/quota-management/page.tsx`.

These source anchors, the Next route table, the API's unauthenticated 401, and the Web route response establish preservation and mounting. They do not replace authenticated owner/admin, quota read/write, usage accounting, or billing reconciliation tests.

## Cleanup and remaining gates

- `docker compose down -v --remove-orphans` exited 0. Post-cleanup checks found zero containers, networks, and volumes with the unique project label. The three temporary smoke image tags were removed after recording their local image IDs.
- A03 remains blocked: no authorization was given to inspect a real deployment or business data, and none was accessed.
- This run used an empty PostgreSQL database only. It did not cover an old-library database copy, destructive-data audit, Agent/backfill data, vector-store migration, a MySQL/production-engine matrix, a registry-published image digest, authenticated business flows, or backup/restore. V03's required checklist items remain open; V04–V06, R02, and all production nodes remain unpassed.

The first-pass Luna report SHA-256 is `e2e7a644cf2d70c7d0b2fbc984c394d8c8ddcd9521e1c2993ae22a71bfe4d2b2` (originally under `/private/tmp/difyplus-upstream1171-smoke-20260930/result.md`).

## Authenticated quota UI self-test and user handoff

On 2026-09-30, a second isolated project, `difyplus-upstream1171-quota-selftest-20260930`, was started from the same exact candidate images on fresh project-scoped named volumes. The initial internal-only network did not publish the application ports, and Web's implicit `HOSTNAME` bound Next to the container address. A temporary `/private/tmp` override set Web `HOSTNAME=0.0.0.0` and attached only API/Web to a dedicated ordinary bridge network; both kept their internal Compose network for database/cache access. API and Web ports were published only on host loopback (`127.0.0.1:25432` and `127.0.0.1:23000`). PostgreSQL and Redis remained on the internal network without host ports. API/Web can make outbound requests on the bridge, which is needed for the user's requested provider-backed LLM test; no provider key has been entered yet.

Host requests returned HTTP 200 from Web `/install` and API `/health` (`status=ok`, version `1.17.1`). In ego-browser TaskSpace 3, the generated one-time setup password opened the administrator form. The account `Quota Smoke Admin` (`quota-selftest@invalid.example`) was created and reached the authenticated home page. The authenticated route `/system-manage-extend/quota-management` listed that account with `used_quota=0.0000 USD`, `total_quota=15.0000 USD`, and balance `15.0000 USD`. The UI edit flow changed the total to `16.0000 USD` and displayed `额度修改成功`; it was then restored to `15.0000 USD`, which was confirmed in the refreshed table.

This establishes authenticated quota read/write behavior against a fresh local database. The app remains running. No real LLM request, production endpoint, or business data was used. At the latest check, the DeepSeek API-key dialog showed only `添加 API 密钥` and no saved entry; a real call and its account/API-key usage readback await the user saving a key in this isolated workspace. This UI self-test is supplemental and does not close V03 tasks 18.1–18.5 or change V03 from `blocked`.

## DeepSeek provider installation and network diagnosis

The first DeepSeek install attempt failed before any provider call. Sanitized API logs showed the expected fallback path: the Plugin Daemon did not yet have the package, so the API tried `download_plugin_pkg()` through its configured SSRF client. That client was configured for `http://ssrf_proxy:3128`, but this selected-service Compose run had omitted `ssrf_proxy`, and its API network override had dropped `ssrf_proxy_network`; Marketplace download retries therefore failed with `Name or service not known` and `MaxRetriesExceededError`. Direct Marketplace access outside the configured SSRF client had previously returned the package, which did not prove the application download path worked.

The candidate Compose source references the private `ccr.ccs.tencentyun.com/yfgaia/dify-plugin-daemon:0.6.10-local` and `ccr.ccs.tencentyun.com/yfgaia/ubuntu-squid:latest` images. The plugin-daemon pull returned `manifest unknown`. For this local rehearsal only, a temporary override selected the matching upstream `langgenius/dify-plugin-daemon:0.6.10-local` image (digest `sha256:34412a22d1e1d6a1c73727fef88cffbcdd9be7a21bf9ad3076b4bf5c1b88fd9b`) and `ubuntu/squid:latest`; it started `ssrf_proxy` on the internal proxy network plus the isolated access bridge, without publishing proxy ports, and reattached only the test API to the proxy network. Repository source was not changed by this workaround.

After the change, the API resolved `ssrf_proxy` to `172.26.0.5`. A request through the configured SSRF client followed the Marketplace redirect and returned HTTP 200 with a ZIP signature; Squid logs recorded successful CONNECTs to `marketplace.dify.ai` and the Cloudflare R2 package host. The official Marketplace plugin `langgenius/deepseek:0.0.24@9e12eef09973667f0eaf4f082af883167e1de88740b22e71e71488d020c3778a` then completed Plugin Daemon install task `01a0f098-87fe-7896-8bda-40c0e26f7fe0`; the daemon logged `Installed model: deepseek`, and the refreshed provider page displayed DeepSeek version `0.0.24`.

The current page still offers `添加 API 密钥` without a saved entry. A fresh read-only check on 2026-09-30 confirmed the installed DeepSeek `0.0.24` provider card, then its configuration panel showed only `添加 API 密钥`; no saved credential entry was visible. The user reports a DeepSeek key is configured, but it is not present in this isolated workspace at this checkpoint. No key form value was read and no provider setting was changed. The user must save the credential in this local workspace before a real LLM and quota-accounting test can run. This package-install success is supplemental only: private-registry provenance and the full V03 gates remain blocked.

While waiting for that handoff, a private, unpublished Chatflow draft named `Quota smoke chat` was created in the isolated workspace (app ID `d7af1af0-d92f-4d78-bbfa-8802de395199`) for one short DeepSeek request. The model selector is still empty; no provider request was sent and the account balance has not been charged. The provider page remains available at `http://127.0.0.1:23000/integrations/model-provider`; the test stack remains running on loopback for the user to configure the credential.
