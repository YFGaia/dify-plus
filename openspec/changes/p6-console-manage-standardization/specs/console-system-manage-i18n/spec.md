# console-system-manage-i18n

系统管理页面文案国际化：硬编码文案清零，extend.json 20 语言全量覆盖。

## ADDED Requirements

### Requirement: 系统管理页面无硬编码文案

quota-management 与 system-integration 页面渲染的全部用户可见文案 SHALL 经 `useTranslation` 从 `extend` namespace 读取；quota 页分页控件文案（原"每页/条，共 N 条/第 X 页"）与 email-api 说明段落（原硬编码中文）MUST 进入 i18n 体系。若分页文案由 `base/pagination` 组件自带 i18n 提供，则 SHALL 复用组件既有 key，不重复新增。

#### Scenario: 硬编码中文清零

- **WHEN** 执行 `rg -n "[\u4e00-\u9fff]" web/app/\(commonLayout\)/system-manage-extend/ --glob '!**/__tests__/**'`（排除注释行后核对）
- **THEN** JSX 渲染内容中无硬编码中文字符串

#### Scenario: email-api 说明多语言展示

- **WHEN** 用户以 en-US locale 打开 system-integration 的 email-api 面板
- **THEN** 说明段落展示英文翻译而非中文原文

### Requirement: extend.json 20 语言全量覆盖

新增 i18n key SHALL 写入 `web/i18n/{locale}/extend.json` 全部 20 个 locale；SHALL 以 zh-Hans 与 en-US 为人工基准，其余 locale MAY 由 `web/i18n-config/auto-gen-i18n.js` 工具链生成。

#### Scenario: i18n 完整性检查通过

- **WHEN** 执行 `pnpm run i18n:check`（`check-i18n.js`）
- **THEN** extend namespace 无缺失 key 报告
