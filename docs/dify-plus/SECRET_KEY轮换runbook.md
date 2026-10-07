# SECRET_KEY 轮换 Runbook

> 所属 change：`openspec/changes/archive/2026-07-05-p5-admin-decommission`（任务 11.1，对应 design.md D-6 决策）
> 性质：**不可逆运维操作**（BREAKING），MUST 经人工确认后在计划维护窗口内执行，原则上不回滚。

## 1. 背景与目标

admin 管理后台（gin-vue-admin）曾与 Dify api 共享 `SECRET_KEY`：compose 中 admin-server 以 `JWT_SIGNING_KEY: ${SECRET_KEY}` 注入同一密钥（`docker/docker-compose.dify-plus.yaml:1704-1706`，随 admin 下线一并删除），可用共享密钥签发 Dify 兼容 JWT 调用 Console API（注册用户、批量工作流回调等）。密钥一旦跨服务共享即视为暴露——admin 的历史镜像、配置文件、部署备份中任何一处泄露，攻击者即可伪造任意用户的 console JWT。

admin-server/admin-web 下线后，必须轮换 `SECRET_KEY`，作废其曾持有的签名能力，重建信任边界。

**影响面概述**（详见第 2 节清单）：

- 全员 Console session 失效，需重新登录；已签发的 console/webapp JWT 全部作废。
- `system_integration_extend.app_secret`（钉钉/OAuth2 的 secret）为 Blowfish + `SECRET_KEY` 加密，旧密文不可解，需管理员重新保存。
- 历史文件预览签名 URL 失效（本身有时效，自然重签）。
- `service_api` 的 `app-` 前缀 token **不受影响**（随机串存 DB，非 JWT 派生）。
- reset password / 邮箱验证码 token **不受影响**（随机 UUID 存 redis，非密钥派生）。

## 2. SECRET_KEY 派生用途清单

以下清单来自全仓代码调研（`rg -n 'SECRET_KEY' api/ --type py`，逐处确认），**轮换前请按当时代码状态复核一遍**：

| # | 用途 | 代码位置 | 轮换后影响 | 需要的后续动作 |
|---|------|----------|-----------|---------------|
| 1 | Console / WebApp JWT 签发与校验（`PassportService`，HS256） | `api/libs/passport.py`；签发方：`api/services/account_service.py`（console access token）、`api/services/webapp_auth_service.py`、`api/controllers/web/passport.py`（webapp end-user token）；校验方：`api/extensions/ext_login.py`、`api/libs/token.py`、`api/controllers/web/wraps.py` 等。旧二开 `/console/api/passport-extend` 已于 1.16.0 Beta 退役 | 所有已签发 JWT 立即失效，Console/WebApp 请求返回 401 | 无需操作，全员重新登录即可（refresh_token 为随机串存 redis，刷新后即换发新密钥签名的 access token） |
| 2 | 二开转发鉴权装饰器 `repost_login_required` 的 JWT 校验 | `api/libs/login_extend.py:30` | 同上，旧 token 401 | 同上，重新登录 |
| 3 | login-config JWT（CVE-2025-63387 防护，1h 过期） | `api/controllers/console/feature.py:25,33` | 旧 token 校验失败 | 无需操作，自动重新签发 |
| 4 | Flask app `secret_key`（session cookie 签名） | `api/extensions/ext_set_secretkey.py:8-13`、`api/configs/secret_key.py` | 旧 session cookie 失效 | 无需操作 |
| 5 | **Blowfish 加密 `system_integration_extend.app_secret`（钉钉/OAuth2 secret，二开）** | 解密：`api/models/system_extend.py:49`（`decodeSecret`）；加解密：`api/services/system_manage_extend.py:91,132,154,183` | **旧密文不可解**，钉钉/OAuth2 SSO 登录与配置读取报错 | **必做**：管理员在 Console「系统集成」页面重新录入并保存钉钉与 OAuth2 的 secret 配置 |
| 6 | 文件预览 URL 的 HMAC-SHA256 签名 | `api/core/tools/signature.py:12`、`api/core/tools/tool_file_manager.py:55,70`、`api/core/datasource/datasource_file_manager.py:39,54`、`api/models/dataset.py:952,969,988,1026`、`api/core/app/workflow/file_runtime.py:130-136` | 历史签名 URL 校验失败 | 无需操作，签名 URL 本身有时效（`FILES_ACCESS_TIMEOUT`），页面刷新后按需重签 |
| 7 | 系统级工具/触发器 OAuth client 参数的 AES 加密（key = SHA-256(`SECRET_KEY`)） | 加密入口：`api/core/tools/utils/system_encryption.py:41-44` + `api/commands/plugin.py:47,97`（`setup-system-tool-oauth-client` / `setup-system-trigger-oauth-client` 命令）；解密：`api/services/tools/builtin_tools_manage_service.py:523`、`api/services/trigger/trigger_provider_service.py:638` | 旧 `encrypted_oauth_params` 不可解，对应插件 OAuth 授权流程报错 | **条件必做**：若曾用上述 flask 命令配置过系统级 OAuth client，需用新密钥重新执行命令录入 |
| 8 | （待删除项）admin 注册端点解析 GVA JWT | `api/controllers/console/auth/register_extend.py:42`（`verify_signature=False`） | 不适用 | 前置条件已要求该文件删除，轮换前确认不存在 |

