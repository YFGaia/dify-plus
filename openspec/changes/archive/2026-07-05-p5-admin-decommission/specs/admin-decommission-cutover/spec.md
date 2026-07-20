# admin-decommission-cutover

admin 废弃的切流验证要求：前置依赖关卡、GVA 冗余入口停用、D3 待确认项处置（含观察期）与计费周期观察放行判据。切流验证是清理阶段（`admin-decommission-cleanup`）的前置门。

## ADDED Requirements

### Requirement: 前置依赖验证关卡

切流开始前，系统状态 MUST 满足：(1) P1（批量工作流处置）已完成——全仓无 `/admin/gaia/workflow/batch` 调用、`api/services/batch_workflow_statistics_service.py` 无 `sys_users` 引用（或该文件已删除）；(2) 迁移项 code-execution-control-console 已上线且功能等价验证通过。任一不满足 MUST 阻塞切流与后续清理。

#### Scenario: P1 未完成时阻塞

- **WHEN** `rg '/admin/gaia/workflow/batch' web/` 或 `rg 'sys_users' api/services/` 仍有业务代码命中
- **THEN** 切流不得开始，回到 P1 处置

#### Scenario: 依赖齐备放行

- **WHEN** 两项前置验证全部通过并留存检查记录
- **THEN** 允许进入 GVA 入口停用步骤

### Requirement: GVA 冗余双写入口停用

系统 SHALL 停用 GVA 管理端的用户额度管理、系统集成配置、全局代码授权（`sys_user_global_code` 维护）三处入口（摘除菜单/权限，服务本体保留运行），使 Console 成为唯一写入方。停用动作 MUST 可逆（观察期内可快速恢复）。

#### Scenario: GVA 入口不可达

- **WHEN** 管理员登录 GVA 管理端
- **THEN** 额度管理、系统集成、全局代码授权菜单不可见或操作被拒绝

#### Scenario: Console 侧功能不受影响

- **WHEN** GVA 入口停用后，管理员在 Console 侧修改用户额度与系统集成配置
- **THEN** 操作成功，`account_money_extend` / `system_integration_extend` 正常更新

### Requirement: 计费周期观察与放行判据

GVA 入口停用后，系统 SHALL 观察一个完整计费周期（建议自然月，与月度额度重置对齐），期间以巡检确认：(1) `account_money_extend`、`system_integration_extend` 的全部写入来源为 api 侧（无 GVA 来源写入）；(2) code 节点执行控制在新链路下工作正常（配置变更 → redis → 节点行为）；(3) 计费扣减、额度重置、SSO 登录无异常。三项全部满足 MUST 作为进入清理阶段的放行判据。

#### Scenario: 观察期通过

- **WHEN** 一个计费周期结束且三项巡检均无异常
- **THEN** 出具放行记录，允许进入 admin-decommission-cleanup

#### Scenario: 观察期发现 GVA 来源写入

- **WHEN** 巡检发现 `account_money_extend` 或 `system_integration_extend` 存在 GVA 侧写入
- **THEN** 暂停切流，定位写入路径并补充停用措施，观察期重新计时

### Requirement: D3 待确认项处置

对模型供应商网关（`/gaia/proxy/*`）、公开转发代理（`/gaia/forward/proxy/*`，计费写 `account_money_extend.used_quota`）与应用版本管理公开端点（`GET /latest`、`GET /releases`）,系统 SHALL 先取证外部依赖（admin-server/网关访问日志 + 业务访谈），并按结论二选一：

- 无外部依赖：进入下线流程——先停路由（摘除端口映射或反代转发）观察 2-4 周，无异常反馈后方可随 admin 整体删除；
- 有外部依赖：该模块单列后续 change，admin-server 保留至替代方案上线，本 change 的「删除 `admin/` 目录」与「DROP 全部 GVA 表」相应延后，其余清理照常执行。

取证结论与处置决定 MUST 书面记录并回填总线路图 D3。

#### Scenario: 无依赖直接下线

- **WHEN** 访问日志显示观察窗口内三类端点无外部调用且业务确认不在用
- **THEN** 停路由进入 2-4 周观察期，期满无反馈后模块随 admin 删除

#### Scenario: 停路由观察期发现调用方

- **WHEN** 停路由后收到外部客户端故障反馈
- **THEN** 恢复路由，该模块转「有依赖」分支单列后续 change

#### Scenario: 有依赖时主体不阻塞

- **WHEN** 确认 `/gaia/forward/proxy/*` 有钉钉转发在用
- **THEN** 转发代理单列 change，其余 admin 废弃步骤（GVA 入口停用、api/web/部署清理）照常推进
