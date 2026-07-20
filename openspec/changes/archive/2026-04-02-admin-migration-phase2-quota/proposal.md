# Admin 管理功能迁移至 Dify 原生技术栈 — 第二阶段：用户额度管理

## Summary

将 Go+Vue Admin 后台中"用户额度管理"（`QuotaList`）功能迁移至 Dify 原生 Next.js + Python 技术栈，实现对所有 Dify 用户总额度（`account_money_extend.total_quota`）的集中管理。

迁移完成后，Admin 后台的额度管理入口将被废弃（不再需要独立 Go 服务），由 Dify Console 的系统管理菜单统一承接。

## Background

### 当前状态

- Admin 后台（`/#/layout/QuotaList`）提供：按用户名/邮箱搜索、分页列表、单用户额度修改
- 数据层：`account_money_extend` 表，已被 Dify Python 侧使用（调度任务、workflow 执行扣费等）
- Phase 1 已建立迁移模式（`system_manage_extend.py` 控制器 + 权限装饰器 + `layout.tsx` 侧边栏）

### 为什么迁移

- 额度管理是系统管理员高频操作，但需要切换到独立的 Admin 服务访问
- Admin 服务后续将被完全停用，额度管理功能必须迁移
- Dify 侧已有完整的 `account_money_extend` 模型，无需新增数据库对象

## Design

### 后端（追加到 `system_manage_extend.py`）

**新增 Service：** `QuotaManageService` （在 `api/services/system_manage_extend.py`）

- `get_quota_list(page, page_size, keyword)` — 分页查询，JOIN accounts，按 `used_quota DESC` 排序
- `set_user_quota(account_id, quota)` — UPSERT `account_money_extend.total_quota`（改善 Admin 的纯 UPDATE）

**新增 Controller 资源：**（在 `api/controllers/console/system_manage_extend.py`）

- `QuotaManagementListExtend`：GET `/system-manage-extend/quota-management`
- `QuotaManagementSetExtend`：POST `/system-manage-extend/quota-management/set`

**权限：** 复用已有 `@system_admin_required_extend`

### 前端

**新增路由目录：** `web/app/(commonLayout)/system-manage-extend/quota-management/`

- `page.tsx`：额度管理表格页（搜索 + 分页 + 列表 + 修改弹窗）

**更新 `layout.tsx`：** 在侧边栏菜单添加"用户额度"菜单项

**追加 `web/service/system-manage-extend.ts`：**

- `getQuotaList(params)` — 获取分页列表
- `setUserQuota(accountId, quota)` — 修改指定用户额度

**追加 `web/models/system-manage-extend.ts`：**

- `QuotaListItem` — 列表行类型
- `QuotaListResponse` — 分页响应类型

**追加 `web/i18n/{zh-Hans,en-US}/extend.json`：** `systemManage.quota.*` 命名空间

### 低冲突策略

继承 Phase 1 约定：

- 全部修改限于已有的 extend 文件或新的 `quota-management/` 目录
- 后端仅追加到已有文件，不改上游路由
- 前端仅修改 `layout.tsx`（追加菜单项）

## Non-goals

- 不迁移用户基础信息管理（用户列表/封禁/角色分配）
- 不修改额度的自动扣减逻辑（调度任务）
- 不提供额度统计图表（属于 Dashboard，后续阶段）
- 不开放用户自助申请额度的流程
