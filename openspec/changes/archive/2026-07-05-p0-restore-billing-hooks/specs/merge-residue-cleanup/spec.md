# merge-residue-cleanup

合并残留治理：冲突标记清零、幽灵文件与死代码清理，保证代码库可编译、无孤儿 import。

## ADDED Requirements

### Requirement: 代码库不得残留未解决冲突标记

仓库 HEAD 中 `api/` 与 `web/` 目录下的源码文件 SHALL 不包含任何 git 冲突标记（`<<<<<<<`、`=======`、`>>>>>>>` 三元组）。当前工作区对 6 个文件（`api/core/workflow/nodes/knowledge_index/{__init__.py,entities.py,knowledge_index_node.py}`、`api/core/workflow/nodes/knowledge_retrieval/{knowledge_retrieval_node.py,retrieval.py}`、`api/core/workflow/nodes/trigger_plugin/trigger_event_node.py`）的冲突修复 MUST 被固化提交。

#### Scenario: 冲突标记全量检查通过

- **WHEN** 在提交冲突修复后的 HEAD 上运行 `git grep -lI '<<<<<<<' -- api/ web/`（排除二进制文件）
- **THEN** 输出为空，命令返回无源码文件命中

#### Scenario: 修复后的模块可正常导入

- **WHEN** 运行 `uv run --project api python -c "from app_factory import create_app"`
- **THEN** 导入成功，无 SyntaxError 或 ImportError

### Requirement: 幽灵文件与死代码 SHALL 被清理

合并过程中产生的无调用方残留文件 SHALL 被删除：`api/core/workflow/workflow_cycle_manager.py`（旧路径幽灵文件，内含已无调用方的计费 hook）、两个 0 字节空文件（`api/controllers/service_api/dataset/upload_file.py`、`api/core/model_runtime/model_providers/bedrock/llm/llm.py`）。`api/core/workflow/nodes/node_mapping.py` MUST 完成去留确认：确认为残留则与配套测试 `api/tests/unit_tests/core/workflow/test_node_mapping_bootstrap.py` 同批删除，否则保留并记录结论。

#### Scenario: 删除幽灵文件后无孤儿 import

- **WHEN** 删除上述文件后运行全仓检索 `rg "workflow_cycle_manager"` 与 `rg "core.workflow.nodes.node_mapping"`，并执行 `uv run --project api python -c "from app_factory import create_app"`
- **THEN** 检索无业务代码命中（文档除外），应用可正常创建

#### Scenario: 计费任务引用唯一化

- **WHEN** 删除 `workflow_cycle_manager.py` 并恢复 persistence.py 挂点后，运行 `rg -l "update_account_money_when_workflow_node_execution_created_extend" api/ --glob '!tests'`
- **THEN** 命中仅剩任务定义文件 `api/tasks/extend/update_account_money_when_workflow_node_execution_created_extend.py` 与派发点 `api/core/app/workflow/layers/persistence.py` 两处
