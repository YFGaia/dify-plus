# console-system-manage-ui

系统管理页面 UI 规范：base（dify-ui）组件族复用、overlay 原语、Confirm 删除确认、toast 单轨化、错误类型化。

## ADDED Requirements

### Requirement: 页面 UI 复用 base 组件族

quota-management 与 system-integration 页面的表格、分页、输入框、按钮、开关、下拉选择、tab 切换 SHALL 复用 `web/app/components/base/` 下的既有组件（`pagination`、`input`、`button`、`switch`、`select`、tab 组件族等），MUST NOT 以原生元素 + Tailwind 手写等价物；组件选型以 1.15.0 合并后的 base（dify-ui）组件族为准。

#### Scenario: quota 列表分页控件复用 base/pagination

- **WHEN** quota-management 页渲染分页区域
- **THEN** 分页由 `base/pagination` 组件渲染，支持 page_size 10/30/50/100 切换，行为与原手写实现等价（页码从 1 起）

#### Scenario: system-integration tab 复用 base tab 组件

- **WHEN** 用户在 system-integration 页切换 dingtalk/oauth2/email-api/forward-token 面板
- **THEN** tab 由 base tab 组件渲染并以本地 state 切换，面板内容与切换行为不变

### Requirement: 弹窗与确认交互使用规范 overlay 原语

编辑额度弹窗 SHALL 使用 `@/app/components/base/ui/*` 的 overlay 原语（如 `ui/dialog`）实现，MUST NOT 使用 `fixed inset-0` 手写遮罩；forward-token 删除确认 SHALL 使用 `base/confirm` 组件，MUST NOT 使用 `globalThis.confirm`；新增或修改代码 MUST NOT 引入 deprecated overlay import。

#### Scenario: 额度编辑弹窗

- **WHEN** 用户点击某行"编辑"
- **THEN** 弹窗经 overlay 原语打开，保留输入校验（非负数字）、Enter 确认、失败提示行为

#### Scenario: 删除确认

- **WHEN** 用户点击 forward-token 行的"删除"
- **THEN** 出现 `base/confirm` 确认弹窗，确认后才发起删除请求；`rg "globalThis.confirm" web/app/\(commonLayout\)/system-manage-extend/` 无匹配

### Requirement: Toast 单轨化与错误类型化

系统管理全部页面 SHALL 统一使用新 `@/app/components/base/ui/toast`，MUST NOT 引用旧 `@/app/components/base/toast`；错误处理 MUST NOT 使用 `catch (e: any)`，SHALL 通过 query/mutation 的 `error: Error` 通道或 `error instanceof Error` 收窄获取错误消息。

#### Scenario: 旧 toast API 清零

- **WHEN** 执行 `rg "base/toast" web/app/\(commonLayout\)/system-manage-extend/`
- **THEN** 无任何匹配

#### Scenario: catch any 清零

- **WHEN** 执行 `rg "catch \(e: any\)|catch \(error: any\)" web/app/\(commonLayout\)/system-manage-extend/`
- **THEN** 无任何匹配
