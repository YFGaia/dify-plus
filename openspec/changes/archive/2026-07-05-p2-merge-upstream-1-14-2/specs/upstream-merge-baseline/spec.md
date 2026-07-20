# upstream-merge-baseline: 上游 1.14.2 合并基线

合并到 upstream 1.14.2 后，fork 基线（`merge/upstream-1.14.2` 分支）必须满足的整体质量要求。

## ADDED Requirements

### Requirement: 合并结果无冲突残留

合并提交后的代码树 SHALL 不包含任何未解决的冲突标记，也 SHALL 不包含因合并产生的孤儿文件（上游已删除/重命名但 fork 侧仍残留旧路径副本的文件）。

#### Scenario: 冲突标记扫描

- **WHEN** 在合并提交上执行 `git grep -l '<<<<<<<' -- api/ web/`
- **THEN** 输出为空

#### Scenario: dify_graph 副本无孤儿

- **WHEN** 上游在 1.14.2 中移动或重命名了 fork 曾复制过的模块（如 `api/dify_graph/` 下的 code 节点控制副本）
- **THEN** fork 侧对应旧路径文件被删除或同步迁移，仓库中不存在新旧两份并存的副本

### Requirement: 后端可构建可导入

合并后的 api 侧 SHALL 通过全量语法编译与应用工厂导入冒烟。

#### Scenario: py_compile 全量通过

- **WHEN** 对 `api/` 下全部 Python 文件执行 `py_compile`
- **THEN** 无编译错误

#### Scenario: 应用工厂导入

- **WHEN** 执行 `uv run --project api python -c "from app_factory import create_app"`
- **THEN** 命令以退出码 0 结束，无 ImportError

### Requirement: 双 Alembic 迁移链并存可升级

上游迁移链与 fork 独立迁移链 SHALL 均能成功执行，且互不干扰。

#### Scenario: 上游迁移执行

- **WHEN** 在合并后的代码上执行 `uv run --project api flask db upgrade`
- **THEN** 上游 1.13.3→1.14.2 新增的 3 个迁移全部应用成功

#### Scenario: fork 扩展迁移链完好

- **WHEN** 上游迁移完成后执行 `uv run --project api flask extend_db upgrade`
- **THEN** `api/migrations_extend` 链执行成功（已是最新时为 no-op），全部 `*_extend` 表结构不变

### Requirement: quota v3 与 fork 额度体系物理隔离

上游 quota v3（`quota_reserve`/`quota_commit`/`quota_release` 及 tenant 级 Redis 锁）SHALL 按上游原样合入，且 fork 代码 SHALL NOT 调用这些接口；fork 自建额度表体系 SHALL 保持结构与语义不变（决策 D1）。

#### Scenario: fork 代码零引用 quota v3

- **WHEN** 在合并结果上搜索 fork 侧文件（`*_extend*` 及 fork 侵入的上游文件中 fork 添加的代码段）对 `quota_reserve|quota_commit|quota_release` 的引用
- **THEN** 无任何匹配

#### Scenario: fork 额度表结构不变

- **WHEN** 对比合并前后 `account_money_extend`、`api_token_money_extend` 相关四表及 `forwarding_*` 表的模型定义
- **THEN** 字段与语义完全一致

### Requirement: Python 与依赖对齐上游且保留 fork 专属依赖

合并后的依赖声明 SHALL 与 upstream 1.14.2 对齐（含 Python 3.12 走向），同时 SHALL 保留 fork 专属依赖。

#### Scenario: fork 依赖保留

- **WHEN** 检查合并后的 `api/pyproject.toml` 与 `api/uv.lock`
- **THEN** fork 专属依赖（如 `alibabacloud_dingtalk`、`pypinyin`）仍存在且可解析安装

#### Scenario: 构建文件同步

- **WHEN** 检查 `api/Dockerfile` 与 `api/requirements.docker.txt`
- **THEN** 基础镜像 Python 版本与依赖清单和 `pyproject.toml`/`uv.lock` 一致，Docker 构建可通过

### Requirement: 前端可构建

合并后的 web 侧（1.14.2 阶段仍为单包结构）SHALL 通过 lint、类型检查与生产构建。

#### Scenario: web 质量门禁

- **WHEN** 在 `web/` 下依次执行 `pnpm lint`、`pnpm type-check:tsgo`、`pnpm build`
- **THEN** 三者均以退出码 0 结束

### Requirement: 核心功能回归全绿

合并定稿前 SHALL 通过 fork 核心功能回归：P0 建立的 6 条计费链路、SSO 登录（钉钉/OAuth2/Casdoor）、应用中心（`/explore/apps-center-extend`）、系统管理页（`/system-manage-extend`）。

#### Scenario: 计费回归清单

- **WHEN** 执行 P0 建立的 6 条计费回归链路（Console 调试扣费、Explore 扣费、WebApp 登录+扣费、Service API 日/月限额拦截、workflow LLM 节点扣费、月初额度重置）
- **THEN** 6 条链路全部通过

#### Scenario: SSO 与页面回归

- **WHEN** 分别通过钉钉、OAuth2、Casdoor 登录，并访问应用中心与系统管理页
- **THEN** 登录成功、页面加载与核心交互无回归
