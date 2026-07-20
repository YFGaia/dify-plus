## ADDED Requirements

### Requirement: 二开组件零 headlessui 残留

合并 1.15.0 后（上游 `web/package.json` 已移除 `@headlessui/react`），全部 fork 二开组件 SHALL 不再 import `@headlessui/react`；已知候选（`invite-modal`、`system-manage-extend` 各页面、`auto-select-extend`、`react-multi-email-extend`）及扫描发现的其余 fork 文件 MUST 全部完成替换，且 MUST NOT 把 `@headlessui/react` 重新加回依赖。

#### Scenario: 全仓扫描归零

- **WHEN** 在合并分支执行 `rg '@headlessui/react' web/`
- **THEN** 无任何匹配结果，且 `web/package.json` 与根锁文件中不含该依赖

### Requirement: 替换使用 dify-ui overlay 原语且行为等价

headlessui 的 Dialog/Transition/Popover/Listbox 等用法 SHALL 替换为 `packages/dify-ui` 的 overlay 原语或上游同用途 base 组件；替换 MUST 保持交互行为等价（打开/关闭、焦点管理、Esc/遮罩关闭、选项选择），不做视觉重构。

#### Scenario: 邀请成员弹窗行为等价

- **WHEN** 在成员管理页打开 invite-modal 并完成一次邀请操作
- **THEN** 弹窗打开/关闭、多邮箱输入、角色选择与提交流程与迁移前一致

#### Scenario: 系统管理页交互正常

- **WHEN** 在 `/system-manage-extend/` 各页面操作原 headlessui 承载的弹窗/下拉控件
- **THEN** 交互功能正常且 `pnpm type-check:tsgo` 无相关类型错误
