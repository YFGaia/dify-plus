# Admin 管理功能迁移至 Dify 原生技术栈 — 第一阶段：系统集成

## Summary

将 Go+Vue 技术栈的 Admin 管理后台中"系统集成"功能迁移至 Dify 原生 Next.js + Python 技术栈，包括钉钉 SSO 配置、OAuth2.0 配置、邮箱 API 配置和转发 Token 管理。

## Motivation

- 双技术栈维护成本高（Go/Vue + Python/Next.js）
- Admin 需独立容器部署，增加运维负担
- 管理员需在两个系统间切换，体验割裂
- 上游合并摩擦大

## Design

### 后端

- 新增 `api/controllers/console/system_manage_extend.py`：权限装饰器 + Controller 路由
- 新增 `api/services/system_manage_extend.py`：Service 层（CRUD + 测试连接）
- 复用已有 `system_integration_extend` 表，不新增数据库表
- 路由前缀 `/console/api/system-manage-extend/`
- 权限：`@system_admin_required_extend`（workspace owner 或配置管理员）

### 前端

- 新增 `web/app/(commonLayout)/system-manage-extend/` 路由目录
- 新增 `web/service/system-manage-extend.ts` API 封装
- 新增 `web/models/system-manage-extend.ts` 类型定义
- 新增 Header 导航入口（仅 owner/admin 可见）
- 新增 i18n 文件

### 低冲突策略

- 全新目录/文件，不改上游路由
- 独立命名空间 `-extend` 后缀
- 独立 i18n key
- 仅 `__init__.py` 需一行 import 合并

## Non-goals

- 不迁移用户管理、模型管理、运营管理（后续阶段）
- 不修改 Admin 自身 RBAC 体系
- 不修改已有的钉钉/OAuth2 登录逻辑
