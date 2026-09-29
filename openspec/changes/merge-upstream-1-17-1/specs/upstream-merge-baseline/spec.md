# 1.17.1 合并基线变更

## MODIFIED Requirements

### Requirement: 合并结果无冲突残留

合并后的代码树 SHALL 无未解决冲突和因上游删除/重命名产生的孤儿宿主；所有 fork 行为 MUST 在新版本实际调用链中可达，并保留当前 fork 基线 tag 后四次提交的效果。

#### Scenario: 冲突标记扫描

- **WHEN** 检查最终合并树和实际路由/页面调用链
- **THEN** 无未解决索引、冲突标记或不再被调用的旧宿主；每应用 WebApp 认证开关与匿名上下文修复在新页面生效

#### Scenario: dify_graph 副本无孤儿

- **WHEN** 目标版本移动或删除原 fork 宿主及历史 dify_graph 副本相关模块
- **THEN** fork 行为迁入实际新宿主，不保留因合并产生的孤儿模块或新旧双源

### Requirement: 双 Alembic 迁移链并存可升级

上游与 fork 独立迁移链 SHALL 均可从支持的来源版本及空库升级且互不干扰。1.16.0 到 1.17.1 的主链 MUST 覆盖全部 17 项新增迁移并到达 `c3f1a9b2e6d4`，扩展链 MUST 到达 `020_workflow_run_account`。数据删除、归一与引用改写 SHALL 有前后审计及恢复证据。

#### Scenario: 上游迁移执行

- **WHEN** 对真实来源库的隔离副本依次执行主链和扩展链
- **THEN** 两版本表到达目标，扩展业务结构完好，17 项迁移的数据影响经过对账，异常阻断上线

#### Scenario: 空库升级

- **WHEN** 从空库执行两条完整迁移链
- **THEN** 两链均成功，初始化账号、扩展配置和核心业务可用

#### Scenario: fork 扩展迁移链完好

- **WHEN** 主链完成后运行 fork 扩展迁移
- **THEN** 独立版本表到达 020_workflow_run_account，已有最新库为无操作，全部扩展结构和数据仍满足当前业务

### Requirement: Python 与依赖对齐上游且保留 fork 专属依赖

合并后的依赖声明、锁文件与构建运行环境 SHALL 对齐固定的 upstream 1.17.1，同时保留 fork 专属依赖及其功能。Python 运行基线 SHALL 为目标要求的 3.12。

#### Scenario: fork 依赖保留

- **WHEN** 从合并后的声明与锁文件安装并构建 API 镜像
- **THEN** 钉钉、拼音等 fork 专属依赖可解析安装和导入，运行版本与构建声明一致

#### Scenario: 构建文件同步

- **WHEN** 检查 API Dockerfile、依赖声明、锁文件及最终镜像
- **THEN** Python 与依赖版本一致，fork 依赖可运行，构建不依赖过时的独立清单

### Requirement: 前端可构建

前端 SHALL 在目标 Node 24.20.0、pnpm 12.3.4 的工作区工具链下通过依赖锁定安装、静态检查、类型检查、国际化检查以及 Next 和 Vinext 生产构建。

#### Scenario: web 质量门禁

- **WHEN** 在固定候选源码执行目标工作区质量门禁与两种生产构建
- **THEN** 所有命令成功，fork 的 24 个语言扩展资源可加载，实际镜像使用验证过的构建产物

### Requirement: 核心功能回归全绿

合并定稿 SHALL 保持六条既有计费链路、SSO、应用中心、系统管理、每应用 WebApp 认证与匿名上下文修复，并覆盖目标新增的环境 WebApp 和 dataset key 权限。新增回归 MUST 修复；已有基线缺陷 SHALL 独立记录，不得把未实现的计费幂等或匿名付款人规则声明为已通过。

#### Scenario: 计费回归清单

- **WHEN** 验证 Console、Explore、登录 WebApp、Service API 额度、workflow LLM 节点与周期重置
- **THEN** 合并不新增漏扣、错误归因或重复派发，真实用量与现有规则一致；既有缺陷另附对照证据

#### Scenario: SSO 与页面回归

- **WHEN** 验证各登录提供方、built-in 公开/需登录组合、环境 WebApp、应用中心与系统管理
- **THEN** 权限和回跳正确、匿名公开应用可运行、已有管理功能有效且未扩大权限
