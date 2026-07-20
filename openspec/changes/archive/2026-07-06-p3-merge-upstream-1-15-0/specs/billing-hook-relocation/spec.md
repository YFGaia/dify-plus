## ADDED Requirements

### Requirement: 计费挂点在 1.15.0 结构上全部重新定位

沿用 P2 更新过的计费挂点清单，全部挂点 SHALL 在 1.15.0 代码结构上重新核对并落位（含 `persistence.py` 节点计费派发、两个 `app_generator.py` 的 `app_token_id` 写入、`generate_task_pipeline.py` 关联记录、`service_api/wraps.py` 校验、`apikey.py` 联查、`ext_celery.py` beat 注册等）；新坐标 MUST 登记回 `docs/dify-plus/与上游差异总表.md`，代码块 MUST 保留「二开部分 Begin/End」标记。

#### Scenario: 挂点清单逐项核对完成

- **WHEN** 合并修复完成后对照挂点清单逐项检查
- **THEN** 每个挂点在 1.15.0 代码中有明确落位（原位保留或已搬迁），差异总表坐标为最新

### Requirement: 6 条计费回归链路全绿

合并完成后 SHALL 通过 P0 建立的 6 条计费回归链路：Console 调试运行扣费、Explore 运行扣费、WebApp 登录+扣费、Service API 密钥日/月限额拦截、workflow LLM 节点扣费、月初额度重置（beat 任务注册在位）。

#### Scenario: workflow 节点扣费

- **WHEN** 手动触发含 LLM 节点的 workflow 运行成功
- **THEN** `account_money_extend.used_quota` 出现对应增量

#### Scenario: 密钥限额拦截

- **WHEN** Service API 密钥的日/月已用额度达到上限后再次调用
- **THEN** 请求被限额校验拦截并返回额度受限错误

#### Scenario: 重置任务在位

- **WHEN** 查看 celery beat 调度列表
- **THEN** 3 个 extend 额度重置任务（月度用户额度、日/月密钥额度）均已注册
