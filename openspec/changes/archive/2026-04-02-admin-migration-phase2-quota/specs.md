# Specs: User Quota Management

## Functional Requirements

### FR-1 额度列表查看

- 管理员可看到所有在 `account_money_extend` 中有记录的用户
- 按已使用配额从高到低排序
- 列显示：排名、头像、姓名、已使用配额、总配额、余额（=总-已用）
- 支持分页，默认每页 10 条，可选 10/30/50/100

### FR-2 用户搜索

- 支持按姓名（`accounts.name`）或邮箱（`accounts.email`）模糊搜索
- 搜索空时返回全量分页
- 搜索无结果时显示空态

### FR-3 修改用户额度

- 管理员点击"修改额度"后，弹出对话框展示当前额度值
- 输入新额度（非负浮点数），点击确认提交
- 提交成功后刷新列表
- 失败时显示错误提示

### FR-4 权限控制

- 仅 workspace owner 或 admin 角色可访问本页面
- 非授权用户访问时显示无权限提示

## Non-functional Requirements

### NFR-1 性能

- 列表接口响应 < 500ms（正常 100 用户以内）
- 支持 10000+ 用户场景（index on `account_money_extend.used_quota` 已存在或可加）

### NFR-2 数据一致性

- 额度修改使用 UPSERT，保证幂等
- `total_quota` 允许设为 0（冻结用户额度）

## API Contract

### GET /console/api/system-manage-extend/quota-management

**Query params:**

```
page:      int, default=1
page_size: int, default=10, max=100
keyword:   str, optional
```

**Response 200:**

```json
{
  "list": [
    {
      "account_id": "550e8400-e29b-41d4-a716-446655440000",
      "ranking": 1,
      "name": "张三",
      "email": "zhangsan@example.com",
      "avatar": null,
      "used_quota": 45.2345678,
      "total_quota": 100.0,
      "balance": 54.7654322
    }
  ],
  "total": 128,
  "page": 1,
  "page_size": 10
}
```

### POST /console/api/system-manage-extend/quota-management/set

**Request body:**

```json
{
  "account_id": "550e8400-e29b-41d4-a716-446655440000",
  "quota": 150.0
}
```

**Response 200:**

```json
{ "result": "success" }
```

**Error responses:**

- 400: `account_id` 格式无效 / `quota` < 0
- 403: 无权限
- 500: 数据库错误
