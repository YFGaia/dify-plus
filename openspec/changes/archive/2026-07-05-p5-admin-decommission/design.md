# Design: p5-admin-decommission

## Context

admin/（gin-vue-admin）与 Dify 主站共库共 `SECRET_KEY`，其两大管理职能（用户额度、系统集成）已迁 Console 并验证，GVA 侧成冗余双写点。P1 完成批量工作流处置后，主站对 admin 的最后一条运行时依赖是 code 节点执行控制链路：

```
GVA 表 sys_user_global_code (uint user_id)
  → admin SyncExecuteCode（admin/server/service/gaia/account.go:226，授权变更时触发 + 依赖 GVA 侧用户操作）
  → redis 键 control_mail（JSON 邮箱数组，persistent / KeepTTL）
  → api 侧 ExecutionControl.check_code(tenant_id)（api/core/workflow/nodes/code/control_extend.py 及 api/dify_graph/nodes/code/ 副本）
  → purview=True 时 CodeExecutor 切换到 FULL_CODE_EXECUTION_ENDPOINT（sandbox-full，默认 http://full_sandbox:8195）
```

**关键现状（本设计的修正点）**：读侧链路当前已断——`check_code` 无任何调用方。原始接线（commit b5aa970766）在 `code_node.py` 中调用 `ExecutionControl().check_code(tenant_id)` 并把 `purview` 传给 `CodeExecutor`；1.13.3 合并后 code 节点重构为 `WorkflowCodeExecutor` protocol（`api/dify_graph/nodes/code/code_node.py`），`api/core/workflow/node_factory.py` 的 `DefaultWorkflowCodeExecutor.execute` 调用 `CodeExecutor.execute_workflow_code_template` 时未传 `purview`（默认 `False`）。`CodeExecutor.execute_code` 的 `purview` 参数与 sandbox-full 切换逻辑（`api/core/helper/code_executor/code_executor.py:69-79`）完好。

其余上下文：

- `register_extend.py` 暴露 `/console/api/admin_register_user`，用 `jwt.decode(..., options={"verify_signature": False})` 解析 GVA JWT 并比对 `AuthorityId == ADMIN_GROUP_ID`（默认 888）——签名不校验，仅靠 CSRF 白名单（`api/libs/token.py:26`）+ `@login_required` 松散防护，是明确攻击面。
- compose 中 admin-server 的 `JWT_SIGNING_KEY: ${SECRET_KEY}`，admin 以共享密钥伪造 Dify 兼容 JWT 调 Console API（注册用户、批量工作流回调等）。
- D3 待确认项：`/gaia/proxy/*`（模型供应商网关，JWT 鉴权）、`/gaia/forward/proxy/*`（公开转发入口，forwarding token + ding_id 鉴权，计费写 `account_money_extend.used_quota`）、app-version 公开端点 `GET /latest`、`GET /releases`（可能被桌面客户端轮询）。
- admin cron 每分钟执行 `SyncUser()`（GVA `sys_users` ↔ Dify `accounts` 同步）+ `SyncUserStatus()`，随服务消亡，无需替代（`sys_users` 表本身将被 DROP）。

约束：本 change 是 openspec 规划序列 P5，前置 P1（批量工作流处置）必须完成；与 P2/P3（上游合并）无强顺序但清理动作建议在合并稳定后执行；DROP 表与 `SECRET_KEY` 轮换不可逆。

## Goals / Non-Goals

**Goals:**

1. code 节点执行控制迁 Console：新写入方（管理端点维护 redis `control_mail`）+ 恢复读侧接线，功能等价（配置 → redis → 节点行为）。
2. 存量 `sys_user_global_code` 数据一次性迁移（uint user_id → email 名单 → 新表 + redis）。
3. 切流验证：GVA 侧额度/系统集成 UI 停用，观察一个计费周期确认 Console 写入独占。
4. D3 项处置：确认外部依赖后直接下线（含 2-4 周观察期）或单列后续 change。
5. 一次性清理：api/web/部署/数据库/仓库五域清除 admin 资产，全文无 `gaia`/`admin_register_user`/`ADMIN_GROUP_ID`/`NEXT_PUBLIC_ADMIN_API_URL` 残留引用。
6. 安全收尾：轮换 `SECRET_KEY`，消除共享密钥信任。

**Non-Goals:**

- 批量工作流的迁移或删除（P1 职责；本 change 仅验证其完成状态）。
- Dashboard 报表、租户目录等「仅 GVA 使用」能力向 Console 的迁移（线路图 Phase 5.5 后续独立项目）。
- 模型供应商网关/应用版本管理的替代实现（若 D3 确认在用，单列 change）。
- system-manage-extend 既有页面的 contract 化规范改造（P6 职责；但本 change**新增**的前端块直接按 contract + TanStack Query 规范写，不再欠新账）。
- 上游合并冲突处理（P2/P3 职责）。

