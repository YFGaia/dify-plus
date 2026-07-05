# billing-hook-registry

计费挂点注册表：全部计费侵入点坐标入档，作为每次上游升级的强制 checklist。

## ADDED Requirements

### Requirement: 挂点注册表章节入档

`docs/dify-plus/与上游差异总表.md` MUST 新增「计费挂点注册表」章节，登记全部计费/额度侵入点（约 21 处，以本 change 完成后的实际代码为准盘点）。每个条目 MUST 包含：挂点文件路径、函数/位置、被 hook 的上游函数或扩展点名、用途分类（归因/校验/扣费/重置/治理）、对应回归链路（p0 建立的 6 条计费回归链路编号，或标注不适用）、最近核对的上游版本。

#### Scenario: 条目信息完整

- **WHEN** 查阅注册表中任一条目
- **THEN** 可直接定位到挂点代码位置、知道它 hook 了上游哪个函数、属于哪类用途、用哪条链路回归

#### Scenario: 覆盖全部侵入点

- **WHEN** 用 `rg` 按 `extend`/「二开部分」标记扫描 api 计费相关文件并与注册表比对
- **THEN** 无遗漏的计费侵入点（新增挂点必须同步登记）

### Requirement: 注册表反映本 change 改造后的坐标

注册表条目 MUST 以 p4 改造完成后的代码为准：`wraps.py` 条目 SHALL 记录 contextvar 方案与 `api/libs/api_token_context_extend.py`；9 个 service_api controller SHALL 标注「签名已归零，无侵入」；新增的幂等键、缓存、归档任务 SHALL 作为新条目登记。

#### Scenario: 升级 checklist 可用性

- **WHEN** 下次上游合并时按注册表逐条核对
- **THEN** 每个挂点可判定「上游未动/上游重构需搬迁/fork 可删除」，核对完成后更新「最近核对版本」列

### Requirement: 长期技术债项登记

注册表 MUST 附带长期跟踪项清单，至少包含：扣费金额 float 精度问题（评估迁移 Decimal）、`events/__init__.py` 自带 Events 框架与上游漂移风险、message handler 同步扣费形态的演进评估。

#### Scenario: 技术债不失踪

- **WHEN** 后续规划新的加固 change
- **THEN** 可从注册表长期项清单直接取得待办与背景，无需重新调研
