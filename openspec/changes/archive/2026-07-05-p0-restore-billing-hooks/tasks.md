# Tasks: p0-restore-billing-hooks

## 1. 提交冲突标记修复（merge-residue-cleanup）

- [x] 1.1 复核工作区中 6 个冲突修复文件的改动内容（`api/core/workflow/nodes/knowledge_index/{__init__.py,entities.py,knowledge_index_node.py}`、`knowledge_retrieval/{knowledge_retrieval_node.py,retrieval.py}`、`trigger_plugin/trigger_event_node.py`），确认无残留冲突标记、逻辑取舍合理（对照 origin/main 与 upstream 1.13.3 双方版本）。完成标准：逐文件 diff 复核记录。
- [x] 1.2 仅将这 6 个文件（及其必要配套改动）提交为独立 commit，明确排除工作区中与本 change 无关的改动（`api/Dockerfile`、web 侧文件、docs 等）。完成标准：`git show --stat HEAD` 仅含目标文件。
- [x] 1.3 验证：`git grep -lI '<<<<<<<' -- api/ web/` 无源码命中；`uv run --project api python -c "from app_factory import create_app"` 通过。

## 2. 恢复 4 处计费挂点（主 agent 顺序完成）

- [x] 2.1 对照 `git show origin/main:api/core/app/workflow/layers/persistence.py`（约 L290-302），在当前分支 `_handle_node_succeeded` 中恢复 domain execution dict 构造（`created_by_role` 映射、`workflow_run_id` 注入）与 `update_account_money_when_workflow_node_execution_created_extend.delay(...)` 派发，保留「二开部分 Begin/End - 计费」注释锚点。若上下文有漂移，以行为等价为准（见 design Risks）。
- [x] 2.2 对照 origin/main 在 `api/core/app/apps/workflow/app_generator.py`（约 L167-180）与 `api/core/app/apps/advanced_chat/app_generator.py`（约 L136-146）恢复 `extras["app_token_id"] = api_token.id` 写入及配套 `account_id` 透传，含 `cast(ApiToken, api_token)` 与必要 import。
- [x] 2.3 对照 origin/main 在 `api/core/app/apps/advanced_chat/generate_task_pipeline.py`（约 L315-321）恢复 `ApiTokenMessageJoinsExtend(app_token_id=..., record_id=message.workflow_run_id, app_mode=AppMode.ADVANCED_CHAT.value).add_app_token_record_id()` 写入，位置在 message 关联 `workflow_run_id` 之后。
- [x] 2.4 对照 origin/main（约 L188-209）在 `api/extensions/ext_celery.py` 恢复「二开部分」imports 与 beat_schedule 三条目（`update_account_used_quota` 每月 1 号、`update_api_token_daily_used_quota_task_extend` 每天、`update_api_token_monthly_used_quota_task_extend` 每月 1 号），并在 `api/configs/extend/__init__.py` 新增 `CeleryScheduleTasksConfig` 风格布尔开关（默认 true）控制注册；开关粒度按 design Decision 3 定案（建议单开关）并在回归清单文档记录。
- [x] 2.5 将 2.1-2.4 作为同一批「挂点恢复」commit 提交（与 1.2 的冲突修复 commit 分开），commit message 注明对照的 origin/main commit hash。
- [x] 2.6 挂点级验证：`rg` 确认 4 处挂点代码就位且带「二开部分」锚点；`uv run --project api python -c "from app_factory import create_app"` 通过；启动方式检查 `celery_app.conf.beat_schedule` 包含 3 个 extend 条目、设开关为 false 时不包含（对应 spec quota-reset-scheduling 两个 Scenario）。

## 3. 幽灵文件与死代码清理（可与 §2 并行，见 §5）

- [x] 3.1 删除 `api/core/workflow/workflow_cycle_manager.py`；删除前 `rg "workflow_cycle_manager" api/` 确认无其他引用方。注意：本任务与 2.1 存在语义依赖（删除后任务引用唯一化断言才成立），文件本身无编辑冲突。
- [x] 3.2 确认 `api/core/workflow/nodes/node_mapping.py` 去留：`rg` 全仓检索模块路径引用 + 阅读 `api/tests/unit_tests/core/workflow/test_node_mapping_bootstrap.py`（subprocess 动态执行，需人工判读）；确认为残留则文件与测试同批删除，否则保留并在回归清单文档记录结论。
- [x] 3.3 删除两个 0 字节空文件：`api/controllers/service_api/dataset/upload_file.py`、`api/core/model_runtime/model_providers/bedrock/llm/llm.py`；删除前确认无 import 方（注意 package `__init__` 与蓝图注册的间接引用）。
- [x] 3.4 验证：删除后 `uv run --project api python -c "from app_factory import create_app"` 通过；`uv run --project api python -m compileall api -q`（或等效 py_compile 全量）通过；`rg -l "update_account_money_when_workflow_node_execution_created_extend" api/ --glob '!tests'` 仅命中任务定义与 `persistence.py` 两处。

