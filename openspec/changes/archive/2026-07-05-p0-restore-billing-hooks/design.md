# Design: p0-restore-billing-hooks

## Context

分支 `merge/upstream-1.13.3` 是合并上游 1.13.3 后的工作分支，也是后续合并 1.14.2 / 1.15.0 的起点。调研（对照 `origin/main`）确认两类损伤：

- **HEAD 内 6 个文件残留未解决冲突标记**（`api/core/workflow/nodes/knowledge_index/{__init__.py,entities.py,knowledge_index_node.py}`、`knowledge_retrieval/{knowledge_retrieval_node.py,retrieval.py}`、`trigger_plugin/trigger_event_node.py`）；当前工作区已有修复这些文件的未提交改动。
- **4 处计费挂点在合并中丢失**（已用 `git show origin/main:<path>` 逐一核实存在、且当前分支确认缺失）：
  1. `api/core/app/workflow/layers/persistence.py` 的 `_handle_node_succeeded`（当前分支 L269 起）缺失对 `update_account_money_when_workflow_node_execution_created_extend.delay(...)` 的派发（origin/main 约 L301，Celery 队列 `extend_high`）；
  2. `api/core/app/apps/workflow/app_generator.py`（origin/main 约 L167-180）与 `api/core/app/apps/advanced_chat/app_generator.py`（约 L136-146）缺失 `extras["app_token_id"] = api_token.id` 写入（当前分支 `rg app_token_id` 仅命中 agent_chat/chat/completion 与 workflow/generate_task_pipeline.py，这两个文件确认丢失）；
  3. `api/core/app/apps/advanced_chat/generate_task_pipeline.py`（origin/main 约 L315-321）缺失 `ApiTokenMessageJoinsExtend(...).add_app_token_record_id()` 关联记录写入；
  4. `api/extensions/ext_celery.py` 缺失整段「二开部分」imports + beat_schedule（origin/main 约 L188-209，注册 3 个 `api/schedule/*_extend.py` 任务）——当前文件 `rg -c extend` 为 0。

计费体系背景（本 change 只恢复挂点，不改动这些组件）：

- 用户额度：`account_money_extend` 表；密钥额度：`api_token_money_extend` 四表；归因表：`end_user_account_joins_extend`、`api_token_message_joins_extend`。
- 消息侧扣费走 Blinker 信号 `message_was_created` 的 handler（`api/events/event_handlers/update_account_money_when_messaeg_created_extend.py`），**该链路未断**；workflow 侧扣费走 Celery 任务（队列 `extend_high`），断在 persistence.py 挂点。
- 3 个额度重置任务文件本体存在于 `api/schedule/`（`update_account_used_quota_extend.py`、`update_api_token_daily_used_quota_task_extend.py`、`update_api_token_monthly_used_quota_task_extend.py`，队列 `extend_low`），只是 beat 注册丢失。

疑似合并残留：`api/core/workflow/workflow_cycle_manager.py`（19KB，fork 为修 import 加回的旧路径「幽灵文件」，是当前分支上 `update_account_money_when_workflow_node_execution_created_extend` 的唯一引用方，但该文件本身已无调用方）；`api/core/workflow/nodes/node_mapping.py`（无静态 import 方，`get_node_type_classes_mapping` 实际由 `core/workflow/node_factory.py` 提供；存在 `api/tests/unit_tests/core/workflow/test_node_mapping_bootstrap.py` 需一并确认）；两个 0 字节空文件（`api/controllers/service_api/dataset/upload_file.py`、`api/core/model_runtime/model_providers/bedrock/llm/llm.py`）。

约束：本 change 是 Phase 2 上游合并的前置阻塞项；所有恢复以 `origin/main` 为唯一对照基准；不引入新架构。

## Goals / Non-Goals

**Goals:**

