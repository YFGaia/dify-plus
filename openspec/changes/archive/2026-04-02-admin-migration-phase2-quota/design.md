# Design: Admin Migration Phase 2 — User Quota Management

## Backend Architecture

### Service Layer

**File:** `api/services/system_manage_extend.py`（追加 `QuotaManageService` class）

```
class QuotaManageService:
    @staticmethod
    def get_quota_list(page: int, page_size: int, keyword: str) -> dict:
        """
        分页查询用户额度列表
        - 以 used_quota DESC 排序（展示消耗最多的用户在前）
        - keyword 非空时：先从 accounts 按 name/email LIKE 搜索
        - 返回：{ list: [...], total: int, page: int, page_size: int }
        """

    @staticmethod
    def set_user_quota(account_id: str, quota: float) -> None:
        """
        设置指定用户的总额度
        - 使用 INSERT ... ON CONFLICT DO UPDATE (upsert)
        - 无记录时自动创建（Admin 纯 UPDATE 的改进）
        """
```

**数据查询逻辑（对应 Go 原版）：**

```python
# 1. keyword 搜索（先取 account_id 子集）
if keyword:
    matched_ids = db.session.query(Account.id).filter(
        or_(Account.name.ilike(f"%{keyword}%"),
            Account.email.ilike(f"%{keyword}%"))
    ).all()

# 2. 主查询
query = db.session.query(AccountMoneyExtend).order_by(
    AccountMoneyExtend.used_quota.desc()
)

# 3. 关联账户信息（批量查，避免 N+1）
account_ids = [r.account_id for r in rows]
accounts = {a.id: a for a in db.session.query(Account).filter(
    Account.id.in_(account_ids)
).all()}

# 4. 组装响应（含排名 = offset + i + 1）
```

**UPSERT 实现：**

```python
from sqlalchemy.dialects.postgresql import insert as pg_insert

stmt = pg_insert(AccountMoneyExtend).values(
    account_id=account_id,
    total_quota=quota
).on_conflict_do_update(
    index_elements=['account_id'],
    set_={'total_quota': quota}
)
db.session.execute(stmt)
db.session.commit()
```

### Controller Routes

**File:** `api/controllers/console/system_manage_extend.py`（追加）

```
GET  /console/api/system-manage-extend/quota-management
     参数：page (int), page_size (int), keyword (str, optional)
     响应：{ list: QuotaItem[], total: int, page: int, page_size: int }

POST /console/api/system-manage-extend/quota-management/set
     Body：{ account_id: str, quota: float }
     响应：{ result: "success" }
```

**QuotaItem 结构：**

```json
{
  "account_id": "uuid-string",
  "ranking": 1,
  "name": "用户名",
  "email": "user@example.com",
  "avatar": "https://...",
  "used_quota": 12.3456789,
  "total_quota": 100.0,
  "balance": 87.6543211
}
```

### Route Registration

`api/controllers/console/__init__.py` 已有 import，无需修改（本次追加的类在同一文件内）

## Frontend Architecture

### Route Structure

```
web/app/(commonLayout)/system-manage-extend/
├── layout.tsx                    ← 追加"用户额度"菜单项
├── page.tsx                      (已有，redirect)
├── system-integration/           (已有，Phase 1)
│   └── ...
└── quota-management/             ← 新增
    └── page.tsx                  ← 额度管理主页面
```

### Component Design (`quota-management/page.tsx`)

```
┌─────────────────────────────────────────────────────┐
│  用户额度管理                                         │
├─────────────────────────────────────────────────────┤
│  [🔍 搜索用户名/邮箱...]          [查询]              │
├────┬──────┬────────┬───────────┬──────────┬─────────┤
│排名│头像  │成员     │已使用配额  │总配额    │操作      │
├────┼──────┼────────┼───────────┼──────────┼─────────┤
│ 1  │ 🧑  │张三     │ 45.23 USD │100.00 USD│[修改额度]│
│ 2  │ 👤  │李四     │ 32.10 USD │ 50.00 USD│[修改额度]│
│ ...│      │        │           │          │          │
├────┴──────┴────────┴───────────┴──────────┴─────────┤
│     < 上一页  第1页  下一页 >    共 128 条             │
└─────────────────────────────────────────────────────┘

点击[修改额度]弹出对话框：
┌──────────────────────────────┐
│  修改张三的额度（单位：USD）   │
│  [    100.00               ] │
│  [取消]              [确认]   │
└──────────────────────────────┘
```