## Architecture Assessment

### Existing Design Reuse

- **后端管理端点**：复用 `api/controllers/console/system_manage_extend.py` 的装饰器栈（`@setup_required` + `@login_required` + `@account_initialization_required` + `@system_admin_required_extend`）与 `services/system_manage_extend.py` 的 service 分层，新增 resource 类与 service 方法即可，无新框架。
- **数据库迁移**：复用 `api/migrations_extend/` 独立 Alembic 链（已有 15 个版本先例），新增配置小表迁移 + GVA 表清理迁移。
- **前端挂载**：复用 `web/app/(commonLayout)/system-manage-extend/layout.tsx` 的 `menuItems` 挂载模式加第三个菜单项；数据层遵循 web/AGENTS.md 强制的 contract + TanStack Query 规范（`web/contract/console/` + `consoleQuery`）。
- **redis 访问**：api 侧复用 `extensions.ext_redis.redis_client`，与现有读侧同一客户端。
- **读侧接线**：复用现存 `CodeExecutor.execute_code(purview, ...)` 的 sandbox-full 切换与 `FULL_CODE_EXECUTION_ENDPOINT` 配置（`api/configs/extend/__init__.py:60`），不新增执行面。

### Boundaries and Ownership

- **配置数据所有权**：新表 `code_execution_control_extend` 由 api 侧（`migrations_extend` + `models/`）独占拥有；DB 为 source of truth，redis `control_mail` 是**投影缓存**——每次配置写操作后全量重建 redis 键，DB 与 redis 不一致时以 DB 为准（提供重建入口）。
- **写路径**：仅 Console 管理 API（service 层）写 `control_mail`；迁移完成后全仓不得有第二个写入方。
- **读路径（热路径）**：code 节点执行时经 `ExecutionControl.check_code` 读 redis，不查配置表（保持原有读性能语义）；`check_code` 内部的 owner 校验查询（`accounts` join `tenant_account_joins`）维持不变。
- **失败语义**：redis 键缺失或解析失败 → `purview=False`（回退普通 sandbox，安全默认）；管理 API 写 redis 失败 → 返回 500 且事务回滚，不允许 DB/redis 半写（先写 DB commit，再写 redis，redis 失败记 error 日志并在响应中提示「已保存但缓存同步失败，请重试或手动重建」——见 Decisions D-2）。
- **清理动作所有权**：DROP 表迁移属 `migrations_extend`；compose/部署清理属 docker 域；`SECRET_KEY` 轮换属运维操作（documented runbook，不入代码）。

### Options and Rationale

见 Decisions。核心取舍：新建小表 vs 复用 `system_integration_extend`；redis 投影 vs 直接查库；清理一步到位 vs 停路由观察期。

### Quality Attributes

- **安全**：删除 `admin_register_user`（未验签 JWT 的注册入口）与 CSRF 豁免；轮换 `SECRET_KEY` 使 admin 曾持有的签名能力作废；`control_mail` 管理入口受 `@system_admin_required_extend` 保护。
- **可靠性**：DROP 表前 `pg_dump` 备份导出；清理迁移的 `downgrade` 明确标注不可恢复（`raise NotImplementedError`），依赖备份回滚。
- **性能**：读侧保持 redis 单键读，无新增热路径开销；管理写路径低频，无性能要求。
- **可观测性**：管理 API 写操作记 info 日志（操作者、名单变化）；redis 同步失败记 error；切流观察期以 SQL 巡检脚本核对写入来源。
- **可测试性**：`check_code` 读侧与 service 写侧各配单测；功能等价验证提供手工验收路径（配置名单 → 跑含 code 节点的 workflow → 断言命中 sandbox-full 端点）。
- **可部署性**：compose 删除两个服务后 `docker compose config` 校验 + 全栈起停验证；`SECRET_KEY` 轮换 runbook 写明重启顺序与影响。

### Complexity and Exceptions

新增项仅一张单表 + 一组 CRUD 端点 + 一个前端管理块，属最小增量；未引入新依赖、新服务、新协议。例外说明：`api/dify_graph/nodes/code/control_extend.py` 与 `api/core/workflow/nodes/code/control_extend.py` 双副本是上游拆包过渡产物，本 change 收敛为单一实现（`dify_graph` 侧为准，`core` 侧副本删除或改为 re-export，视 P2/P3 合并进度取其一，任务中落为验证项）。

## Decisions

### D-1 配置存储：新建小表，不复用 `system_integration_extend`