- HEAD 无冲突标记残留，`api/` 全量可编译（py_compile / import 冒烟通过）。
- 4 处计费挂点与 `origin/main` 行为等价恢复，workflow 扣费、密钥额度归因、额度周期重置全部复活。
- beat 任务注册获得 `CeleryScheduleTasksConfig` 风格配置开关（默认开启，保持与 origin/main 行为一致）。
- 幽灵文件与死代码清零，消除对后续合并的干扰。
- 产出 6 条链路的可重复计费回归清单，并以 tag `fork-pre-merge-1.15.0` 固化基线。

**Non-Goals:**

- 不接入上游 quota v3（`quota_reserve/commit/release`）——Phase 1 决策 D1 已倾向保持自建体系。
- 不做扣费幂等化、原子化、缓存等加固（Phase 3）。
- 不收窄 `service_api/wraps.py` 的 30+ 方法签名侵入面（Phase 3.1）。
- 不修改前端、不做数据库 schema 变更、不动 admin。

## Architecture Assessment

### Existing Design Reuse

- **挂点代码 100% 来自 `origin/main`**：4 处恢复均为把 origin/main 中带「二开部分 Begin/End」标记的代码块移植回对应文件的对应位置，不重写、不重构。
- **Celery 任务与队列复用**：`api/tasks/extend/update_account_money_when_workflow_node_execution_created_extend.py`（`extend_high`，`max_retries=3`）与 `api/schedule/*_extend.py` 三个任务（`extend_low`）均已在仓库中，仅恢复派发点/注册点。
- **配置模式复用**：开关沿用上游 `api/configs/feature/__init__.py` 中 `CeleryScheduleTasksConfig` 的 `ENABLE_*: bool = Field(...)` 模式，但定义在 fork 配置入口 `api/configs/extend/__init__.py`（遵循 AGENTS.md「fork 配置经 configs/extend 挂载」约定），避免侵入上游文件。
- **回归清单落点复用文档体系**：写入 `docs/dify-plus/`，与既有《与上游差异总表》《上游升级与回归检查清单》并列。

### Boundaries and Ownership

- **capability 边界**：`merge-residue-cleanup`（仓库卫生）/ `workflow-node-billing`（graph 持久化层 → Celery）/ `api-token-quota-attribution`（app_generator 入口层 + task pipeline 层 → 归因表）/ `quota-reset-scheduling`（ext_celery 装配层）/ `billing-regression-baseline`（验证资产）。
- **调用关系**：`persistence.py` 只负责在节点成功事件时**异步派发**（`.delay`），扣费计算、重试（`max_retries=3`）、账号归因全部由 Celery 任务自身负责——失败不影响 workflow 主流程。`app_generator.py` 只负责把 `api_token` 透传进 `extras`；限额**拦截**发生在 `service_api/wraps.py` 的 `validate_app_token`（本 change 不动，仅回归验证）。
- **状态所有权**：扩展表归 `migrations_extend` Alembic 链所有，本 change 零 schema 变更。
- **配置所有权**：新增开关归 fork（`configs/extend`），上游合并时无冲突面。

### Options and Rationale

见 Decisions。

### Quality Attributes

- **可靠性**：workflow 扣费为异步派发 + 任务级重试，恢复后不改变主流程失败语义；beat 任务错过窗口的补偿逻辑维持任务自身现状（Non-Goal）。
- **可维护性**：恢复块保留「二开部分 Begin/End」注释锚点，并要求实现后把最新坐标登记进回归清单文档，作为 Phase 2 挂点搬迁 checklist 的输入。
- **可测试性**：验收以可执行命令表达（`git grep`、`celery inspect`/beat 配置断言、SQL 增量断言），回归清单要求"可重复执行"。
- **可部署性**：默认开关值 = 开启，部署无需新增环境变量即恢复 origin/main 行为；关闭开关可作为紧急止血手段。

### Complexity and Exceptions

无新增抽象、依赖、服务、存储或协议。唯一"新增"是 3 个布尔配置开关，属最小 API 面，回滚方式为设为 false。

## Decisions

