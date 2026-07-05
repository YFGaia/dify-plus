# billing-config-unification

计费配置单一来源：汇率经后端 API 下发前端、初始额度统一读取 `ACCOUNT_TOTAL_QUOTA`，消除硬编码。

## ADDED Requirements

### Requirement: 汇率经后端 API 下发

`GET /console/api/account/money`（`AccountMoneyApi`）响应 MUST 包含 `rmb_to_usd_rate` 字段，值取自 `dify_config.RMB_TO_USD_RATE`。前端额度徽章组件（`account-money-extend` 或 p3 迁移后的 main-nav 等价组件）MUST 使用该字段进行美元→人民币换算，MUST NOT 保留硬编码汇率常量 `6.97`；响应缺失该字段时 SHALL 回退到内置默认值并保持组件可渲染。

#### Scenario: 前端展示使用后端汇率

- **WHEN** 后端配置 `RMB_TO_USD_RATE = 7.26` 且用户 `used_quota = 1`（USD）
- **THEN** 额度徽章展示已用金额 ¥7.26，而非按 6.97 计算的值

#### Scenario: 响应缺字段时的兼容回退

- **WHEN** 前端收到不含 `rmb_to_usd_rate` 的旧版响应（滚动发布窗口）
- **THEN** 组件使用默认汇率渲染，不报错、不空白

### Requirement: 初始额度统一读取配置

`api/services/billing_extend.py::calculate_user_billing_information` 在为无额度记录的账号建档时，`total_quota` MUST 取 `dify_config.ACCOUNT_TOTAL_QUOTA`，MUST NOT 使用硬编码常量 `15`。代码库内（api 计费链路）SHALL 不再存在初始额度魔法数字。

#### Scenario: 转发计费建档使用配置额度

- **WHEN** `ACCOUNT_TOTAL_QUOTA = 100`，一个无 `account_money_extend` 记录的账号首次经转发计费扣费
- **THEN** 新建记录的 `total_quota` 为 100

#### Scenario: 与消息扣费建档口径一致

- **WHEN** 同一配置下分别经消息扣费链路与转发计费链路为两个新账号建档
- **THEN** 两条记录的 `total_quota` 相同，均为 `ACCOUNT_TOTAL_QUOTA`