- **选择**：`migrations_extend` 新增 `code_execution_control_extend` 表：`id`（PK）、`email`（unique, not null）、`created_by`（account id）、`created_at`。开关语义用「表空 = 功能关闭」即可，无需单独开关字段（redis 键为空数组或不存在时 `check_code` 返回 False，与现状一致）。
- **替代方案**：(a) 复用 `system_integration_extend`——其 schema（corp_id/app_key/app_secret/config）面向 OAuth 凭据，塞 JSON 名单进 `config` 字段语义扭曲、无法对 email 建唯一约束；(b) 只存 redis 不落库——redis 非持久保证（misconfiguration/flush 即丢失授权名单），且无审计字段。
- **理由**：名单是行级数据（增删查、审计、唯一约束），独立小表最贴合；一张表的成本远低于语义扭曲的复用。

### D-2 redis 投影：DB 为 source of truth，写后全量重建

- **选择**：管理 API 每次写操作（增/删邮箱）在 DB 事务 commit 后，从 DB 全量查名单并 `SET control_mail <json array>`（persistent，与现状 KeepTTL 语义一致）；另提供幂等的「重建缓存」service 方法供迁移脚本与故障恢复复用。
- **替代方案**：(a) api 读侧直接查库（D5 的另一选项）——每次 code 节点执行多一次 DB 查询，且 `check_code` 在 workflow 热路径上；(b) 增量更新 redis——全量名单量级极小（人工维护的邮箱名单），增量徒增复杂度。
- **理由**：采纳线路图 D5 建议「保持 redis 读性能，管理入口统一」；全量重建使 DB/redis 收敛简单可靠。

### D-3 读侧接线恢复：在 `DefaultWorkflowCodeExecutor` 注入 tenant_id

- **选择**：`api/core/workflow/node_factory.py` 中 `DifyNodeFactory` 实例化 `DefaultWorkflowCodeExecutor` 时传入 `self._dify_context.tenant_id`；`execute` 内调用 `ExecutionControl().check_code(tenant_id)` 得 `purview` 并传给 `CodeExecutor.execute_workflow_code_template(..., purview=purview)`。Jinja2 模板渲染路径（`DefaultLLMTemplateRenderer`）不接 purview（原始实现也仅覆盖 code 节点）。
- **替代方案**：(a) 在 `CodeNode._run` 内调用——需改上游节点类，合并冲突面更大；(b) 在 `CodeExecutor` 内部查——helper 层无 tenant 上下文，破坏分层。
- **理由**：`node_factory.py` 是 fork 已有侵入点（挂点注册表在册），`DefaultWorkflowCodeExecutor` 是 protocol 实现处，tenant 上下文现成，改动面最小。
- **附带修缮**：`check_code` 的裸 `except:` 改为精确异常（`json.JSONDecodeError`、redis/DB 异常分开），并按 api/AGENTS.md 补 docstring 与类型标注。

### D-4 存量数据迁移：一次性脚本经 email 映射

- **选择**：迁移脚本（Alembic data migration 或 `flask` 命令，任务阶段定）执行 `SELECT email FROM sys_users WHERE id IN (SELECT user_id FROM sys_user_global_code)`，写入新表并重建 redis。在 DROP `sys_user_global_code` **之前**执行并验证行数一致。
- **替代方案**：不迁移、要求管理员在新 UI 重录——名单可能承载生产授权，静默丢失有业务风险。
- **理由**：迁移成本一条 SQL，收益是零感知切换。

### D-5 D3 待确认项处置：条件分支 + 停路由观察期

- **选择**：先取证（admin-server 访问日志 / nginx 日志 / 业务访谈，覆盖 `/gaia/proxy/*`、`/gaia/forward/proxy/*`、`/latest`、`/releases`）。无外部依赖 → 进入下线流程：**先停路由**（compose 摘除端口映射或反代摘除转发）观察 2-4 周，无异常反馈再随 admin 整体删除；有依赖 → 该模块单列后续 change，admin 其余部分照常废弃，admin-server 保留至替代方案上线（此时本 change 的「删除 admin/ 目录」「DROP 全部 GVA 表」两项相应延后，其余清理不受影响）。
- **理由**：外部客户端（钉钉转发、桌面客户端）故障反馈链路长，观察期是低成本保险；条件分支避免 D3 阻塞主体。

### D-6 SECRET_KEY 轮换：清理完成后的独立运维窗口

- **选择**：所有清理项完成（特别是 `register_extend.py` 删除、admin 服务下线）后，安排维护窗口执行：修改 `docker/.env` 的 `SECRET_KEY` → 按序重启（api → worker/worker_beat → web；admin 服务此时已不存在）→ 验证登录与 API 调用。影响面：全员 Console session 失效需重新登录；已发放的 console JWT 全部失效。注意 `service_api` 的 app token（`app-` 前缀随机串，存 DB 非 JWT 签名）**不受影响**；`system_integration_extend.app_secret` 用 Blowfish + `SECRET_KEY` 加密（`api/models/system_extend.py:49`），轮换后需重新保存钉钉/OAuth2 的 secret 配置（runbook 必列项）。
- **替代方案**：不轮换——admin 历史镜像/配置泄露即可伪造任意用户 JWT，不可接受。
- **理由**：密钥曾跨服务共享即视为暴露；轮换成本一次重登录，收益是信任边界重建。

