# M04 实施前只读分析

- 独立分析任务：`01a0e969-2e41-7942-b435-6c690a4bc5d9`（Sol/high）
- 分析源码：`c2ec20d3945da1b72aa2113d507388ffb67dd641`
- 上游目标：`8387590ace4a094de812b7847fc6a4c3a27cd52b`
- 分析性质：检查合并后源码、fork 父提交、执行图及 A04 契约；未编辑代码、未运行测试、未访问真实环境。

## 结论和契约归属

Service API 的 `validate_app_token` 当前只注入 `app_model`，没有恢复 fork 的账号/密钥额度校验、`api_token` 注入或 EndUser→Account 归因；下游生成器仍接收 `api_token` 参数，所以这条前置断链会令 Service API 生成缺少 token 归因。`advanced_chat` 的 workflow-start join 也没有 `INITIAL` 条件；它在 fork 父提交已有此行为，属于需按当前契约收紧的缺口，不能宣称是本次升级新增回归。

执行图中的“十项挂点”总览包含 OAuth，但 OAuth gateway、Casdoor/OaOAuth、state 与回跳、钉钉已由 M03 完成。M04 实施及复核其余九项；十项整体证据中 OAuth 引用 M03 的 9.2、`evidence/M03/result.json` 与 `handoff.md`，避免重复实现。M04 编码开始前将此拆分同步到任务描述与图节点。

契约索引：C08 workspace summary 归 M02；M04 直接消费 C09 app key 额度、C10 dataset key scope/masking、C13 匿名计费基线。C14 的匿名 UI context guard 归 M06，M04 负责其中后端记忆调用链与 NULL retention 行为。

## 十项挂点与当前证据

| #                                         | merge 后源码证据与 M04 边界                                                                                                                                                                                                                                                                                                                                                        |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1 工作流节点计费                          | `api/core/app/workflow/layers/persistence.py::WorkflowPersistenceLayer._handle_node_succeeded` 仍在成功路径派发扩展扣费任务，并保留 `created_by`、角色和 run ID。`_handle_graph_run_started` 含恢复路径；不要在恢复、retry 或 Agent v2 同步保存再添派发。Celery 重投非幂等仍属 P4 债务。                                                                                           |
| 2 请求线程 extras                         | `advanced_chat/app_generator.py::generate` 与 `workflow/app_generator.py::generate` 在启动生成前物化 `app_token_id/account_id`。Service API 前置注入缺失会令 token extra 缺失；恢复注入后仍须在请求线程生成 extras，不在 worker 读取 Flask/contextvar。                                                                                                                            |
| 3 workflow/chatflow join                  | 两条 `generate_task_pipeline.py::_handle_workflow_started_event` 都在取得 run ID 后建立 token-run 关联；标准 workflow 有 INITIAL 守卫，advanced chatflow 当前无。按 INITIAL 建一条、RESUMPTION 不新建一条验证；记录这是按契约修正既有缺口。                                                                                                                                        |
| 4 chat/agent_chat/completion message join | 三个 `app_generator.py::generate` 在 `_init_generate_records` 后用传入 Session 写 token-message join；Service API completion 仍传 `api_token` 形参，但当前装饰器不注入。检查 token 进入生成器且 join 提交早于 message 计费信号。                                                                                                                                                   |
| 5 Service API 额度/归因                   | `api/controllers/service_api/wraps.py::validate_app_token` 丢失 fork quota、token kwargs、owner-EndUser 映射。恢复时保留 `-1` 不限、账号总额、token 日/月额度和归档租户拒绝；保留上游 Cloud vector-space 503 与逐请求 dataset binding 校验。审计所有装饰入口参数，包括新 human input/file preview 等端点。                                                                         |
| 6 API key 额度                            | 当前 `api/controllers/console/apikey.py` 缺少 fork 六额度字段、联查、创建/编辑 quota 及删除清理。恢复 app/agent key 能力须保留上游 RBAC、租户过滤、Agent gate、dataset key reveal-once/masking 与 ApiToken cache 语义。                                                                                                                                                            |
| 7 message 计费 handler                    | `api/events/event_handlers/update_account_money_when_messaeg_created_extend.py` handler 仍注册；`easy_ui_based_generate_task_pipeline.py::_save_message` 仍发送 signal。保持提交/计费顺序、USD/RMB 与 payer 优先级。匿名按 C13 保留当前 end_user UUID 归因，不改 `completion.py::is_money_limit` 及其付款人决策，不假定 app owner 付款；并发少扣和匿名归因缺口继续标记为已知债务。 |
| 8 quota beat reset                        | `api/extensions/ext_celery.py::init_app` 仍受原开关控制注册账号月、token 日、token 月三项 reset。保留任务名、队列、调度和开关，不覆盖上游新增 beat；代码存在不等于实际 Celery 运行验收。                                                                                                                                                                                           |
| 9 memory context                          | `TokenBufferMemory.get_history_prompt_messages`、`control_registers` 与 `BaseAppRunner.add_messages_context` 仍在；保留 assistant message ID、函数内延迟导入 db，以及 `AppExtend` 缺行/`retention_number IS NULL` 时跳过。匿名前端 context guard 属 M06。                                                                                                                          |
| 10 OAuth                                  | M03 已完成新 gateway 的 OAuth2/Casdoor token/state/同源回跳及钉钉保留，见 M03 9.2 测试证据和 handoff。M04 仅核对调用边界，不重开旧 controller、不重复改 OAuth。真实 IdP/SSO 仍未验收。                                                                                                                                                                                             |

## 所有权核对和边界

- C10 的 `api/controllers/console/datasets/datasets.py::DatasetApiKeyApi` 当前处理 workspace 级 RBAC、租户绑定、创建 reveal-once 和列表遮罩。对 fork 父提交 `1c3368ed` 的该文件执行 `git grep ApiTokenMoneyExtend` 无命中；dataset key 没有已实现的 fork quota 合约。按 C10 保持 dataset scope/masking，M04 不添加伪额度、不改该文件。若未来要给 dataset key 新增 quota，应先另行冻结契约并将 `datasets.py` 与配套测试登记给唯一 owner。
- `api/controllers/web/completion.py::is_money_limit` 不在 M04 写范围；只审计它和消息 handler 的当前 payer/limit 结果，不改付款策略。M03 handoff 已将该 hunk 留给 M04 负责验收，`is_end_login` 仍是 M03 唯一获批 hunk。
- 当前 M04 冲突清单覆盖 service API、console app keys、执行 hooks、消息 handler、Celery reset 及已列测试；非冲突新增测试先登记，跨 owner 路径先移交。

## Astra 实施顺序和验收重点

1. 先恢复 Service API 前置额度和 EndUser 归因、token 参数注入，再审计每个 decorator caller 的签名和调用形状。
2. 恢复 app/agent API key quota CRUD；单独证明 C10 上游 dataset scope、RBAC、绑定和遮罩仍工作，不扩展 dataset quota。
3. 对照五类消息与两类 workflow/chatflow 关联、请求线程 extras 和节点成功派发；INITIAL 创建 join，RESUMPTION 不新增 join/派发。
4. 核对 message signal payer/额度结果、三个 beat reset、匿名多轮会话和 NULL retention；将 P4 重投、并发读改写、匿名付款人债务与本轮行为回归分开报告。
5. 引用 M03 OAuth 证据闭合十项总览，运行本节点允许的定向验证并将测试源码、输入版本哈希、命令/结果与提交写入 `evidence/M04/`。

本报告是源码级实施前审查；所有所列运行时、Celery、真实数据库、provider、业务验收均待相应门禁执行。
