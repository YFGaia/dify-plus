# console-system-manage-access-control

系统管理入口权限口径：前端可见性判断与后端 `@system_admin_required_extend` 装饰器统一为 admin_or_owner。

## ADDED Requirements

### Requirement: 前端入口可见性统一为 admin_or_owner
`web/app/components/header/system-manage-nav-extend/index.tsx` 与 `web/app/(commonLayout)/system-manage-extend/layout.tsx` 的可见性/访问守卫 SHALL 使用 `isCurrentWorkspaceManager`（owner 与 admin 均为 true），MUST NOT 再使用 `isCurrentWorkspaceOwner` 作为系统管理入口的判断条件。

#### Scenario: admin 角色可见并可访问
- **WHEN** 当前 workspace 角色为 admin 的用户登录 Console
- **THEN** 顶部导航显示系统管理入口，且 `/system-manage-extend/` 下页面正常渲染

#### Scenario: 普通成员不可见且被守卫拦截
- **WHEN** 当前 workspace 角色为 normal 的用户登录 Console
- **THEN** 顶部导航不显示系统管理入口；直接访问 `/system-manage-extend/` URL 时页面展示无权限提示而非管理内容

### Requirement: 后端权限口径保持 admin_or_owner 且为强制安全边界
`api/controllers/console/system_manage_extend.py` 的 `system_admin_required_extend` 装饰器 SHALL 保持 `current_user.is_admin_or_owner` 判断，并 MUST 覆盖全部 6 组系统管理端点；前端守卫定位为 UX 层，API 层强制校验 MUST 不依赖前端。

#### Scenario: 非管理角色直调 API 被拒绝
- **WHEN** 角色为 normal 的用户携带有效登录态直接请求 `GET /console/api/system-manage-extend/quota-management`
- **THEN** 返回 403

#### Scenario: admin 直调 API 放行
- **WHEN** 角色为 admin 的用户请求任一系统管理端点
- **THEN** 请求按正常业务逻辑处理（与 owner 一致）
