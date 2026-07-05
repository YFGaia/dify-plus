# billing-data-governance

计费数据治理：`api_token_message_joins_extend` 归档/TTL 策略与额度重置任务分批处理，控制大表增长与内存占用。

## ADDED Requirements

### Requirement: api_token_message_joins_extend 定期归档

系统 MUST 提供归档表 `api_token_message_joins_archive_extend`（结构同源表，经 `api/migrations_extend/` 建表）与每日执行的 Celery beat 归档任务（`extend_low` 队列）：将 `created_at` 早于保留期（配置 `API_TOKEN_MESSAGE_JOINS_RETENTION_DAYS`，默认 180 天）的行分批（每批 5000 行、每批独立事务）迁移到归档表并从源表删除。任务 MUST 提供开关 `ENABLE_API_TOKEN_MESSAGE_JOINS_ARCHIVE_TASK`（默认开启），并记录每批归档行数日志。

#### Scenario: 过期数据被归档

- **WHEN** 归档任务执行，源表存在 `created_at` 早于保留期的记录
- **THEN** 这些记录出现在归档表中且从源表删除，行数守恒（迁移数 = 删除数）

#### Scenario: 保留期内数据不受影响

- **WHEN** 归档任务执行
- **THEN** `created_at` 在保留期内的记录保留在源表，在线扣费链路对 `record_id` 的短窗口查询不受影响

#### Scenario: 开关关闭时不执行

- **WHEN** `ENABLE_API_TOKEN_MESSAGE_JOINS_ARCHIVE_TASK` 为 false
- **THEN** beat 不调度该任务，源表不发生归档删除

### Requirement: 额度重置任务分批处理

三个额度重置定时任务（`update_account_used_quota_extend`、`update_api_token_daily_used_quota_task_extend`、`update_api_token_monthly_used_quota_task_extend`）的快照阶段 MUST NOT 使用 `.all()` 一次性加载全表，MUST 改为流式分批（`yield_per` 或 keyset 分页，批大小约 1000）并分批落库快照；重置阶段的整表 `UPDATE` 单条 SQL 语义保持不变。

#### Scenario: 大表快照内存受控

- **WHEN** `account_money_extend` 存在大量记录且月度重置任务执行
- **THEN** 任务以固定批大小流式处理，全量快照写入快照表，进程内存占用不随表行数线性增长

#### Scenario: 重置语义不变

- **WHEN** 重置任务执行完成
- **THEN** 快照表含执行时点的全量额度快照，且对应额度表的 `used_quota` 全部归零（与改造前行为一致）

### Requirement: 归档可逆

归档 MUST 可通过反向 `INSERT INTO ... SELECT` 从归档表还原到源表；归档表 MUST NOT 在未确认还原能力前被删除。

#### Scenario: 计费争议追溯

- **WHEN** 需要追溯一条 200 天前 API 调用的密钥归因
- **THEN** 归档表中可按 `record_id` 查到对应 `app_token_id` 映射
