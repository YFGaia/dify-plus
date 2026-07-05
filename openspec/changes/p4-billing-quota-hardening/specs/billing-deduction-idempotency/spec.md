# billing-deduction-idempotency

扣费的原子性与幂等性：账号额度更新采用原子 UPDATE，Celery 扣费任务重试受幂等键保护，保证并发与重试场景下金额守恒。

## ADDED Requirements

### Requirement: 账号扣费为原子 UPDATE

`message_was_created` handler（`update_account_money_when_messaeg_created_extend.py`）对 `AccountMoneyExtend.used_quota` 的扣减 MUST 使用数据库端原子自增表达式（`used_quota = used_quota + price`，`synchronize_session=False`），MUST NOT 使用「先 SELECT 再以 Python 计算值覆盖写」的读改写模式。写法 SHALL 对齐 `update_account_money_when_workflow_node_execution_created_extend` 的既有实现。

#### Scenario: 并发扣费金额守恒

- **WHEN** 同一账号并发产生 N 条消息，单条费用为 p
- **THEN** 全部处理完成后该账号 `used_quota` 恰好增加 N × p，无丢失更新

#### Scenario: 额度记录缺失时建档

- **WHEN** 扣费时该账号无 `account_money_extend` 记录（原子 UPDATE 影响行数为 0）
- **THEN** 系统创建记录：`used_quota` 为本次费用、`total_quota` 为 `dify_config.ACCOUNT_TOTAL_QUOTA`；并发首扣发生插入冲突时 SHALL 回退为原子 UPDATE 重试，不重复建档

### Requirement: Celery 扣费任务重试幂等

workflow 扣费任务（`update_account_money_when_workflow_node_execution_created_extend`，`max_retries=3`）MUST 以业务 ID（`node_execution_id`）为幂等键：任务开始时执行 `SET billing:dedup:<task>:<business_id> NX EX 86400`，键已存在则记录日志并跳过执行；任务异常触发重试前 MUST 删除该键以允许重试执行；扣费 commit 成功后键 SHALL 保留至 TTL 过期。

#### Scenario: 重复投递不重复扣费

- **WHEN** 同一 `node_execution_id` 的扣费任务被投递或执行两次，且第一次已成功 commit
- **THEN** 第二次执行检测到幂等键存在，直接跳过，账号与密钥额度只扣减一次

#### Scenario: 失败重试可正常执行

- **WHEN** 任务第一次执行因数据库异常回滚并进入 Celery 重试
- **THEN** 幂等键已在进入重试前删除，重试执行正常完成扣费，最终只扣一次

#### Scenario: Redis 异常时不阻断扣费

- **WHEN** 设置幂等键时 Redis 连接异常（非键已存在）
- **THEN** 任务记录 warning 日志后继续执行扣费（宁可极小概率重复，不停止记账）

### Requirement: 扣费金额换算与归因契约不变

改造 MUST 保持既有行为契约：付款人归因优先级为 `message.from_account_id` → `from_end_user_id` 是真实 Account 则直接使用 → 否则查 `end_user_account_joins_extend` 按 `created_at` 最新一条；金额为 `total_price`（currency 为 USD 直接使用，否则除以 `dify_config.RMB_TO_USD_RATE`）；密钥额度三字段（`accumulated_quota`/`day_used_quota`/`month_used_quota`）同步自增。

#### Scenario: end_user 归因到 owner 账号

- **WHEN** 一条消息 `from_account_id` 为空且 `from_end_user_id` 不是真实 Account，但 `end_user_account_joins_extend` 存在映射
- **THEN** 费用扣在映射到的最新 `account_id` 上

#### Scenario: 人民币计价换算

- **WHEN** 消息 `currency` 非 USD 且 `total_price = 7.26`、`RMB_TO_USD_RATE = 7.26`
- **THEN** 账号 `used_quota` 增加 1.0（USD 口径）
