# fork-extension-compatibility Specification

## Purpose

TBD - created by archiving change p2-merge-upstream-1-14-2. Update Purpose after archive.

## Requirements

### Requirement: extend service 适配显式 session 与 select() 模式

fork 的 `*_extend` service 中调用上游被重构 service（FeedbackService、TagService、FileService、ApiKeyAuthService 等）的代码 SHALL 按 1.14.2 新签名（显式传 `session`、SQLAlchemy 2.0 `select()`）改写，SHALL NOT 通过适配层保留旧调用方式。

#### Scenario: 无旧签名调用残留

- **WHEN** 合并后运行 py_compile 与应用导入冒烟，并审查 `*_extend` service 对上游 service 的调用点
- **THEN** 无因签名不匹配产生的 TypeError/AttributeError 隐患，无针对旧签名的包装函数

#### Scenario: 事务边界不回归

- **WHEN** extend service 改为显式 session 后执行计费写入路径
- **THEN** 扣费与业务数据写入的事务边界与合并前一致，无双写或漏提交

### Requirement: extend controller 适配 user/tenant 注入模式

fork 的 extend console controller 及被 fork 侵入的上游 console controller SHALL 跟进 1.14.2 的注入式 user/tenant 获取模式。

#### Scenario: extend 管理接口可用

- **WHEN** 登录后访问 system-manage-extend 相关后端接口（额度管理、系统集成等）
- **THEN** 接口正常返回，权限装饰器 `@system_admin_required_extend` 在新模式下正常生效

### Requirement: extend 模型类外迁独立文件

`api/models/model.py` 中内嵌的 3 个 extend 模型类 SHALL 外迁到独立的 `api/models/*_extend.py` 文件，且 SHALL 保持导入兼容（表结构与既有 import 路径均不破坏）。

#### Scenario: 外迁后导入兼容

- **WHEN** 外迁完成后执行全量 py_compile 与应用导入冒烟
- **THEN** 所有引用这 3 个模型的代码（含 `models/__init__.py` 导出）正常工作，无 schema 变化

#### Scenario: model.py 侵入清零

- **WHEN** 对比外迁后的 `api/models/model.py` 与 upstream 1.14.2 同文件
- **THEN** fork 侧不再有内嵌 extend 模型类的差异（其余必要差异除外）

### Requirement: fork 安全防护包装保留

`controllers/console/feature.py` 的 CVE 防护包装与 web 侧 `contract/router.ts` 的 CVE 防护合约 SHALL 在合并后保留。

#### Scenario: 防护层存在性检查

- **WHEN** 审查合并后的 `api/controllers/console/feature.py` 与 `web/contract/router.ts`
- **THEN** fork 的 CVE 防护逻辑完整保留且与上游新增合约共存

### Requirement: web 侧 extend 挂载保持有效

web 侧 fork 挂载点（`contract/console/system.ts` 扩展合约、`context/app-context*` 扩展字段、`config/index.ts`、`i18n-config` 的 extend namespace 注册）SHALL 在 1.14.2 合并后保持有效。

#### Scenario: 前端构建与扩展功能可用

- **WHEN** web 构建通过后访问依赖 extend 挂载的页面（应用中心、系统管理、额度展示）
- **THEN** 页面渲染正常，extend i18n 文案正确加载，无 console 运行时错误

### Requirement: 账号初始化额度逻辑保留

`api/services/account_service.py` 中 fork 的账号额度初始化逻辑 SHALL 在上游重构后保留。

#### Scenario: 新账号获得初始额度

- **WHEN** 新用户完成注册/首次登录
- **THEN** `account_money_extend` 生成该账号的初始额度记录
