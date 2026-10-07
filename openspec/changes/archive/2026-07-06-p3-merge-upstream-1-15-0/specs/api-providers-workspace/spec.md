## ADDED Requirements

### Requirement: fork 引用的 vdb import 路径修正

上游把 `api/core/rag/datasource/vdb/` 的 66 个具体 provider 迁至 `api/providers/vdb/*`（`api/core/rag/datasource/vdb/` 仅保留基类/工厂/注册表）。fork 代码中引用旧具体 provider 路径的 import SHALL 全部修正为新路径；合并后 MUST 无孤儿 import。

#### Scenario: 应用可加载

- **WHEN** 执行 `uv run --project api python -c "from app_factory import create_app; create_app()"`
- **THEN** 无 ImportError/ModuleNotFoundError，应用工厂创建成功

#### Scenario: 无旧路径残留引用

- **WHEN** 扫描 fork 侧代码对已迁移 provider 模块的 import
- **THEN** 不存在指向 `api/core/rag/datasource/vdb/` 下已迁走 provider 的引用

### Requirement: uv workspace 多包构建对齐

api 侧 SHALL 对齐上游 uv workspace 结构：`api/pyproject.toml` 的 `[tool.uv.workspace]`（`providers/vdb/*`、`providers/trace/*`）与 `dify-agent` editable 包生效；`api/uv.lock` 基于上游重生成并包含 fork 增补依赖；Python 版本收窄为 `~=3.12.0`。

#### Scenario: 本地依赖同步成功

- **WHEN** 在 Python 3.12 环境执行 `uv sync --project api`
- **THEN** 全部 workspace 成员与 fork 增补依赖安装成功

#### Scenario: api 单测通过

- **WHEN** 运行 api 单元测试套件
- **THEN** 测试通过，无因拆包或依赖升级引入的失败

### Requirement: fork 定制 Dockerfile 对齐 workspace 构建

`api/Dockerfile` SHALL 以上游 1.15.0 版本为骨架重做：采纳 uv workspace 多包拷贝/安装顺序与 Python 3.12 基镜像，同时保留 fork 定制（阿里云镜像源、`requirements.docker.txt` 额外依赖），定制内容 MUST 以集中"补丁块"形式存在以便下次升级叠加。

#### Scenario: api 镜像构建并启动

- **WHEN** 构建 fork 的 api 镜像并经 `docker/docker-compose.dify-plus.yaml` 启动
- **THEN** 镜像构建成功，api/worker 容器正常启动，`requirements.docker.txt` 中依赖在容器内可用
