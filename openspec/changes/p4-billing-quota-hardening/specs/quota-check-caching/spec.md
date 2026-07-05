# quota-check-caching

额度校验的短 TTL 缓存与 DB 异常降级策略：消除每请求查库，DB 抖动时 fail-open 放行并告警而非误拦。

## ADDED Requirements

### Requirement: 额度校验结果短 TTL 缓存

额度前置校验读路径——`money_limit`（`api/controllers/console/money_extend.py`）、`is_money_limit`（`api/controllers/web/completion.py`）、`validate_app_token` 内的账号额度与密钥日/月额度校验——MUST 优先读取 Redis 缓存（账号键 `billing:quota_check:<account_id>`，密钥键 `billing:quota_check:token:<app_token_id>`），缓存未命中时查库并回写，TTL 由配置 `QUOTA_CHECK_CACHE_TTL` 控制（默认 30 秒）。缓存 MUST 存储额度快照（`used_quota`/`total_quota` 或密钥日/月四元组），超限判断逻辑保持不变。

#### Scenario: 缓存命中不查库

- **WHEN** 同一账号在 TTL 窗口内连续发起多次受额度校验的请求
- **THEN** 仅首次请求查询数据库，后续请求由缓存返回判断依据

#### Scenario: TTL 过期后重新加载

- **WHEN** 缓存写入后超过 `QUOTA_CHECK_CACHE_TTL` 秒再次请求
- **THEN** 校验重新查库并刷新缓存，反映最新额度

#### Scenario: 超限判断语义不变

- **WHEN** 缓存快照显示 `used_quota >= total_quota`（或密钥日/月超限，且限额不为 -1）
- **THEN** 请求被与改造前相同的错误类型拦截

### Requirement: DB/Redis 异常时 fail-open 并告警

`is_money_limit` 的裸 `except: return True`（fail-closed）MUST 改为精确异常捕获（`SQLAlchemyError`、`RedisError` 等）：Redis 异常 SHALL 降级为直接查库；数据库异常 SHALL 记录含用户标识的 warning 告警日志并返回不超限（fail-open 放行）。`money_limit` 与 `validate_app_token` 的校验路径 SHALL 采用相同降级策略。fail-open 的权衡依据：事后异步扣费在故障恢复后仍会入账，短窗口超扣有界；而 fail-closed 会造成全部 WebApp 请求不可用。

#### Scenario: DB 抖动不误拦

- **WHEN** 数据库暂时不可用，WebApp 用户发起对话请求
- **THEN** 额度校验记录 warning 告警日志并放行请求，不返回额度不足错误

#### Scenario: Redis 故障降级查库

- **WHEN** Redis 不可用但数据库正常
- **THEN** 校验直接查库完成判断，行为与无缓存时一致

### Requirement: 缓存一致性边界

系统 SHALL 依赖 TTL 自然过期而非扣费后主动失效；缓存脏读窗口 MUST NOT 超过 `QUOTA_CHECK_CACHE_TTL`。该窗口与既有「事前拦截 + 事后异步扣费」的最终一致语义同量级，属可接受设计。

#### Scenario: 刚超限账号的放行窗口有界

- **WHEN** 账号在缓存写入后第 1 秒因扣费达到超限
- **THEN** 该账号最迟在 `QUOTA_CHECK_CACHE_TTL` 秒后的下一次请求被拦截
