# M04 源码实现与独立验收交接

## 状态与提交

- 输入 HEAD：`e83bdfe536eb114aa5dee17ddec539d9a71ab99e`；分支 `codex/merge-upstream-1.17.1`，提交前逐次核对 HEAD。
- 上游固定 SHA：`8387590ace4a094de812b7847fc6a4c3a27cd52b`。
- `521eb98761517347f28a18dcdfdcf646c389ddb8`：Service API 额度、身份与参数归因；范围登记。
- `a65840a41a85de545811b8b879891a9f5d5ad278`：app/agent API key 额度 CRUD 与权限、回滚测试。
- `08e130ed1dbe7f8c0907a9171b76406836eeb668`：chatflow INITIAL 守卫及计费/记忆挂点测试。
- M04 **passed**。10.1–10.5 全部通过；独立 Luna 验收线程 `01a0e987-dcbb-79b0-b535-68612b57bb1f` 在冻结 HEAD `615efae14dcff1d02ea51d5c4b61e403df76ec5b` 通过，记录见 `independent-review.json`。本节点仍不可部署；未执行环境或生产验证。

## 九项挂点及 OAuth 引用

| 挂点 | 实际路径 / 调用与结果 | owner / 验证 |
|---|---|---|
| 1 节点扣费 | `api/core/app/workflow/layers/persistence.py::_handle_node_succeeded` → `tasks/extend/update_account_money_when_workflow_node_execution_created_extend.py`。成功事件派发一次，保留 user role/run ID；retry、failure、RESUMPTION 不增加派发。源码未改。 | M04 / hooks 测试实际调用 persistence；既有 task-body 测试执行 `.run`；外部 broker 被隔离。 |
| 2 请求线程 extras | `api/core/app/apps/{advanced_chat,workflow}/app_generator.py::generate` 在构造生成实体、启动 worker 前物化 token/account ID。 | M04 / 两个真实 generate 方法捕获实体及当前线程 ID。 |
| 3 workflow/chatflow join | 两个 `generate_task_pipeline.py::_handle_workflow_started_event`；advanced_chat 补 `WorkflowStartReason.INITIAL`。初始持久化一条关联，两次恢复后仍一条。 | M04 / SQLite 真实关联写入；这是 fork 既有缺口的 C09/C13 修正，不称升级新增回归。 |
| 4 message join | `api/core/app/apps/{chat,agent_chat,completion}/app_generator.py::generate`：在 `_init_generate_records` 后复用注入 Session 写 join 并 commit，随后才初始化 queue。 | M04 / 三个真实 generate 方法与真实 SQLite join；检查注入 Session、commit 顺序及 account extras。 |
| 5 Service API admission | `api/controllers/service_api/wraps.py::validate_app_token` 恢复 owner account 总额、token 日/月额度、kwargs 和 EndUser/account join。只有声明该参数或接受 kwargs 的 caller 才收到 token/app_model。 | M04 / `callers.json` 枚举全部 36 个入口，真实 decorator + SQLite 配额、归因 + 每个原签名 probe；包含 human_input_form、audio、annotation、file_preview。生成入口的默认 None 不吞 token。 |
| 6 key CRUD | `api/controllers/console/apikey.py`：六字段、旧 key 默认、原子创建、PUT（owner/admin + app/Agent RBAC + tenant）、quota 软删及 cache 失效。 | M04 / SQLite CRUD、跨租户/资源/角色、创建/修改/删除失败回滚；原 Agent gate 测试保留。Agent list/create/delete 共用 helper，编辑使用其绑定 app ID 的 `/apps/{id}/api-keys` PUT 与 AgentBehindApp MANAGE。未修改跨 owner roster。 |
| 7 message signal | `api/core/app/task_pipeline/easy_ui_based_generate_task_pipeline.py::_save_message` → `api/events/event_handlers/update_account_money_when_messaeg_created_extend.py::handle`。保留先填消息用量、发 signal、handler commit 顺序。 | M04 / 真 handler 注册、payer 优先级、USD/RMB、token/account 金额；真实 save-message 信号观察已提交 join 和最终用量。 |
| 8 三个 beat | `api/extensions/ext_celery.py::init_app` → `api/schedule/update_{account_used_quota,api_token_daily_used_quota_task,api_token_monthly_used_quota_task}_extend.py`。 | M04 / 真 init_app（Celery 构造替身）：名称、imports 唯一、开关、00:00/每月1号；任务 decorator `extend_low` 静态断言。未连接 broker，未声称运行中 beat 验收。 |
| 9 memory | `api/core/memory/token_buffer_memory.py::get_history_prompt_messages/messages_context_handling`；`api/core/app/apps/base_app_runner.py::AppRunner.add_messages_context`（实际类名 AppRunner）。 | M04 / control_registers 开/关、assistant ID、匿名多轮切分；缺行/NULL retention 跳过；函数内 db 延迟导入被实际调用覆盖。源码未改。 |
| 10 OAuth | M03 9.2、`evidence/M03/result.json`、`evidence/M03/handoff.md`。 | M03；本节点只引用，OAuth 源码与输入 HEAD 一致。 |

