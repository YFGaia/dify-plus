# Proposal: p5-admin-decommission

## Why

`admin/`（gin-vue-admin 的 Go 管理后台，约 58 个 gaia 端点、~12k 行 Go + ~11.4k 行 Vue）与 Dify 主站共库共密钥，长期存在三类问题：

1. **冗余双写**：用户额度管理与系统集成（钉钉 SSO/OAuth2/邮箱测试/转发 Token）已迁入 Console（`/system-manage-extend/*`，已验证），GVA 侧同功能成为冗余双写点，越晚下线数据口径风险越大。
2. **安全耦合**：admin 与 api 共享 `SECRET_KEY` 互相伪造/接受 JWT（compose 中 `JWT_SIGNING_KEY = SECRET_KEY`），api 侧还为其保留专用注册端点 `/console/api/admin_register_user`（`register_extend.py`，签名校验用 `options={"verify_signature": False}`，仅比对 payload 中的 `AuthorityId`）与 CSRF 白名单豁免——攻击面必须收敛，且废弃后需轮换 `SECRET_KEY`。
3. **维护成本**：每次上游升级都要额外维护一套 Go + Vue 技术栈及其部署资产（admin-web :8081 / admin-server :8888、十余张 GVA 框架表）。

前置依赖已就绪：P1（批量工作流处置）解决前端唯一运行时依赖与 `sys_users` 报表耦合后，admin 仅剩一条主站残留链路——code 节点执行控制的 `control_mail` redis 键只由 admin 的 `SyncExecuteCode`（`admin/server/service/gaia/account.go:226`，数据源 GVA 表 `sys_user_global_code`）写入。本 change 对应总线路图 Phase 4（除 4.1 批量工作流外的全部内容）+ 决策 D3/D5，完成「迁移 → 切流验证 → 清理」三段式废弃。

**调研发现（影响方案）**：`ExecutionControl.check_code`（`api/core/workflow/nodes/code/control_extend.py` 及 `api/dify_graph/nodes/code/` 副本）当前**没有任何调用方**——原始接线（commit b5aa970766 在 `code_node.py` 中调用并把 `purview` 传给 `CodeExecutor` 以切换 `FULL_CODE_EXECUTION_ENDPOINT`/sandbox-full）在 1.13.3 合并中丢失；`CodeExecutor.execute_code` 的 `purview` 参数与 sandbox-full 切换逻辑仍在。因此本 change 除了替换写入方，还必须**恢复读侧接线**才能达成「功能等价」。

## What Changes

- **迁移项 1（决策 D5）：code 节点执行控制迁 Console**
  - 在 `system_manage_extend` 域新增「代码执行控制」管理能力：配置存储（`migrations_extend` 新增小表）+ 管理端点 + 写 redis 键 `control_mail` 逻辑 + system-manage-extend 前端管理块（开关/邮箱名单）。
  - 存量 GVA `sys_user_global_code` 数据（uint user id → 经 `sys_users.email` 映射为邮箱名单）一次性迁移。
  - 恢复 code 节点读侧接线：`check_code` → `purview` → `CodeExecutor` 的 sandbox-full 切换（调研发现该链路已断）。
- **迁移项 2：批量统计脚本脱离 `sys_users`**——`api/services/batch_workflow_statistics_service.py`（854 行）按 P1 决策改造或随批量功能删除；本 change 只做**验证关卡**：确认 P1 已处置完毕、无 `sys_users` 引用残留。
- **迁移项 3（决策 D3，条件分支）**：模型供应商网关/转发代理（`/gaia/proxy/*`、`/gaia/forward/proxy/*`，转发计费写 `account_money_extend.used_quota`）与应用版本管理（公开端点 `/latest`、`/releases`）——先以访问日志/业务访谈确认外部依赖：无依赖则随 admin 直接下线（先停路由观察 2-4 周再删码）；有依赖则单列后续 change（不阻塞本 change 主体）。
- **切流验证**：GVA 侧额度管理/系统集成 UI 停用，观察一个计费周期，确认 Console 侧写入正常（`account_money_extend`、`system_integration_extend` 无 GVA 来源写入）。
- **清理项（一次性，切流验证通过后）**：
  - api 侧：删除 `api/controllers/console/auth/register_extend.py`（**BREAKING**：`/console/api/admin_register_user` 端点消失）+ `api/libs/token.py` CSRF 白名单条目 + `api/configs/extend/__init__.py` 的 `ADMIN_GROUP_ID` 配置及引用。
  - web 侧：`web/next.config.ts` 删除 `/admin` dev rewrite；`web/docker/entrypoint.sh` 删除死变量 `NEXT_PUBLIC_ADMIN_API_URL`；确认 `web/service/web-extend.ts` 已随 P1 处理。
  - 部署侧：`docker/docker-compose.dify-plus.yaml` 删除 `admin-web`（:8081）与 `admin-server`（:8888）服务及 storage 只读挂载；删除 `docker/admin-server/config.docker.yaml`；删除 `admin/deploy/*`。
  - 数据库：**BREAKING** DROP GVA 框架表（`sys_*` 十余张、`casbin_rule`、`exa_*`、迁移完成后的 `sys_user_global_code`）——出 `migrations_extend` 一次性清理迁移，DROP 前先备份导出；admin 的 cron（sys_users 同步）随服务消亡。
  - 仓库：整体删除 `admin/` 目录（git 历史保留）；`docs/dify-plus` 相关文档更新（admin迁移状态总表标记完结）。
