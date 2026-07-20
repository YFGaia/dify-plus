## ADDED Requirements

### Requirement: 分类数据一次性迁移到上游原生列

系统 SHALL 提供一次性数据迁移（`migrations_extend` 新版本）：把 fork 的 `recommended_category_extend` / `recommended_apps_category_join_extend` 两表中的分类关系写入上游 `recommended_apps.categories` JSON 列（该列的 schema 迁移 `a4f2d8c9b731` 在 1.14.2 已存在）。迁移脚本 MUST 幂等——重复执行不产生重复分类项。

#### Scenario: 数据完整迁移

- **WHEN** 执行 `flask extend_db upgrade` 跑到分类数据迁移版本
- **THEN** 每个原有分类关联的 recommended app 其 `categories` 列包含对应分类名，SQL 抽验分类计数与旧表一致

#### Scenario: 重复执行幂等

- **WHEN** 分类数据迁移逻辑对同一数据重复执行
- **THEN** `categories` 列中不出现重复分类项

### Requirement: 探索页读上游原生数据源

迁移后，explore 分类的唯一数据源 SHALL 是 `recommended_apps.categories`：api 侧删除 `database_retrieval.py` 的 fork 侵入、改走上游原生查询路径；web 侧 explore 相关组件（sidebar、app-card 等 fork 改动处）改读上游 categories 数据。

#### Scenario: 探索页分类展示一致

- **WHEN** 用户打开探索页（应用中心）
- **THEN** 分类侧边栏与分类过滤结果和迁移前一致，数据来自 `recommended_apps.categories`

#### Scenario: 侵入代码已删除

- **WHEN** 检查合并后 api 代码
- **THEN** `database_retrieval.py` 的 fork 分类侵入已删除，无对 fork 两张分类表的运行时读依赖

### Requirement: fork 分类表分步退役

fork 两张分类表 SHALL 经 `migrations_extend` 的独立 drop 迁移版本删除；drop 版本 MUST 与数据迁移版本分离，仅在探索页回归验证通过后执行，保留暂缓余地。

#### Scenario: 回归通过后 drop

- **WHEN** 探索页分类回归通过后执行 drop 迁移
- **THEN** `recommended_category_extend` 与 `recommended_apps_category_join_extend` 两表被删除，应用运行无报错

#### Scenario: drop 可独立暂缓

- **WHEN** 探索页回归未通过
- **THEN** 可只回退代码切换而不执行 drop 迁移，旧表数据完整保留