**确认不受影响的项**（易误判，特此列明）：

- `service_api` 鉴权：`app-` 前缀 API token 为随机串存 `api_tokens` 表，与 `SECRET_KEY` 无关。
- reset password / 邮箱验证码 / 账号删除等 token：`TokenManager`（`api/libs/helper.py:456-509`）用随机 UUID 存 redis，非密钥派生。
- refresh_token：随机 64 位串存 redis（`api/services/account_service.py:1740`），不失效；用户下次刷新即获得新密钥签名的 access token。
- `BILLING_API_SECRET_KEY`、`ENTERPRISE_API_SECRET_KEY`、各对象存储的 `*_SECRET_KEY`、`VIKINGDB_SECRET_KEY`、`PLUGIN_DIFY_INNER_API_KEY` 等：均为独立密钥，与本次轮换无关。

## 3. 前置条件

执行轮换前逐项人工确认（任一不满足则中止）：

- [ ] admin-server 与 admin-web 服务已从 `docker/docker-compose.dify-plus.yaml` 删除并下线（`docker compose ps` 无二者）。
- [ ] `api/controllers/console/auth/register_extend.py` 已删除，`api/libs/token.py` 中对应 CSRF 白名单条目已删除（`rg 'admin_register_user' api/` 零命中）。
- [ ] P5 其余清理项完成（残留扫描通过，见 spec「全文无残留引用验证」）。
- [ ] 维护窗口已向全员公告（模板见第 6 节），并预留至少 30 分钟窗口。
- [ ] 已通知系统管理员：轮换后需重新录入钉钉/OAuth2 secret（提前准备好明文 secret，避免窗口内临时查找）。
- [ ] 确认 `docker/.env` 中 `SECRET_KEY` 为显式非空值（若为空，api 会走 `api/configs/secret_key.py` 的自动生成逻辑，密钥落在存储卷 `.dify_secret_key` 文件中，本 runbook 的修改方式不生效）。注意 compose 文件对 `SECRET_KEY` 有硬编码默认值（`docker/docker-compose.dify-plus.yaml:30`），生产环境必须在 `.env` 中显式覆盖。

## 4. 操作步骤

以下命令均在部署机的仓库 `docker/` 目录下执行。

**Step 1：生成新密钥**

```bash
openssl rand -base64 42
```

**Step 2：修改 `docker/.env`**

将 `SECRET_KEY=` 行替换为新值（旧值先抄录到密钥管理系统留档，供故障排查比对，不得再投入使用）：

```bash
# docker/.env
SECRET_KEY=<上一步生成的新值>
```

**Step 3：按序重启服务（api → worker/worker_beat → web）**

注意：`docker compose restart` **不会**重新读取 `.env`，必须用 `up -d --force-recreate` 重建容器。

