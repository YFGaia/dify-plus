# service-api-token-context

Service API 层 api_token 的 contextvar 传递机制：额度校验装饰器不再修改视图函数签名，controller 代码保持上游原样，降低上游升级冲突成本。

## ADDED Requirements

### Requirement: api_token 经 contextvar 传递而非视图签名注入

系统 SHALL 通过 `contextvars.ContextVar`（定义于 `api/libs/api_token_context_extend.py`）在请求生命周期内传递已校验的 `ApiToken`。`validate_app_token` 装饰器 MUST 是唯一写入方，并 MUST 在请求结束时（`finally` 块）重置 contextvar；装饰器 MUST NOT 再向视图函数注入 `kwargs["api_token"]`。

#### Scenario: 装饰器写入且请求后清理

- **WHEN** 一个带 `@validate_app_token` 的 service_api 请求通过密钥校验并执行完毕
- **THEN** 视图执行期间 `get_current_api_token()` 返回该请求的 `ApiToken`，且请求结束后 contextvar 已重置，后续复用同一 worker 的请求不会读到残留值

#### Scenario: 未经装饰器的上下文读取

- **WHEN** 在没有 `validate_app_token` 装饰的调用路径上调用 `get_current_api_token()`
- **THEN** 返回 `None`，调用方不抛出异常

### Requirement: service_api controller 签名回归上游原样

`api/controllers/service_api/app/` 下 9 个 controller（`completion`、`workflow`、`conversation`、`message`、`file`、`audio`、`site`、`app`、`annotation`）的视图方法签名 MUST 与上游对应版本一致，不包含 fork 新增的 `api_token` 形参；`args["api_token"] = api_token` 透传 MUST 被移除，下游消费点（如 `app_generator.py` 的 `ApiTokenMessageJoinsExtend` 关联记录写入）改经 contextvar 或在请求线程内物化的等价方式取值。

#### Scenario: 签名 diff 归零

- **WHEN** 对改造后代码执行 `git diff <upstream-tag> -- api/controllers/service_api/app/`
- **THEN** 9 个 controller 文件的方法签名无差异（wraps.py 是 service_api 目录内唯一的 fork 差异点）

#### Scenario: 密钥关联记录仍然写入

- **WHEN** 使用 API 密钥调用 completion/chat/workflow 类接口并成功创建消息或工作流运行
- **THEN** `api_token_message_joins_extend` 新增一条 `app_token_id` 为该密钥、`record_id` 为消息/运行 ID 的记录（行为与改造前一致）

### Requirement: contextvar 不跨线程隐式传播时的显式物化

对于在 worker 线程中才需要 api_token 的消费点，系统 MUST 在 spawn worker 之前（请求线程内）读取 contextvar 并将所需值物化（如写入 `application_generate_entity.extras["app_token_id"]`），MUST NOT 依赖 contextvar 在线程间自动传播。

#### Scenario: workflow 路径密钥归因不丢失

- **WHEN** 使用 API 密钥调用 workflow/advanced-chat 生成接口（生成逻辑在 worker 线程执行）
- **THEN** 密钥归因值已在请求线程内物化，工作流扣费任务仍能通过 `record_id` 关联到正确的 `app_token_id`

### Requirement: 前置额度校验行为保持不变

签名改造 MUST NOT 改变 `validate_app_token` 的既有校验语义：账号总额度超限抛 `AccountNoMoneyErrorExtend`；密钥日/月额度超限分别抛 `ApiTokenDayNoMoneyErrorExtend`/`ApiTokenMonthNoMoneyErrorExtend`；限额值为 `-1` 时 SHALL 视为不限额。

#### Scenario: 限额 -1 不拦截

- **WHEN** 密钥的 `day_limit_quota = -1` 且 `day_used_quota` 为任意值
- **THEN** 请求不因日限额被拦截

#### Scenario: 账号额度超限拦截

- **WHEN** 密钥所属租户 owner 账号的 `used_quota >= total_quota`
- **THEN** 请求被拒绝并返回 `AccountNoMoneyErrorExtend` 对应错误
