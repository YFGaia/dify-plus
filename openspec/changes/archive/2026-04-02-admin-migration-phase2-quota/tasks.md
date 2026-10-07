# Tasks: Admin Migration Phase 2 — User Quota Management

## Backend Tasks

### B-1: 添加 QuotaManageService

**File:** `api/services/system_manage_extend.py`

- 追加 `QuotaManageService` 类
- 实现 `get_quota_list(page, page_size, keyword)` 方法
  - keyword 搜索：先从 `accounts` 按 name/email ILIKE 搜索，取 account_id 列表
  - 主查询：`account_money_extend` ORDER BY `used_quota DESC`，COUNT + LIMIT/OFFSET
  - 关联账户信息：批量查 `accounts`，避免 N+1
  - 组装响应，含 ranking（offset + i + 1）和 balance（total - used）
- 实现 `set_user_quota(account_id, quota)` 方法
  - 使用 PostgreSQL UPSERT（`INSERT ... ON CONFLICT DO UPDATE`）
  - 校验 quota >= 0

### B-2: 添加 Controller 路由

**File:** `api/controllers/console/system_manage_extend.py`

- 追加 `QuotaManagementListExtend(Resource)` class
  - GET 方法：解析 query params（page, page_size, keyword）→ 调用 service → 返回分页数据
  - 权限：`@system_admin_required_extend`
- 追加 `QuotaManagementSetExtend(Resource)` class
  - POST 方法：解析 JSON body（account_id, quota）→ 校验 → 调用 service → 返回 success
  - 权限：`@system_admin_required_extend`
- 追加路由注册：
  ```python
  api.add_resource(QuotaManagementListExtend, "/system-manage-extend/quota-management")
  api.add_resource(QuotaManagementSetExtend, "/system-manage-extend/quota-management/set")
  ```

## Frontend Tasks

### F-1: 添加类型定义

**File:** `web/models/system-manage-extend.ts`

- 追加 `QuotaListItem` type
- 追加 `QuotaListResponse` type

### F-2: 添加 API 服务方法

**File:** `web/service/system-manage-extend.ts`

- 追加 `getQuotaList(params)` 函数
- 追加 `setUserQuota(data)` 函数

### F-3: 添加 i18n key

**Files:** `web/i18n/zh-Hans/extend.json`、`web/i18n/en-US/extend.json`

- 追加 `systemManage.quota.*` 相关 key（约 15 个）

### F-4: 创建额度管理页面

**File:** `web/app/(commonLayout)/system-manage-extend/quota-management/page.tsx`

- 搜索框 + 查询按钮
- 表格：排名、头像（`<img>` 或 fallback 首字母）、姓名、已用配额、总配额、余额、操作
- 分页（复用 Dify 内置分页组件，或简单的 prev/next）
- 修改额度对话框（`Dialog` 组件，输入框 + 正则校验）
- Loading / Empty 状态

### F-5: 更新侧边栏菜单

**File:** `web/app/(commonLayout)/system-manage-extend/layout.tsx`

- 在 `menuItems` 数组中追加"用户额度"菜单项
  ```typescript
  {
    key: 'quota-management',
    label: t('systemManage.quota', { ns: 'extend' }),
    href: '/system-manage-extend/quota-management',
  }
  ```

## Verification Checklist

- [x] B-1: `get_quota_list` 无 keyword 时返回全量分页
- [x] B-1: `get_quota_list` 有 keyword 时按 name/email 过滤
- [x] B-1: `set_user_quota` 对不存在记录的用户执行时，自动创建记录
- [x] B-2: 未登录或无权限时返回 401/403
- [x] F-4: 页面加载时自动拉取第一页数据
- [x] F-4: 修改额度成功后列表自动刷新
- [x] F-4: 输入非数字时前端校验拦截
- [x] F-5: 侧边栏"用户额度"在 owner/admin 下可见，点击导航正确