```bash
cd docker

# 1) api
docker compose -f docker-compose.dify-plus.yaml up -d --force-recreate api

# 等待 api 健康后再继续
docker compose -f docker-compose.dify-plus.yaml ps api

# 2) worker 与 worker_beat
docker compose -f docker-compose.dify-plus.yaml up -d --force-recreate worker worker_beat

# 3) web
docker compose -f docker-compose.dify-plus.yaml up -d --force-recreate web

# 全栈状态确认
docker compose -f docker-compose.dify-plus.yaml ps
```

**Step 4：重新录入派生密文配置**

- 管理员登录 Console → 系统管理（system-manage-extend）→ 系统集成，重新保存钉钉与 OAuth2 的 secret（清单第 5 项）。
- 若曾配置系统级工具/触发器 OAuth client（清单第 7 项），重新执行：

```bash
docker compose -f docker-compose.dify-plus.yaml exec api flask setup-system-tool-oauth-client --provider <provider> --client-params '<json>'
# 触发器同理：flask setup-system-trigger-oauth-client
```

## 5. 验证清单

窗口内逐项验证并记录结果：

- [ ] **旧 session 失效**：用轮换前已登录的浏览器访问 Console，任意 API 返回 401，被重定向到登录页（对应 spec Scenario「轮换后旧 token 失效」）。
- [ ] **重新登录成功**：邮箱密码登录 Console 正常，工作区、应用列表可访问。
- [ ] **SSO 登录**：先完成第 4 节 Step 4 重新保存 secret，再验证钉钉扫码登录与 OAuth2 登录各走通一次；Console「系统集成」页配置可正常读取回显。
- [ ] **service_api 不受影响**：用既有 `app-` 前缀 token 调用任一应用的 service_api（如 `POST /v1/chat-messages`），返回正常（对应 spec Scenario「service_api 不受影响」）。
- [ ] **reset password 流程**：登录页发起忘记密码，收到邮件、凭 token 重置成功（该 token 存 redis 非密钥派生，预期不受影响，验证以兜底）。
- [ ] **WebApp 访问**：任一已发布 WebApp 重新进入并对话正常（end-user token 重新签发）。
- [ ] **文件预览**：打开含图片/文件的历史对话或知识库文档，预览正常（签名按需重签）。
- [ ] api / worker / worker_beat / web 容器日志无 `SECRET_KEY` 相关报错。

## 6. 影响公告模板

> **【维护公告】Dify 平台安全维护：全员需重新登录**
>
> 各位同事：
>
> 为完成 admin 管理后台下线后的安全收尾，平台将于 **{YYYY-MM-DD HH:mm ~ HH:mm}** 进行密钥轮换维护，预计影响时长 {N} 分钟。
>
> **对所有用户的影响：**
> - 维护期间平台短暂不可用（服务滚动重启）。
> - 维护完成后，所有已登录会话失效，**需重新登录** Console 与 WebApp。
> - 通过 API（`app-` 开头的 token）调用应用的外部系统**不受影响**。
>
> **对系统管理员的额外动作：**
> - 维护完成后需在 Console「系统集成」页面**重新录入并保存钉钉与 OAuth2 的 secret 配置**，在此之前钉钉/OAuth2 SSO 登录不可用（请优先用邮箱密码登录）。
>
> 如维护后遇到登录或使用异常，请联系 {运维联系人}。

## 7. 回滚说明

**原则上不回滚。** 回滚（改回旧 `SECRET_KEY`）等于重新启用已暴露的密钥，直接违背本次轮换的目的（design.md D-6 与 Migration Plan 第 5 条）。

- 若轮换后出现故障，先按第 5 节验证清单定位：绝大多数问题是「派生密文未重新保存」（第 2 节清单第 5、7 项）或「某容器未重建、仍持旧密钥」（用 `docker compose exec <svc> printenv SECRET_KEY` 核对各容器环境变量一致）。
- 若确认新密钥本身有问题（如包含被 shell/compose 转义的特殊字符导致各容器取值不一致），修复方式是**再次轮换**：重新生成一个新密钥，重复第 4 节全部步骤（含重新保存钉钉/OAuth2 secret），而不是回退旧值。
- 旧密钥仅作留档比对用途，任何情况下不得重新写回 `.env`。