## 额度与删除的精确边界

- fork `ApiTokenMoneyExtend.accumulated_quota` 是累计用量，没有独立 token 总额上限字段。本次未新增字段或迁移。`-1` 无限沿基线只用于 key 日/月限额；Account `used_quota >= total_quota` 保持原行为，包括 total=-1 仍拒绝，已有定向断言。
- 旧 key 无 quota 返回 description 空、三类用量 0、日/月限额 -1；PUT 可建立其额度行。创建 token/quota 同一 commit，失败一起回滚。
- 删除物理移除 token、软删额度行、失效 ApiTokenCache，**保留历史 ApiTokenMessageJoinsExtend**。主 Agent 对照基线指出早期额外清理 join 无合同依据，已移除并增加保留断言；最终提交不含该清理。
- C10 dataset key 无 fork quota。`datasets/datasets.py` 完全未改；保留逐请求 binding、tenant/RBAC、创建 reveal-once、列表遮罩、删除 FK cascade。测试实际调用其 CRUD 并验证 quota 表无记录。
- Cloud vector-space Sandbox unknown→503、订阅限额、dataset auth/binding 函数 AST 与固定上游一致，fork额度不替代 Cloud 检查。
- 匿名 `is_money_limit` 与 message payer 保持 A04/C13：无额度放行、读取异常拒绝，后置优先 account / 映射，否则 end_user UUID；不选 app owner。该生产文件未修改。

## 验证与修正

- 最终 `sh openspec/changes/merge-upstream-1-17-1/evidence/M04/verify.sh`：**370 passed，2 warnings，退出码 0**。
- 两条警告为现有 cgi/Pydantic 弃用；LiteLLM 无网时回退本地价格表，未访问真实 provider。
- 9 个改动 Python 文件 Ruff clean；OpenAPI 导出退出码 0，六项 schema 断言通过。生成物未手改，M05/M08 统一刷新 contracts。
- 最初全套出现三项 broker 连接失败与一项 memory query-count 不匹配：为已登记的 persistence 测试隔离 broker，记忆测试明确检查第五条 fork context 查询，并保留两条 batch file query 上限。生产 hooks 未改。
- 16 项固定上游/输入版本不可改边界、36 callers、51 个源码/测试/锁文件 SHA256 见 `static-scope.json`、`callers.json`、`verified-inputs.json`。最终运行前后哈希一致。
- 新测试路径按主 Agent 要求补登记到 conflict-ownership.tsv、owned_conflict_paths（M04/V01），记录见 scope-registration.json。其它 owner 未变。

## 独立验收记录

在仓库根目录执行：

```sh
sh openspec/changes/merge-upstream-1-17-1/evidence/M04/verify.sh
UV_PROJECT_ENVIRONMENT=/private/tmp/dify-m02-python-complete UV_CACHE_DIR=/private/tmp/dify-m02-uv-cache uv run --project api --no-sync python openspec/changes/merge-upstream-1-17-1/evidence/M04/verify_static.py
openspec validate merge-upstream-1-17-1 --strict
```

独立 Luna 在 `615efae14dcff1d02ea51d5c4b61e403df76ec5b` 完成复核：370 项定向测试通过、Ruff 与 OpenSpec strict 通过、51 个输入 SHA 全部匹配，未发现新缺陷。验收细节见 `independent-review.json`。若后续源码改变，应更新受影响证据并重跑。`verify_static.py` 会刷新本节点 JSON 证据。

## 仍有的限制

未修复 Celery redelivery 非幂等、message 读改写并发少扣、匿名付款人/前后限额不一致；这些属于 P4 既有债务。本节点不证明真实 Postgres 并发、生产队列、reset 快照运行、provider、OAuth/SSO 或生产业务验收。未访问生产数据、未发布、未改依赖锁/api/.venv、未操作用户未跟踪目录。源码自测通过不代表可部署。