### State Management

```typescript
// 页面状态
const [keyword, setKeyword] = useState('')
const [page, setPage] = useState(1)
const [pageSize, setPageSize] = useState(10)
const [loading, setLoading] = useState(false)
const [list, setList] = useState<QuotaListItem[]>([])
const [total, setTotal] = useState(0)
// 编辑弹窗
const [editTarget, setEditTarget] = useState<QuotaListItem | null>(null)
const [editValue, setEditValue] = useState<string>('')
const [submitting, setSubmitting] = useState(false)
```

### API Service (追加到 `web/service/system-manage-extend.ts`)

```typescript
export const getQuotaList = (params: { page: number; page_size: number; keyword?: string }) =>
  get<QuotaListResponse>('/system-manage-extend/quota-management', { params })

export const setUserQuota = (data: { account_id: string; quota: number }) =>
  post<{ result: string }>('/system-manage-extend/quota-management/set', { body: data })
```

### Types (追加到 `web/models/system-manage-extend.ts`)

```typescript
export type QuotaListItem = {
  account_id: string
  ranking: number
  name: string
  email: string
  avatar: string | null
  used_quota: number
  total_quota: number
  balance: number
}

export type QuotaListResponse = {
  list: QuotaListItem[]
  total: number
  page: number
  page_size: number
}
```

### i18n Keys (追加到 `extend.json`)

```json
"systemManage.quota": "用户额度",
"systemManage.quota.title": "用户额度管理",
"systemManage.quota.search": "搜索用户名/邮箱",
"systemManage.quota.search.button": "查询",
"systemManage.quota.table.ranking": "排名",
"systemManage.quota.table.avatar": "头像",
"systemManage.quota.table.member": "成员",
"systemManage.quota.table.usedQuota": "已使用配额",
"systemManage.quota.table.totalQuota": "总配额",
"systemManage.quota.table.balance": "余额",
"systemManage.quota.table.actions": "操作",
"systemManage.quota.action.edit": "修改额度",
"systemManage.quota.editDialog.title": "修改 {{name}} 的额度（单位：USD）",
"systemManage.quota.editDialog.inputPlaceholder": "请输入额度数值",
"systemManage.quota.editDialog.invalidInput": "请输入有效的数字",
"systemManage.quota.editDialog.success": "额度修改成功",
"systemManage.quota.editDialog.failed": "额度修改失败",
"systemManage.quota.empty": "暂无额度数据"
```

## Data Flow

```
用户在搜索框输入 keyword
  → 点击查询
    → getQuotaList({ page: 1, page_size: 10, keyword })
      → GET /console/api/system-manage-extend/quota-management?...
        → QuotaManagementListExtend.get()
          → QuotaManageService.get_quota_list()
            → SELECT account_money_extend JOIN accounts
              (keyword→先搜accounts, 再filter)
            → 返回分页数据
          ← { list, total, page, page_size }
      ← JSON 响应
    ← 更新 list/total state
  → 渲染表格

用户点击"修改额度"
  → 弹出对话框，输入新额度
    → 点击确认
      → setUserQuota({ account_id, quota })
        → POST /console/api/system-manage-extend/quota-management/set
          → QuotaManagementSetExtend.post()
            → QuotaManageService.set_user_quota()
              → UPSERT account_money_extend
          ← { result: "success" }
        ←
      → toast 成功提示
      → 刷新列表
```

## Implementation Notes

### 与 Admin Go 版的改进点

| 改进项       | Admin Go 版             | 本次 Python 版               |
| ------------ | ----------------------- | ---------------------------- |
| 设置额度     | 纯 UPDATE（新用户无效） | UPSERT（自动创建记录）       |
| 搜索 keyword | 先查 accounts 再 filter | 同 Go 逻辑，改用 SQLAlchemy  |
| 余额字段     | 前端计算 total-used     | 后端计算后直接返回 `balance` |
| avatar       | 显示（headerImg）       | 显示（accounts.avatar URL）  |

### 边界情况

- `account_money_extend` 无记录的用户不出现在列表中（同 Admin 版行为）
- `quota=0` 合法（意味着无可用额度）
- `quota` 值校验：后端 `≥ 0` 的浮点数，前端也做正则校验
