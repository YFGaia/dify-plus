# 任务规划：API Key 额度控制功能修复

**需求 ID**: api_key_quota_control
**基于版本**: merge/upstream-1.13.2
**参考需求文档**: `specs/requirements_api_key_quota_control.md`

---

## 任务列表

### T1 - 前端：修复"新建 API Key"按钮，使其先弹出额度设置弹窗

- **文件**: `web/app/components/develop/secret-key/secret-key-modal.tsx`
- **优先级**: P0（核心修复）
- **内容**:
  - 将底部"Create New Secret Key"按钮的 `onClick` 从 `onCreate` 改为 `openSecretKeyQuotaSetModalExtend`
  - 让用户先在弹窗内设置 description/day_limit/month_limit，再点弹窗内的"创建"
- **状态**: [ ] 未开始

### T2 - 前端：修复 `onCreate` 函数，传递 keyItem 额度参数

- **文件**: `web/app/components/develop/secret-key/secret-key-modal.tsx`
- **优先级**: P0（核心修复）
- **内容**:
  - 将 `onCreate` 修改为从 keyItem 中读取 description / day_limit_quota / month_limit_quota 并放入 body
  - 创建完成后关闭额度弹窗（setVisibleExtend(false)）
  - 注意：只修改 "从额度弹窗调用的场景"，保持 keyItem 状态的正确传递
- **状态**: [ ] 未开始

### T3 - 后端（可选增强）：apikey.py GET 改为 LEFT JOIN

- **文件**: `api/controllers/console/apikey.py`
- **优先级**: P1（改善）
- **内容**:
  - 将 `api_token_money_extend_query` 由 INNER JOIN 改为 LEFT JOIN
  - 对于无 quota 记录的老 key，在 merged_data 中补充默认值（quota=-1, usage=0）
  - 防止历史创建的 API Key（quota 记录缺失）在列表中不可见
- **状态**: [ ] 未开始

### T4 - 后端：修复 apikey.py GET 中的变量命名错误

- **文件**: `api/controllers/console/apikey.py`
- **优先级**: P1（代码质量）
- **内容**:
  - 修正 `for api_token, api_token_money_extend in api_token_money_extend_query` 的变量名混淆
  - query 顺序是 `(ApiTokenMoneyExtend, ApiTokenAlias)`，loop 变量应反映真实类型
  - 当前虽因字段名不重叠而"侥幸"正确运行，但命名错误会造成混淆
- **状态**: [ ] 未开始

### T5 - 验证：后端额度检查链路端到端测试

- **文件**: (测试 / 验证）
- **优先级**: P1（验证）
- **内容**:
  - 用 curl 测试 POST /v1/workflows/run（Workflow App API Key，含超额场景）
  - 验证 day_limit_quota=0.001 时调用后返回 403 日额度超限
  - 验证 wraps.py validate_app_token 中的 quota check 正常触发
- **状态**: [ ] 未开始

### T6 - 文档：更新验证任务文档

- **文件**: `docs/dify-plus/tasks-verify-1.13.2-merge.md`
- **优先级**: P2（文档）
- **内容**:
  - 更新 T12 验证项为已实现状态
  - 补充额度控制的测试步骤
- **状态**: [ ] 未开始

---

## 实施顺序

```
T1（按钮流程） → T2（onCreate传参） → T4（命名修复） → T3（LEFT JOIN） → T5（链路验证） → T6（文档）
```

## 关键改动说明

```typescript
// ======= T1+T2 综合改动 =======

// Before（按钮直接创建，无额度设置）:
<Button onClick={onCreate}>Create New Secret Key</Button>

const onCreate = async () => {
  const params = { url: `/apps/${appId}/api-keys`, body: {} }  // 无额度参数
  ...
}

// After（按钮先弹窗）:
<Button onClick={openSecretKeyQuotaSetModalExtend}>Create New Secret Key</Button>

const onCreate = async () => {
  const params = {
    url: `/apps/${appId}/api-keys`,
    body: {
      description: keyItem.description,
      day_limit_quota: keyItem.day_limit_quota,
      month_limit_quota: keyItem.month_limit_quota,
    }
  }
  const createApikey = appId ? createAppApikey : createDatasetApikey
  const res = await createApikey(params)
  setVisibleExtend(false)   // ← 关闭额度弹窗
  setVisible(true)
  setNewKey(res)
  if (appId) invalidateAppApiKeys(appId)
  else invalidateDatasetApiKeys()
}
```
