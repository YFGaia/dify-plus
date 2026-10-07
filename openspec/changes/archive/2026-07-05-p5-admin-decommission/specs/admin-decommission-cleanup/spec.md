# admin-decommission-cleanup

admin 资产一次性清理与安全收尾要求：api/web/部署/数据库/仓库五域清理、`SECRET_KEY` 轮换、无残留引用验证与全栈健康验证。全部清理 MUST 在 `admin-decommission-cutover` 放行后执行；不可逆操作（DROP 表、删目录、轮换密钥）MUST 经人工确认并留备份。

## ADDED Requirements

### Requirement: api 侧 admin 专用入口清理

系统 SHALL 删除 `api/controllers/console/auth/register_extend.py`（端点 `/console/api/admin_register_user`，**BREAKING**）、`api/libs/token.py` 中该端点的 CSRF 白名单条目，以及 `api/configs/extend/__init__.py` 的 `ADMIN_GROUP_ID` 配置与其全部引用（含 compose 环境变量透传）。

#### Scenario: 注册端点消失

- **WHEN** 请求 `POST /console/api/admin_register_user`
- **THEN** 返回 404

#### Scenario: 配置无残留

- **WHEN** 执行 `rg 'ADMIN_GROUP_ID|admin_register_user'`（排除 docs 与 openspec）
- **THEN** 无任何命中，且 `uv run --project api python -c "from app_factory import create_app"` 通过

### Requirement: web 侧构建配置清理

系统 SHALL 删除 `web/next.config.ts` 中 `/admin` 与 `/admin/:path*` 的 dev rewrite 条目、`web/docker/entrypoint.sh` 中的死变量 `NEXT_PUBLIC_ADMIN_API_URL` 导出，并确认 `web/service/web-extend.ts` 已随 P1 处理（文件删除或无 GVA 调用）。

#### Scenario: 前端无 admin 残留

- **WHEN** 执行 `rg 'NEXT_PUBLIC_ADMIN_API_URL|/admin' web/next.config.ts web/docker/ web/service/`
- **THEN** 无 admin 相关命中，且 `pnpm build` 通过

### Requirement: 部署资产清理

系统 SHALL 从 `docker/docker-compose.dify-plus.yaml` 删除 `admin-web`（:8081）与 `admin-server`（:8888）服务定义（含 storage 只读挂载 `./volumes/app/storage:/app/storage:ro`），删除 `docker/admin-server/config.docker.yaml` 与 `admin/deploy/*` 部署资产。

#### Scenario: compose 校验与全栈健康

- **WHEN** 执行 `docker compose -f docker/docker-compose.dify-plus.yaml config` 及全栈 `up`
- **THEN** 配置校验通过，无 admin-web/admin-server 服务，其余服务全部健康（api、worker、web、db、redis、sandbox 等）

### Requirement: GVA 数据库表清理

系统 SHALL 在 `migrations_extend` 新增一次性清理迁移，DROP GVA 框架表：`sys_*`（含 `sys_users`、`sys_apis`、`sys_authorities` 等十余张，逐表列名单不用通配）、`casbin_rule`、`exa_*`，以及存量迁移完成后的 `sys_user_global_code`。DROP 前 MUST 用 `pg_dump` 导出备份并归档；迁移的 `downgrade` MUST 明确标注不可恢复（依赖备份回滚）。gaia 业务表（`batch_workflow*` 等）按 P1/D3 决策单独处置，不混入本迁移。

#### Scenario: 备份先行

- **WHEN** 执行清理迁移前
- **THEN** 存在包含全部待 DROP 表的 `pg_dump` 备份文件且经人工确认

#### Scenario: 表清理完成

- **WHEN** 执行 `flask extend_db upgrade` 后查询数据库
- **THEN** 名单内的 `sys_*`、`casbin_rule`、`exa_*`、`sys_user_global_code` 表全部不存在，Dify 自身表不受影响

### Requirement: 仓库与文档清理

系统 SHALL 整体删除 `admin/` 目录（git 历史保留，删除前打 tag `pre-admin-removal`），并更新 `docs/dify-plus/` 相关文档：`admin迁移状态总表.md` 标记完结、总线路图 Phase 4 状态回填。

#### Scenario: 目录删除留档

- **WHEN** 删除提交合入后
- **THEN** 仓库无 `admin/` 目录，tag `pre-admin-removal` 存在，`git log -- admin/` 可追溯历史

### Requirement: 全文无残留引用验证

清理完成后，系统 MUST 通过残留扫描：`rg 'gaia|NEXT_PUBLIC_ADMIN_API_URL|admin_register_user|ADMIN_GROUP_ID|control_mail'`（排除 docs、openspec、git 历史）中，`gaia`/`NEXT_PUBLIC_ADMIN_API_URL`/`admin_register_user`/`ADMIN_GROUP_ID` 零命中，`control_mail` 只命中新链路（单一读实现 + Console 写入 service 及其测试）。

#### Scenario: 残留扫描通过

- **WHEN** 在清理完成的工作区执行上述扫描
- **THEN** 结果符合零命中/白名单要求，作为清理验收证据归档

### Requirement: SECRET_KEY 轮换

所有清理项完成后，系统 SHALL 在计划维护窗口内轮换 `SECRET_KEY`（**BREAKING**：全员 Console session 失效、已发 console JWT 作废）。轮换 runbook MUST 包含：(1) `docker/.env` 修改 `SECRET_KEY`；(2) 重启顺序 api → worker/worker_beat → web；(3) 轮换后动作清单——重新保存钉钉/OAuth2 的 secret 配置（`system_integration_extend.app_secret` 为 Blowfish + `SECRET_KEY` 加密，旧密文不可解）；(4) 验证项——登录、SSO（钉钉/OAuth2）、service_api app token 调用（应不受影响）。轮换 MUST 经人工确认后执行，原则上不回滚。

#### Scenario: 轮换后旧 token 失效

- **WHEN** 轮换完成后使用轮换前签发的 console JWT 调用 Console API
- **THEN** 请求被拒绝（401），用户重新登录后恢复正常

#### Scenario: service_api 不受影响

- **WHEN** 轮换完成后外部系统使用既有 `app-` 前缀 API token 调用 service_api
- **THEN** 调用正常（该 token 存数据库，非 JWT 签名派生）

#### Scenario: 集成配置重新保存

- **WHEN** 轮换完成后按 runbook 重新保存钉钉与 OAuth2 secret
- **THEN** SSO 登录与钉钉配置读取恢复正常
