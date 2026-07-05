# Admin 管理后台迁移状态总表

> 本文追踪从 Gin-Vue-Admin (GVA) 框架迁移到 Dify 原生技术栈（Next.js + Flask）的进度。
> 最终目标：完全抛弃 GVA 管理后台。

## 迁移完成状态

### ✅ 已迁移到 Dify 原生技术栈

| 功能 | GVA 路由 | Dify 原生 API | 前端页面 | 状态 |
|------|----------|--------------|---------|------|
| **钉钉 SSO 配置** | `GET/POST /gaia/system/dingtalk` | `GET/POST /console/api/system-manage-extend/integration/dingtalk` | `/system-manage-extend/system-integration` | ✅ 已验证 |
| **钉钉连接测试** | `GET /gaia/system/dingtalk/test-auth-url` | `GET /console/api/system-manage-extend/integration/dingtalk/test` | 同上 | ✅ 已验证 |
| **OAuth2.0 配置** | `GET/POST /gaia/system/oauth2` | `GET/POST /console/api/system-manage-extend/integration/oauth2` | 同上 | ✅ 已验证 |
| **邮箱 API 配置** | `POST /gaia/system/dingtalk/test-email-config` | `POST /console/api/system-manage-extend/integration/email-api/test` | 同上 | ✅ 已验证 |
| **转发 Token 管理** | `GET/POST/DELETE /gaia/system/forward-tokens` | `GET/POST/DELETE /console/api/system-manage-extend/forward-tokens` | 同上 | ✅ 已验证 |
| **用户额度管理** | `GET /gaia/quota/getManagementList`, `POST /gaia/quota/setUserQuota` | `GET /console/api/system-manage-extend/quota-management`, `POST .../set` | `/system-manage-extend/quota-management` | ✅ 已验证 |

### 🔴 需要迁移（当前 Dify 前端依赖 GVA）

| 功能 | GVA 路由 | 当前调用方 | 迁移优先级 | 说明 |
|------|----------|-----------|-----------|------|
| **批量工作流处理** | `POST /gaia/workflow/batch/processing` 等 8 个端点 | ~~`web/service/web-extend.ts`~~ **已无调用方** | ~~高~~ **已处置** | 决策点 D2（2026-07-05）定案方案 A：前端能力已删除（`web-extend.ts` 等已移除），Dify 前端对 GVA 的运行时依赖清零；Go 侧实现随 P5 删除，数据表冷备保留（见[归档说明](./批量工作流数据表冷备归档说明.md)） |

### 🟡 仅 GVA 管理端使用（无 Dify 前端依赖）

| 功能 | GVA 路由 | 迁移优先级 | 说明 |
|------|----------|-----------|------|
| **Dashboard 分析报表** | `GET /gaia/dashboard/*` (5 个端点) | 中 | 账号额度排行、App 消费排行、Token 日消费等运营分析 |
| **租户目录** | `GET /tenants/*` (3 个端点) | 中 | 全平台工作区列表，当前 Console 仅支持当前工作区 |
| **模型供应商网关** | `GET/POST /gaia/model-provider/*`, `ANY /gaia/proxy/*` | 中-高 | 统一 LLM 网关：管理供应商配置、OpenAI 兼容代理 |
| **转发代理（公开）** | `ANY /gaia/forward/proxy/*` | 高（如在用） | 钉钉/外部客户端的 OpenAI 兼容转发，无 JWT |
| **应用版本管理** | `GET/POST /gaia/app-version/*` | 低-中 | 桌面客户端发布管理 |
| **测试/DB 同步** | `POST/GET /gaia/test/*` | 低 | 开发运维工具 |
| **Worker Pool 管理** | `GET/POST /gaia/workflow/worker-pool/*` | 低 | 批量工作流后台工人池管理 |

## 本次修复的问题

1. **`system_integration_extend` 序列名不匹配** — 模型定义引用 `system_integration_id_sequence`，但数据库实际为 `system_integration_extend_id_seq`，已修正模型文件。
2. **前端构建错误** — 修复了 7 类上游合并冲突导致的编译错误：
   - `config/index.ts` 重复导出 `API_PREFIX`
   - `app-context-provider.tsx` 重复定义 `AppContext` 和占位符
   - `ToastContext` 在上游重命名后的引用适配
   - `text-generation/index.tsx` 二开代码与 `useTextGenerationBatch` hook 重构的变量冲突
   - `explore/sidebar` 中 `ExploreContext` 已被上游移除但二开代码仍引用
   - 缺失依赖：`dingtalk-jsapi`、`papaparse`、`jschardet`、`serwist`、`esbuild-wasm`
   - 缺失组件：`@/app/components/base/slider` 封装适配层

## 前端管理入口

- **入口位置**：顶部 Header 菜单 → "系统管理"
- **权限控制**：仅 workspace owner（`isCurrentWorkspaceOwner`）可见
- **路由**：`/system-manage-extend/system-integration`（系统集成）、`/system-manage-extend/quota-management`（用户额度）

## 后端权限控制

- 装饰器：`@system_admin_required_extend`
- 权限判断：`current_user.is_admin_or_owner`（admin 或 owner 角色均可访问）
- 注意：前端导航入口仅对 owner 开放，后端 API 对 admin 和 owner 都开放，存在轻微不一致

## 下一步行动

1. ~~**批量工作流迁移**~~ 已完成处置（2026-07-05，D2 方案 A 删除）：前端批量能力已移除，Dify 前端对 GVA 的最后运行时依赖已消除；Go 侧代码与统计脚本随 P5（`p5-admin-decommission`）清理
2. **Dashboard 分析**（中优先级）：将运营分析报表迁移到 Console
3. **模型供应商网关评估**：确定是否需要迁移全局 LLM 代理到 Dify 原生
4. **清理 GVA 残留**：迁移完成后移除 `admin/` 目录和相关 Docker 配置
