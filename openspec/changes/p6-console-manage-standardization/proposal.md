# Proposal: Console 系统管理入口规范化（p6-console-manage-standardization）

## Why

Console 系统管理入口（`/system-manage-extend/`，含用户额度管理 quota-management 与系统集成 system-integration 两组页面）是 admin（gin-vue-admin）废弃后的统一前台承载底座（对应线路图 Phase 5）。当前功能可用，但存在五类偏离仓库规范的欠账，若不清偿，每次上游升级都会放大风险：

1. **数据层不合规**：6 个管理端点（quota list/set、dingtalk 配置与测试、oauth2 配置与测试、email-api test、forward-tokens CRUD）全部走 legacy `web/service/base` 的 `get/post/del`，组件内用 `useState/useEffect` 手写 loading/refetch；`web/contract/` 中没有任何 system-manage-extend 合约。这违反 web/AGENTS.md 的强制规范（contract-first + TanStack Query，`consoleQuery` + `queryOptions()/mutationOptions()`，失效逻辑绑定在 mutation 定义中）。
2. **UI 不合规**：表格、分页、编辑弹窗（`fixed inset-0` 手写遮罩）、tab、按钮全部手写原生元素 + Tailwind，未复用 base（dify-ui）组件；违反 overlay 规范（新代码应使用 `base/ui` 的 overlay 原语）；`forward-token-list.tsx` 用 `globalThis.confirm` 做删除确认。
3. **Toast 双轨**：dingtalk/oauth2 已迁新 `ui/toast`，quota-management、email-api、forward-token 仍用旧 `base/toast` 的 `Toast.notify`——旧组件一旦被上游删除即编译失败。
4. **i18n 缺口**：quota 页分页控件（"每页/条，共 N 条/第 X 页"）与 email-api 说明段落硬编码中文，未进入 `web/i18n/{locale}/extend.json`（20 语言）。
5. **权限口径不一致**：前端入口（`system-manage-nav-extend`）与 layout 仅 owner（`isCurrentWorkspaceOwner`）可见，后端 `@system_admin_required_extend` 却是 `is_admin_or_owner` 均放行；且前端仅客户端 return null 守卫。

本 change 前置依赖 P3（upstream 1.15.0 合并完成）：dify-ui 组件族与 contract 体系需以 1.15.0 为准，避免在旧基线上返工。

## What Changes

- **新增 contract 定义**：新建 `web/contract/console/system-manage-extend.ts`，用 oRPC contract 定义全部 6 个端点（对齐 `api/controllers/console/system_manage_extend.py` 的实际请求/响应结构），注册进 `consoleRouterContract`（`web/contract/router.ts`）。
- **call site 迁移**：quota-management 页与 system-integration 4 个组件的数据访问全部迁到 `consoleQuery` 的 `queryOptions()/mutationOptions()`，缓存失效逻辑绑定在 mutation 定义中；**移除** `web/service/system-manage-extend.ts` legacy 封装，`web/models/system-manage-extend.ts` 类型并入 contract schema。
- **UI 规范化**：表格/分页/弹窗/开关/输入/按钮替换为 base（dify-ui）组件族；删除确认从 `globalThis.confirm` 换为 `Confirm` 组件；编辑弹窗改用 `base/ui` overlay 原语；system-integration 的 tab 切换方式在 design 中给出取舍结论。
- **Toast 统一**：quota-management、email-api、forward-token 全部迁到新 `ui/toast`，清除对旧 `base/toast` 的依赖；顺带清除 `catch (e: any)` 写法。
- **i18n 补齐**：分页控件与 email-api 说明的硬编码中文进入 `extend.json`，20 语言全量补齐（可用 `web/i18n-config/auto-gen-i18n.js` 工具链辅助翻译）。
- **权限口径统一**：前后端统一为 admin_or_owner（前端由 `isCurrentWorkspaceOwner` 改为 `isCurrentWorkspaceManager` 级别判断，涉及 `system-manage-nav-extend` 与 layout 两处）；design 中列出与 owner-only 方案的对比及影响。
- **测试**：quota-management 现有 Vitest+RTL 测试（mock service 层）随数据层迁移重写为 mock contract/query 层；system-integration 4 个组件补齐测试。

无破坏性变更：后端 API 路径与请求/响应结构不变（权限装饰器行为不变），仅前端实现方式与前端权限门槛调整。

## Capabilities

### New Capabilities

> `openspec/specs/` 目前为空，以下均为新建 capability。

- `console-system-manage-data-layer`: 系统管理 6 端点的 contract-first 数据层——oRPC contract 定义、router 注册、consoleQuery call site、mutation 失效逻辑、legacy service 封装退役。
- `console-system-manage-ui`: 系统管理页面 UI 规范——base（dify-ui）组件族复用、overlay 原语、Confirm 删除确认、toast 单轨化、错误类型化。
- `console-system-manage-i18n`: 系统管理页面文案国际化——硬编码文案清零，extend.json 20 语言全量覆盖。
- `console-system-manage-access-control`: 系统管理入口权限口径——前端可见性判断与后端 `@system_admin_required_extend` 装饰器统一为 admin_or_owner。

### Modified Capabilities

（无——`openspec/specs/` 中无既有 spec。）

## Architecture Impact

- **contract 体系**（`web/contract/base.ts`、`router.ts`、`console/*.ts`）：新增一个 domain 文件并注册进 `consoleRouterContract`，复用既有 `base.route(...)` 模式，无结构性改动。`web/contract/console/system.ts` 已被 fork 侵入（login_config），本 change 不动它，system-manage-extend 独立成文件。
- **service 层**（`web/service/client.ts` 的 `consoleQuery`）：纯消费方，无改动；`web/service/system-manage-extend.ts` 与 `web/models/system-manage-extend.ts` 退役删除。
- **UI 基础设施**：复用 `base/pagination`、`base/input`、`base/button`、`base/switch`、`base/confirm`、`base/loading` 与 `base/ui/*` overlay 原语，不新增共享组件。
- **i18n 体系**：沿用 `extend.json` namespace 与 `web/i18n-config` 注册机制，不新增 namespace。
- **后端**：`api/controllers/console/system_manage_extend.py` 权限装饰器保持 `is_admin_or_owner`，预期零改动或仅注释澄清；不涉及数据模型与迁移。
- **测试模式**：mock 层级从 service 函数改为 contract/query 层（MSW 或 mock `consoleQuery`），遵循 `frontend-testing` skill 的既有模式。

## Impact

- **前端代码**：
  - 新增：`web/contract/console/system-manage-extend.ts`
  - 修改：`web/contract/router.ts`、`web/app/(commonLayout)/system-manage-extend/` 全部页面（layout、quota-management/page、system-integration 的 page/dingtalk-config/oauth2-config/email-api-config/forward-token-list）、`web/app/components/header/system-manage-nav-extend/index.tsx`、`web/i18n/{20 locales}/extend.json`
  - 删除：`web/service/system-manage-extend.ts`、`web/models/system-manage-extend.ts`
  - 测试：重写 `quota-management/__tests__/page.spec.tsx`，新增 system-integration 4 个组件测试
- **后端代码**：`api/controllers/console/system_manage_extend.py` 预期不改（权限口径以后端为准收敛）。
- **API/依赖**:无新增依赖、无 API 变更、无数据库变更。
- **风险面**：上游升级时旧 `base/toast`/手写 overlay 被删除导致编译失败的风险随本 change 消除；admin 角色将获得系统管理入口可见性（原仅 owner），属预期的权限放宽，与后端实际执行口径一致。
