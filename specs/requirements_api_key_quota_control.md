# API Key 额度控制功能需求文档

## 背景

### 功能说明

在 dify-plus fork 的 1.12.x 版本中，Workflow API Key（以及其他应用类型的 API Key）支持**额度控制**功能，包括：

- **每日额度（day_limit_quota）**：API Key 每天可消耗的最大额度（USD）
- **月额度（month_limit_quota）**：API Key 每月可消耗的最大额度（USD）
- **累计额度（accumulated_quota）**：API Key 历史累计消耗

合并 upstream 1.13.2 后，部分代码在合并过程中出现了功能缺失或断裂，具体表现为：

1. 前端 **创建 API Key 时无法设置额度**（直接创建，跳过额度设置弹窗）
2. 额度弹窗的 **`onCreate` 回调未传递 keyItem 的额度参数**（创建时额度丢失）
3. `openSecretKeyQuotaSetModalExtend()` 函数存在但从未被调用（死代码）

### 已完整实现的部分（无需修改）

- ✅ 数据库表：`api_token_money_extend`、`api_token_message_joins_extend`、日统计表、月统计表
- ✅ 数据库迁移：`2024_08_29_0715-1b804f8bbd28_add_api_token_money_extend.py`
- ✅ 后端额度检查（`wraps.py`）：API 调用前校验日/月额度，超限返回 403
- ✅ 后端计费追踪（`persistence.py` → Celery task）：Workflow LLM 节点执行fee扣减
- ✅ Chat/Completion/AgentChat 计费（`ApiTokenMessageJoinsExtend` 关联 + message 事件）
- ✅ API Key CRUD（`apikey.py`）：创建时生成额度记录，列表含额度字段，PUT 支持编辑
- ✅ 前端额度列展示（`secret-key-modal.tsx`）：description、day/month usage/limit、accumulated
- ✅ Celery Beat 定时任务：每日零点重置 day_used_quota，每月一日零点重置 month_used_quota
- ✅ Admin 额度管理页面（`admin/web/src/view/quota/index.vue` 等）

---

## 问题根因分析

### 问题1：前端创建流程断裂（主要）

**现象**：点击"Create New Secret Key"按钮 → 直接调用 `onCreate` 创建密钥，未显示额度设置弹窗

**期望流程**（1.12 版本设计意图）：

```
点击"Create New Secret Key"
  → openSecretKeyQuotaSetModalExtend()   (打开额度设置弹窗，keyItem.id='')
  → 用户填写 description / day_limit_quota / month_limit_quota
  → 弹窗内点击"Create"
  → onCreateWithQuota()  (调用 POST /apps/{id}/api-keys，带上额度参数)
  → 关闭弹窗，刷新列表
```

**当前断裂代码**：

```tsx
// secret-key-modal.tsx
// 问题：onClick 应绑定 openSecretKeyQuotaSetModalExtend，而非 onCreate
<Button onClick={onCreate}>Create New Secret Key</Button>

// 问题：onCreate 在调用时带的 body 是 {}，没有传递 keyItem 中的额度值
const onCreate = async () => {
  const params = { url: `/apps/${appId}/api-keys`, body: {} }  // ← 空 body
  ...
}
```

### 问题2：额度弹窗的 `onCreate` 未包含额度参数

**现象**：即使用户手动打开额度弹窗（通过代码调试），输入了额度值后点"Create"，实际发送的请求 body 仍为 `{}`

**期望**：调用创建 API 时，body 应包含 `description`、`day_limit_quota`、`month_limit_quota`

---

## 功能修复目标

### 目标1：前端 CREATE 流程修复

- 点击"新建 API Key"按钮 → 先弹出额度设置弹窗
- 弹窗内可设置：密钥描述、每日额度上限、月额度上限（-1 表示无限制）
- 弹窗点击"创建"→ 调 POST API，带上额度参数
- 弹窗点击"关闭"→ 取消创建

### 目标2：前端 EDIT 流程（已有，确认正常）

- 点击每条密钥行尾的编辑图标 → 弹出额度设置弹窗（带当前值）
- 弹窗内修改后点"保存"→ 调 PUT API 更新额度 ✅（已工作）

### 目标3：后端链路完整性验证

- 确认 `wraps.py` 额度检查正确工作
- 确认 `apikey.py` GET 的 INNER JOIN 是否导致旧 key 不可见（可选改为 LEFT JOIN）
- 确认 Workflow 运行时扣费链路正常

---

## 验收标准

1. 新建 API Key 时，出现额度设置弹窗
2. 弹窗内输入 description、day_limit_quota、month_limit_quota 后点击"创建"，密钥被成功创建且额度记录与输入一致
3. API Key 列表正确展示 description、日额度消耗/上限、月额度消耗/上限、累计消耗
4. 编辑 API Key 额度后，更新值正确反映在列表和数据库中
5. 当 day_used_quota >= day_limit_quota（且 day_limit_quota ≠ -1）时，API 调用返回 403 日额度超限错误
6. 当 month_used_quota >= month_limit_quota（且 month_limit_quota ≠ -1）时，API 调用返回 403 月额度超限错误
