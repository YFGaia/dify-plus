# batch-workflow-disposition-gate

使用量验证决策门与两方案共同收尾要求。本 capability 定义决策过程本身的可验证要求：任何删除或迁移动作开始前，决策门 MUST 先通过。

## ADDED Requirements

### Requirement: 使用量证据采集

处置动作（方案 A 删除或方案 B 迁移）开始前，系统维护者 SHALL 在生产数据库执行使用量验证 SQL 并留存结果：近 90 天批次计数（`SELECT count(*) FROM batch_workflows_extend WHERE created_at > now() - interval '90 days'`）与按月分布（`date_trunc('month', created_at)` 分组计数）。

#### Scenario: 近 90 天零使用

- **WHEN** 近 90 天计数为 0 且按月分布显示长期无新增
- **THEN** 决策门输出「候选结论：方案 A（删除）」，进入业务确认环节

#### Scenario: 存在近期使用

- **WHEN** 近 90 天计数大于 0
- **THEN** 决策门输出「候选结论：方案 B（迁移）」，并附使用账号/频次明细供业务评估

#### Scenario: 无法访问生产库

- **WHEN** 执行者无生产库权限
- **THEN** 决策门 MUST 暂停并升级给具备权限的运维/DBA 执行，不得凭推测（如「nginx 无 /admin location」）单独放行删除

### Requirement: 业务确认与决策记录

SQL 证据 MUST 与业务方确认结论共同构成决策依据。最终结论（A 或 B）SHALL 以书面形式回填至本 change 的 proposal.md 与 `docs/dify-plus/实现线路图-upstream-1.15.0升级与admin废弃.md` 的决策点 D2。

#### Scenario: 双门槛通过

- **WHEN** SQL 证据与业务确认一致指向某一方案
- **THEN** 决策记录（结论、证据摘要、确认人、日期）写入上述两处文档，对应方案的任务组解除阻塞

#### Scenario: 证据与业务判断冲突

- **WHEN** SQL 显示零使用但业务方声称功能在用（或反之）
- **THEN** 决策门 MUST 保持关闭，补充调查（如自定义网关访问日志、用户访谈）后重新评估

### Requirement: 前端构建配置 admin 残留清理

决策门结论落地（方案 A 删除完成，或方案 B 前端切换完成）后，`web/next.config.ts` 中的 `/admin` dev rewrite 与 `web/docker/entrypoint.sh` 中的死变量 `NEXT_PUBLIC_ADMIN_API_URL` SHALL 被移除，或显式移交至 P5（admin 废弃）任务清单并记录。

#### Scenario: 本 change 内清理

- **WHEN** 处置完成且 admin 其他功能不再需要 dev rewrite
- **THEN** 删除 rewrite 与死变量后 `pnpm build` 通过，web 容器启动无该环境变量引用

#### Scenario: 移交 P5

- **WHEN** 处置完成但 P5 尚未启动且 dev 调试仍需访问 admin 其他端点
- **THEN** 在 P5 change（`p5-admin-decommission`）的任务清单中登记这两处清理项，本 requirement 视为满足