- **安全收尾：轮换 `SECRET_KEY`**——**BREAKING**：全员重新登录、已发 token 失效，需安排窗口；design 写明影响面与操作步骤（`.env` 修改、服务重启顺序）。

## Capabilities

### New Capabilities

- `code-execution-control-console`: code 节点执行控制的 Console 管理能力——配置存储、管理端点、redis 键 `control_mail` 写入、前端管理块、存量数据迁移、读侧接线恢复与功能等价验证。
- `admin-decommission-cutover`: admin 废弃的切流验证要求——GVA 冗余双写入口停用、D3 待确认项处置（含观察期）、一个计费周期的写入来源观察与放行判据。
- `admin-decommission-cleanup`: admin 资产一次性清理与安全收尾要求——api/web/部署/数据库/仓库五域清理、`SECRET_KEY` 轮换、全文无残留引用验证、全栈健康验证。

### Modified Capabilities

（无——`openspec/specs/` 当前为空，本 change 不修改既有 capability。）

## Architecture Impact

- **复用现有 fork 模式**：管理端点挂 `api/controllers/console/system_manage_extend.py` 既有装饰器栈（`@setup_required` + `@login_required` + `@account_initialization_required` + `@system_admin_required_extend`）；配置存储走 `api/migrations_extend` 独立 Alembic 链新增小表（不复用 `system_integration_extend`——其 schema 面向 OAuth 凭据，语义不符，理由见 design）；前端在 `web/app/(commonLayout)/system-manage-extend/` layout `menuItems` 加项挂新页，数据层遵循 web/AGENTS.md 强制的 contract + TanStack Query 规范。
- **保持 redis 读路径不变**：code 节点热路径继续读 `control_mail` redis 键（保持读性能），仅替换写入方为 Console 管理 API——D5 决策采纳「Console 管理 API 维护 redis」而非「api 直接查库」。
- **消除跨服务安全基础设施**：删除共享 `SECRET_KEY` 伪造 JWT 的最后接受方（`register_extend.py`）后轮换密钥，admin 与 api 的信任边界彻底消失。
- **排期依赖**：P1（批量工作流处置）必须先完成（前端 GVA 调用与 `sys_users` 报表耦合是其交付物）；迁移项 1 可先行开发；清理项必须在切流验证通过后执行；DROP 表与 `SECRET_KEY` 轮换为不可逆操作，顺序执行并留备份。

## Impact

- **后端**：新增 `migrations_extend` 迁移（配置小表 + 存量迁移 + GVA 表清理）、`system_manage_extend` controller/service 扩展、code 节点读侧接线（`node_factory.py` / `dify_graph/nodes/code/`）；删除 `register_extend.py`、`token.py` 白名单条目、`ADMIN_GROUP_ID` 配置。
- **前端**：`system-manage-extend` 新增管理块 + `web/contract/console/` 契约；`next.config.ts`、`docker/entrypoint.sh` 清理。
- **部署**：`docker/docker-compose.dify-plus.yaml` 移除两个服务；`docker/admin-server/`、`admin/deploy/` 删除；`.env` 的 `SECRET_KEY` 轮换。
- **数据库**：新增 1 张配置小表；DROP `sys_*`、`casbin_rule`、`exa_*`、`sys_user_global_code`（备份先行）。
- **外部系统**：D3 确认项（OpenAI 兼容客户端、钉钉转发、桌面客户端版本拉取）——确认无依赖方可下线，有依赖单列 change。
- **全员用户**：`SECRET_KEY` 轮换导致一次性全员重新登录。