## 4. 计费回归基线与留档（billing-regression-baseline）

- [x] 4.1 编写回归清单文档（`docs/dify-plus/` 下，建议命名 `计费回归基线清单.md`），覆盖 6 条链路，每条含前置条件/步骤/SQL 或接口断言：Console 调试运行扣费、Explore 运行扣费、WebApp 登录+扣费、Service API 密钥日/月限额拦截、workflow LLM 节点扣费、月初额度重置（注明自然触发与手动触发两种执行方式）。清单编写可先行，不依赖 §2 完成。
- [ ] 4.2 （**延后执行**：本机无 dify-plus 运行栈，运行时回归由用户在自备环境按《计费回归基线清单》执行并登记结果）在挂点恢复完成的代码上逐条执行 6 条链路回归，重点断言：workflow 运行后 `account_money_extend.used_quota` 增量（spec workflow-node-billing）、`api_token_message_joins_extend` 新增归因记录与限额拦截（spec api-token-quota-attribution）、手动触发重置任务后周期用量字段归零（spec quota-reset-scheduling）。全部通过并在清单登记结果。
- [x] 4.3 将 4 处挂点的最新坐标（文件+行号+锚点注释）登记进回归清单文档（或 `docs/dify-plus/与上游差异总表.md`），作为 Phase 2 合并时的挂点搬迁 checklist 输入。
- [x] 4.4 全部验收通过后在最终 commit 打 tag `fork-pre-merge-1.15.0`；验证 `git rev-parse fork-pre-merge-1.15.0` 指向包含全部内容的 commit。

## 5. Parallelization Plan（并发执行策略）

- [x] 5.1 **主 agent 顺序完成（不可委派）**：§2 全部任务（4 处挂点恢复）必须由主 agent 顺序处理——4 处恢复共享同一对照基准（origin/main 的同一批「二开部分」代码块）、需统一判断上下文漂移、且要求同批 commit 与统一验证（2.5/2.6），拆给多个 sub-agent 会导致对照口径与提交边界碎片化。§1.2 的选择性提交与 §4.4 打 tag 属仓库状态变更，同样由主 agent 执行。
- [x] 5.2 **可交 sub-agent 并行的任务组**（互不编辑相同文件，可与 §2 同时进行）：
  - (a) §1.1 冲突修复复核：context inputs = 6 个目标文件的工作区 diff + `git show origin/main:<path>` + `git show 1.13.3:<path>`；只读权限；产出 = 逐文件复核结论（取舍是否正确、有无遗漏）；
  - (b) §3.1-3.3 幽灵文件清理与引用面调研：context inputs = design.md「Context」节 + 全仓 `rg` 权限；可编辑范围仅限 4 个待删文件与配套测试；产出 = 删除 diff + 引用检索证据 + `node_mapping.py` 去留结论；
  - (c) §4.1 回归清单编写：context inputs = proposal.md、design.md、specs/billing-regression-baseline、`api/events/event_handlers/`、`api/schedule/`、`api/tasks/extend/` 相关源码（只读）；可编辑范围仅限 `docs/dify-plus/` 新文档；产出 = 清单初稿。
- [x] 5.3 **主 agent 合并与验证责任**：主 agent 负责 (1) 汇总三个 sub-agent 产出并裁决冲突结论（尤其 `node_mapping.py` 去留、冲突修复取舍异议）；(2) 控制提交顺序——commit 1 冲突修复（§1）→ commit 2 挂点恢复（§2）→ commit 3 清理（§3，需在 2.1 之后合入以满足引用唯一化断言）→ 回归（§4.2）→ tag（§4.4）；(3) 亲自执行全部验证命令（1.3、2.6、3.4、§6），sub-agent 的验证结果仅作参考不作验收依据。

## 6. Architecture Verification

- [ ] 6.1 （**延后执行**：同 4.2，需 api+worker+beat+LLM 凭据的运行环境）挂点行为等价验证（integration 级）：本地起 api + worker + beat，实际触发一次含 LLM 节点的 workflow 运行与一次密钥调用的 chatflow 运行，用 SQL 断言 `account_money_extend.used_quota` 增量与 `api_token_message_joins_extend` 新记录——不以代码 diff 通过为完成标准（对应 design Risks 第 1 条）。
- [x] 6.2 配置开关回滚路径验证：设开关为 false 重启，确认 beat_schedule 无 extend 条目且应用其余功能正常（验证紧急止血手段可用，对应 design Migration Plan 3）。
- [x] 6.3 静态检查：`uv run --project api python -m compileall api -q` 全量通过；如仓库配置有 lint（ruff 等）对被改动文件执行并通过。
- [x] 6.4 回归防护：确认 `api/tests/` 中与被删文件相关的测试（如 `test_node_mapping_bootstrap.py`）已同步处置，`uv run --project api pytest api/tests/unit_tests/core/workflow/ -q`（或受影响最小集）通过。6.3/6.4 可交 sub-agent 并行执行，结果由主 agent 复核后勾选。