### D-7 清理顺序：验证关卡前置，不可逆操作殿后

固定顺序：迁移项开发上线 → 切流验证（一个计费周期）→ 可逆清理（代码/compose/文档，可并行）→ 人工确认 → 不可逆操作（DROP 表 → 删 `admin/` 目录 → 轮换 `SECRET_KEY`，顺序执行且各自留备份/tag）。

## Risks / Trade-offs

- [读侧接线恢复改变现网行为：此前 purview 恒为 False，恢复后名单内租户 code 节点走 sandbox-full] -> 上线前确认 `full_sandbox` 服务在目标环境可用（compose 中 sandbox-full 服务健康）；灰度先配置空名单（行为不变），再逐步加人。
- [DROP GVA 表误伤：某张 `sys_*`/`exa_*` 表仍被未知脚本引用] -> DROP 前全仓 `rg` 验证 + `pg_dump` 备份导出；清理迁移逐表列名单（不用通配），执行前人工核对。
- [D3 取证不充分，下线后外部客户端故障] -> 停路由观察 2-4 周 + 保留快速回滚路径（compose 恢复服务定义即可，镜像不删）；观察期内 admin-server 日志留存。
- [SECRET_KEY 轮换遗漏派生用途] -> runbook 全仓搜索 `SECRET_KEY` 引用清单化（JWT 签发、`system_integration_extend` 的 Blowfish 加密、reset token 等），逐项写明轮换后动作；窗口内验证登录、SSO、钉钉配置读取。
- [P1 未完成即执行本 change 清理，`web-extend.ts`/`sys_users` 引用残留导致构建或报表失败] -> 任务首项设置 P1 完成验证关卡（全仓无 `/admin/gaia/workflow/batch` 调用、`batch_workflow_statistics_service.py` 无 `sys_users`），未通过则阻塞。
- [双副本 control_extend.py 只改一处，另一处成僵尸] -> 任务含收敛验证：`rg 'control_mail'` 结果只剩单一读实现 + Console 写入方。
- [切流观察期内 GVA 侧仍有人误操作写入] -> 停用 GVA 入口采取「摘除菜单/权限 + 观察日志」双保险；观察 SQL 巡检 `account_money_extend`/`system_integration_extend` 的更新时间与来源。

## Migration Plan

1. **阶段 A（迁移开发，可与 P2/P3 并行）**：新表迁移 + service/controller + 前端管理块 + 读侧接线 + 存量迁移脚本；上线后用空名单验证行为不变，再导入存量名单验证 sandbox-full 命中。
2. **阶段 B（切流）**：停用 GVA 侧额度/系统集成/全局代码授权入口；admin cron 保持运行（其 `SyncExecuteCode` 因授权入口停用不再变更 redis，Console 写入方接管）；观察一个计费周期。
3. **阶段 C（可逆清理）**：切流放行后，api/web/部署/文档四域并行清理；`docker compose up` 全栈健康验证；打 tag `pre-admin-removal` 留档。
4. **阶段 D（不可逆，人工逐项确认）**：`pg_dump` 导出 GVA 表 → DROP 迁移 → 删除 `admin/` 目录提交 → `SECRET_KEY` 轮换窗口。
5. **回滚策略**：阶段 A/B 可直接恢复 GVA 入口；阶段 C 靠 git revert + compose 恢复；阶段 D 靠备份导入（表）、git 历史（目录）；`SECRET_KEY` 轮换原则上不回滚（回滚等于恢复暴露密钥），仅在轮换导致故障时以再次轮换修复。

## Open Questions

1. D3 取证结论：`/gaia/proxy/*`、`/gaia/forward/proxy/*` 是否有外部 OpenAI 兼容客户端/钉钉转发在用？app-version `/latest`、`/releases` 是否有桌面客户端轮询？（需访问日志或业务确认；影响 admin-server 最终删除时点）
2. `SECRET_KEY` 轮换窗口安排在哪个时间？（全员重新登录，需业务方确认；线路图开放问题 5）
3. 切流观察的「一个计费周期」取自然月还是滚动 30 天？（建议自然月，与月度额度重置对齐）
4. `api/core/workflow/nodes/code/control_extend.py` 与 `dify_graph` 副本的收敛方向，取决于 P2/P3 合并后 code 节点的最终归属包——任务中以验证项兜底，执行时按当时代码状态定。
