# Proposal: p0-restore-billing-hooks

## Why

当前分支 `merge/upstream-1.13.3` 在合并上游 1.13.3 的过程中造成两类基线损伤：

1. **HEAD 提交内残留 6 个文件的未解决冲突标记**（`<<<<<<< HEAD ... >>>>>>> 1.13.3`），涉及 `api/core/workflow/nodes/knowledge_index/`、`knowledge_retrieval/`、`trigger_plugin/` 下的节点实现——当前工作区已有未提交的修复，必须先固化提交。
2. **计费链路断链**：对照 `origin/main` 可确认丢失了 4 处关键计费挂点——workflow/chatflow LLM 节点扣费派发、两个 `app_generator.py` 的 `extras["app_token_id"]` 写入、chatflow 的密钥关联记录写入、`ext_celery.py` 的 3 个额度重置 beat 任务注册。后果是：workflow 侧 LLM 节点费用不再扣减、API 密钥额度累计与日/月限额失效、月度额度重置停摆。

本 change 对应总体线路图（`docs/dify-plus/实现线路图-upstream-1.15.0升级与admin废弃.md`）的 **Phase 0 基线止血**，是后续合并 upstream 1.14.2 / 1.15.0 的**前置阻塞项**：不先恢复一个"计费功能完整、无冲突残留"的可信基线，后续合并中的计费挂点搬迁将失去对照物与回归依据。

## What Changes

- **提交冲突标记修复**：将当前工作区对 6 个文件的冲突修复固化提交（`api/core/workflow/nodes/knowledge_index/{__init__.py,entities.py,knowledge_index_node.py}`、`knowledge_retrieval/{knowledge_retrieval_node.py,retrieval.py}`、`trigger_plugin/trigger_event_node.py`），使 `git grep -l '<<<<<<<' -- api/ web/` 为空。
- **恢复 4 处丢失计费挂点**（以 `origin/main` 为对照基准）：
  - `api/core/app/workflow/layers/persistence.py` 的 `_handle_node_succeeded` 恢复 `update_account_money_when_workflow_node_execution_created_extend.delay(...)` 派发（origin/main 约 L301）；
  - `api/core/app/apps/workflow/app_generator.py`（约 L172）与 `api/core/app/apps/advanced_chat/app_generator.py`（约 L140）恢复 `extras["app_token_id"] = api_token.id` 写入；
  - `api/core/app/apps/advanced_chat/generate_task_pipeline.py`（约 L316-319）恢复 `ApiTokenMessageJoinsExtend` 密钥关联记录写入；
  - `api/extensions/ext_celery.py` 恢复「二开部分」beat_schedule 与 imports（约 L188-209）：3 个额度重置任务（`update_account_used_quota_extend` 每月 1 号、`update_api_token_daily_used_quota_task_extend` 每天、`update_api_token_monthly_used_quota_task_extend` 每月 1 号），并顺势为其增加 `CeleryScheduleTasksConfig` 风格的配置开关。
- **清理幽灵文件/死代码**：删除 `api/core/workflow/workflow_cycle_manager.py`（fork 为修 import 加回的旧路径文件，其中计费 hook 已无调用方）；确认 `api/core/workflow/nodes/node_mapping.py` 去留；删除两个 0 字节空文件（`api/controllers/service_api/dataset/upload_file.py`、`api/core/model_runtime/model_providers/bedrock/llm/llm.py`）。
- **建立计费回归基线清单**（6 条链路）：Console 调试运行扣费、Explore 运行扣费、WebApp 登录+扣费、Service API 密钥日/月限额拦截、workflow LLM 节点扣费、月初额度重置。
- **打 tag `fork-pre-merge-1.15.0`** 留档，作为合并 1.15.0 前的可信基线锚点。

不属于本 change 范围：接入上游 quota v3、`service_api/wraps.py` 侵入面收窄、扣费幂等化（均属 Phase 1/3）。

## Capabilities

### New Capabilities

（`openspec/specs/` 目前为空，以下均为新增 capability）

- `merge-residue-cleanup`: 合并残留治理——冲突标记清零、幽灵文件与死代码清理，保证代码库可编译、无孤儿 import。
- `workflow-node-billing`: workflow/chatflow LLM 节点执行成功后的账号额度扣费派发（Celery 任务 `extend_high` 队列）。
- `api-token-quota-attribution`: Service API 密钥额度归因——`app_token_id` 透传与 `ApiTokenMessageJoinsExtend` 关联记录写入，支撑密钥日/月限额累计。
- `quota-reset-scheduling`: 额度周期重置调度——3 个 Celery beat 定时任务注册（`extend_low` 队列）及配置开关。
- `billing-regression-baseline`: 计费回归基线——6 条链路的可重复回归清单与基线 tag 留档。

### Modified Capabilities

（无——`openspec/specs/` 中尚无已归档 spec）

## Architecture Impact

- **不新增架构元素**：4 处挂点均为"恢复 origin/main 已有实现"，Celery 任务文件（`api/tasks/extend/`、`api/schedule/*_extend.py`）与扩展表（`account_money_extend`、`api_token_money_extend` 四表、`api_token_message_joins_extend`、`end_user_account_joins_extend`）均已存在，无 schema 变更、无新依赖。
- **配置**：新增的 beat 任务开关沿用上游 `CeleryScheduleTasksConfig`（`api/configs/feature/__init__.py`）的 `ENABLE_*_TASK: bool` 风格，挂载到 fork 配置入口 `api/configs/extend/__init__.py`，保持 fork 配置隔离。
- **消息侧扣费链路未断**（Blinker 信号 `message_was_created` → `api/events/event_handlers/update_account_money_when_messaeg_created_extend.py`），本 change 不触碰，仅纳入回归清单验证。
- **挂点可发现性**：恢复的代码块保留「二开部分 Begin/End」注释标记，为 Phase 2 合并时的挂点搬迁提供定位锚点。

## Impact

- **api 代码**：`api/core/app/workflow/layers/persistence.py`、`api/core/app/apps/{workflow,advanced_chat}/app_generator.py`、`api/core/app/apps/advanced_chat/generate_task_pipeline.py`、`api/extensions/ext_celery.py`、`api/configs/extend/__init__.py`；删除 `api/core/workflow/workflow_cycle_manager.py` 及两个 0 字节文件；提交 6 个冲突修复文件。
- **运行时行为**：workflow 扣费与密钥额度累计恢复；celery beat 新增 3 个 extend 任务（默认开启，可经配置关闭）。
- **数据库/前端**：无变更。
- **文档/git**：新增回归清单文档（`docs/dify-plus/` 下）；新增 tag `fork-pre-merge-1.15.0`。
- **下游依赖**：Phase 2（合并 1.14.2/1.15.0）以本 change 产出的挂点清单与回归基线为对照物；未完成前禁止开始上游合并。
