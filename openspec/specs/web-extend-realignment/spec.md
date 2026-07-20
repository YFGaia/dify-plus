# web-extend-realignment Specification

## Purpose

TBD - created by archiving change p3-merge-upstream-1-15-0. Update Purpose after archive.

## Requirements

### Requirement: text-generation 按 D2 决策分叉处理

`web/app/components/share/text-generation/index.tsx`（fork +719 行批量逻辑）SHALL 按 D2 决策结论处理：若 D2 决定删除批量功能，则以上游 1.15.0 版本为准、不搬迁批量逻辑（仅保留其余 fork 小改）；若 D2 决定保留，则批量逻辑 MUST 对位到上游重写后的组件结构且批量运行功能可用。执行前 MUST 取得 D2 书面结论。

#### Scenario: D2 为删除

- **WHEN** D2 结论为删除批量功能且合并完成
- **THEN** 该文件与上游 1.15.0 基本一致，批量 tab 不再出现，页面单次运行功能正常

#### Scenario: D2 为保留

- **WHEN** D2 结论为保留且合并完成
- **THEN** 批量运行入口与执行流程与迁移前行为一致

### Requirement: contract 与 app-context 侵入重对位

fork 对 `web/contract/router.ts`、`web/contract/console/system.ts`、`web/context/app-context*` 的侵入 SHALL 在上游 1.15.0 重写（含 contract 迁入 `packages/contracts`）后重新对位，fork 的 contract 注册与 context 扩展字段功能不回退。

#### Scenario: 系统管理接口可用

- **WHEN** 打开依赖 fork contract（如 system 集成配置）的页面
- **THEN** 请求经重对位后的 contract 路由成功返回，页面数据正常

#### Scenario: context 扩展字段可用

- **WHEN** 组件读取 fork 在 app-context 中的扩展字段
- **THEN** 字段值正常提供，类型检查通过

### Requirement: i18n extend namespace 与 uk-UA 策略统一

fork 的 i18n extend namespace 注册（`i18n-config/i18next-config.ts`、`resources.ts`）SHALL 在上游 1.15.0 的 i18n 结构上恢复生效；`i18n/uk-UA` 的处理 MUST 与 fork 既定策略统一（防止上游更新使其"复活"造成策略不一致）。

#### Scenario: extend 文案正常渲染

- **WHEN** 切换任一支持语言并访问包含 extend namespace 文案的页面
- **THEN** 二开文案正常渲染，无 i18n key 裸露

#### Scenario: uk-UA 策略一致

- **WHEN** 检查合并后 `i18n/uk-UA` 目录状态
- **THEN** 其存在与内容符合 fork 既定策略，无策略不一致残留
