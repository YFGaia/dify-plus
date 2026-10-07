# api-token-quota-attribution

Service API 密钥额度归因：`app_token_id` 透传与 `ApiTokenMessageJoinsExtend` 关联记录写入，支撑密钥日/月限额累计与拦截。

## ADDED Requirements

### Requirement: app_generator SHALL 将 api_token 透传进 extras

`api/core/app/apps/workflow/app_generator.py` 与 `api/core/app/apps/advanced_chat/app_generator.py` SHALL 在构造 application generate entity 前，从 `args` 读取 `api_token`，存在时写入 `extras["app_token_id"] = api_token.id`，行为与 `origin/main`（分别约 L167-180、L136-146）等价；同一代码块中随 origin/main 一并恢复 `args.get("account_id")` 存在时写入 `extras["account_id"]` 的逻辑（WebApp 登录用户归因）。

#### Scenario: Service API 调用 workflow 应用时 extras 携带 app_token_id

- **WHEN** 使用 API 密钥经 Service API 触发 workflow 应用生成
- **THEN** `WorkflowAppGenerateEntity.extras` 中包含 `app_token_id`，其值等于该 `ApiToken.id`

#### Scenario: Console 调试运行不写 app_token_id

- **WHEN** 从 Console 调试运行（`args` 中无 `api_token`）
- **THEN** `extras` 不包含 `app_token_id`，生成流程行为不变

### Requirement: chatflow pipeline SHALL 写入密钥关联记录

`api/core/app/apps/advanced_chat/generate_task_pipeline.py` SHALL 在 workflow run 启动、message 关联 `workflow_run_id` 之后，当 `extras` 中存在 `app_token_id` 时，构造 `ApiTokenMessageJoinsExtend(app_token_id=..., record_id=message.workflow_run_id, app_mode=AppMode.ADVANCED_CHAT.value)` 并调用 `add_app_token_record_id()` 写入关联记录，行为与 `origin/main`（约 L315-321）等价。

#### Scenario: chatflow 经密钥调用产生归因记录

- **WHEN** 使用 API 密钥经 Service API 调用 advanced-chat 应用并产生一次运行
- **THEN** `api_token_message_joins_extend` 表新增一条 `app_token_id` 与该次 `workflow_run_id` 的关联记录

#### Scenario: 密钥日/月限额拦截恢复生效

- **WHEN** 某 API 密钥的当日或当月累计用量达到 `api_token_money_extend` 配置的限额后再次发起调用
- **THEN** `validate_app_token`（`api/controllers/service_api/wraps.py`，本 change 不修改）依据恢复的用量归因数据拦截该请求