1. **恢复而非重构**（vs 借机把挂点改为事件/信号机制）：Phase 0 的唯一目标是回到可信基线，任何行为变化都会污染后续合并的对照物。重构机会留给 Phase 3。
2. **以 `origin/main` 为唯一对照基准**（vs 以更早的 release tag 为准）：`origin/main` 是计费功能最后的完整形态，且线路图调研已按它标定行号；混用多个基准会引入歧义。
3. **beat 开关放 `configs/extend/__init__.py` 而非改上游 `CeleryScheduleTasksConfig` 类本体**（vs 直接在上游类里加字段）：保持 fork 配置隔离，上游 1.14/1.15 对 `feature/__init__.py` 的改动不会与之冲突。仅借用其命名与 Field 风格（如 `ENABLE_EXTEND_QUOTA_RESET_TASKS: bool = Field(default=True)` 或按任务拆分为 3 个开关——实现时二选一，建议单开关控制 3 个任务，减少配置面）。
4. **`workflow_cycle_manager.py` 直接删除**（vs 保留观望）：它是旧路径幽灵文件，其中的计费 hook 无调用方；保留会让 `rg update_account_money_when_workflow_node_execution_created_extend` 出现双命中，干扰 Phase 2 的挂点定位。删除前需 `rg` 确认无 import 方。
5. **`node_mapping.py` 先确认后处置**（vs 直接删）：虽无静态 import 方，但存在配套测试 `test_node_mapping_bootstrap.py`（subprocess 方式动态执行），需一并确认；若确认为合并残留则文件与测试同批删除，否则保留并在回归清单登记结论。
6. **冲突修复与挂点恢复分两个 commit**（vs 一把梭）：冲突修复固化的是工作区既有改动，挂点恢复是新增改动；分开提交便于 Phase 2 出问题时二分定位。tag 打在全部完成之后。

## Risks / Trade-offs

- [origin/main 与当前分支的上下文漂移导致挂点无法逐行照搬（如 `persistence.py` 在 1.13.3 中签名/局部变量有变）] -> 恢复时以"行为等价"为准而非逐字复制；恢复后必须跑通 workflow 扣费链路的实际增量验证（`account_money_extend.used_quota` 变化），不以代码 diff 通过为完成标准。
- [beat 任务恢复后在月初/凌晨触发大批量 UPDATE，对生产库造成冲击] -> 本 change 保持任务本体不动（分批化属 Phase 3.5）；配置开关提供紧急关闭手段。
- [删除幽灵文件误伤动态 import（Flask CLI、celery imports、测试 subprocess）] -> 删除前用 `rg` 全仓检索文件名与模块路径，删除后运行 `uv run --project api python -c "from app_factory import create_app"` 与相关单测冒烟。
- [回归清单中部分链路（月初重置）无法在验收窗口自然触发] -> 清单允许以"手动触发 Celery 任务 + 断言表数据"替代自然触发，但需在清单中注明两种执行方式。
- [工作区还混有与本 change 无关的改动（Dockerfile、web 侧文件等）] -> 提交冲突修复时只挑 6 个目标文件及其必要配套，其余改动留在工作区，避免把无关变更混入基线 commit。

## Migration Plan

无数据库迁移。部署即代码发布：

1. 合入后重启 api、worker、beat 三类进程（beat 需重启才能加载新 schedule）。
2. 验证：beat 日志/`celery -A app inspect registered` 可见 3 个 extend 任务；触发一次 workflow 运行确认 `account_money_extend.used_quota` 增量。
3. 回滚：revert 挂点恢复 commit 即可（无 schema、无数据回滚需求）；或将配置开关设为 false 临时停用 beat 任务。
4. 全部验收通过后打 tag `fork-pre-merge-1.15.0` 并推送。

## Open Questions

- `node_mapping.py` 与 `test_node_mapping_bootstrap.py` 是否为成对的合并残留？（实现时确认，二者同进退）
- beat 开关粒度：单开关控 3 个任务，还是每任务一个开关？（建议单开关，实现时定案并记录在回归清单文档）
- 工作区中与本 change 无关的未提交改动（如 `api/Dockerfile`、web 侧文件）如何处置不在本 change 范围内，由主 agent 在提交时明确排除。
