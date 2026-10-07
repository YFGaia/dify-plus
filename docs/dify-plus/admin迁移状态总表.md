# Admin 管理后台迁移状态总表

> 本文追踪从 Gin-Vue-Admin (GVA) 框架迁移到 Dify 原生技术栈（Next.js + Flask）的进度。
> 最终目标：完全抛弃 GVA 管理后台。
>
> **✅ 已完结（2026-07-05，`p5-admin-decommission`）**：`admin/` 目录、部署资产（compose `admin-web`/`admin-server`、`docker/admin-server/`、`admin/deploy/*`）、admin 专用注册端点（`register_extend.py` + CSRF 白名单 + `ADMIN_GROUP_ID`）已全部删除，GVA 框架表由 `migrations_extend` 迁移 `016_drop_gva_admin_tables` 清理（DROP 前需 pg_dump 备份），删除前 tag `pre-admin-removal` 留档。code 节点执行控制已迁 Console（`/system-manage-extend/code-execution-control`）并恢复读侧接线。`SECRET_KEY` 轮换按 [runbook](./SECRET_KEY轮换runbook.md) 在部署环境维护窗口执行。下文保留作为迁移过程的历史记录。

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
| **代码执行控制（sandbox-full 授权名单）** | GVA 全局代码授权（`sys_user_global_code` + `SyncExecuteCode` 写 redis `control_mail`） | `GET/POST/DELETE /console/api/system-manage-extend/code-execution-control` | `/system-manage-extend/code-execution-control` | ✅ P5 迁移完成（含读侧接线恢复与存量迁移命令） |

### 🔴 需要迁移（当前 Dify 前端依赖 GVA）

| 功能 | GVA 路由 | 当前调用方 | 迁移优先级 | 说明 |
|------|----------|-----------|-----------|------|
| **批量工作流处理** | `POST /gaia/workflow/batch/processing` 等 8 个端点 | ~~`web/service/web-extend.ts`~~ **已无调用方** | ~~高~~ **已处置** | 决策点 D2（2026-07-05）定案方案 A：前端能力已删除（`web-extend.ts` 等已移除），Dify 前端对 GVA 的运行时依赖清零；Go 侧实现随 P5 删除，数据表冷备保留（见[归档说明](./批量工作流数据表冷备归档说明.md)） |

### 🟡 仅 GVA 管理端使用（无 Dify 前端依赖）——已随 admin 下线

| 功能 | GVA 路由 | 处置结论（2026-07-05） |
|------|----------|------|
| **Dashboard 分析报表** | `GET /gaia/dashboard/*` (5 个端点) | 随 admin 删除；如需运营报表按 Console 骨架另立项目（线路图 Phase 5.5） |
| **租户目录** | `GET /tenants/*` (3 个端点) | 随 admin 删除 |
| **模型供应商网关** | `GET/POST /gaia/model-provider/*`, `ANY /gaia/proxy/*` | D3 定案：无外部依赖，随 admin 删除 |
| **转发代理（公开）** | `ANY /gaia/forward/proxy/*` | D3 定案：无钉钉/外部客户端在用，随 admin 删除 |
| **应用版本管理** | `GET/POST /gaia/app-version/*` | D3 定案：无桌面客户端轮询，随 admin 删除 |
| **测试/DB 同步** | `POST/GET /gaia/test/*` | 随 admin 删除 |
| **Worker Pool 管理** | `GET/POST /gaia/workflow/worker-pool/*` | 随 admin 删除（批量工作流已按 D2 方案 A 删除） |

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

1. ~~**批量工作流迁移**~~ 已完成处置（2026-07-05，D2 方案 A 删除）：前端批量能力已移除，Dify 前端对 GVA 的最后运行时依赖已消除；Go 侧代码与统计脚本已随 P5（`p5-admin-decommission`）删除
2. ~~**清理 GVA 残留**~~ 已完成（2026-07-05，P5）：`admin/` 目录、Docker 配置、GVA 表清理迁移全部落地
3. **Dashboard 分析**（可选后续项目）：如需运营分析报表，按 Console `/system-manage-extend` 骨架 + contract 模式另立项目
4. **部署环境收尾**（生产执行项，顺序不可颠倒）：pg_dump 备份全部待 DROP 表 → `flask extend_db upgrade --revision 015_code_execution_control`（建新表）→ 执行存量迁移命令（`sys_user_global_code(s)` → `code_execution_control_extend` + redis 重建，命令名见 `api/commands/extend.py`）→ `flask extend_db upgrade`（执行 016 DROP GVA 表）→ 按 [SECRET_KEY 轮换 runbook](./SECRET_KEY轮换runbook.md) 轮换密钥
