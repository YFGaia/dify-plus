# console-system-manage-data-layer

系统管理 6 端点的 contract-first 数据层：oRPC contract 定义、router 注册、consoleQuery call site、mutation 失效逻辑、legacy service 封装退役。

## ADDED Requirements

### Requirement: 系统管理端点全部以 oRPC contract 定义
系统 SHALL 在 `web/contract/console/system-manage-extend.ts` 中为以下 6 组端点定义 oRPC contract，且 schema MUST 对齐 `api/controllers/console/system_manage_extend.py` 的实际请求/响应结构：

1. `GET /system-manage-extend/quota-management`（query：`page`、`page_size`、`keyword?`；输出 `{ list, total, page, page_size }`）
2. `POST /system-manage-extend/quota-management/set`（body：`{ account_id, quota }`）
3. `GET|POST /system-manage-extend/integration/dingtalk`、`GET .../dingtalk/test`、`POST .../dingtalk/test-callback`
4. `GET|POST /system-manage-extend/integration/oauth2`、`POST .../oauth2/test`
5. `POST /system-manage-extend/integration/email-api/test`（body：`{ url, key }`）
6. `GET|POST /system-manage-extend/forward-tokens`、`DELETE /system-manage-extend/forward-tokens/{seq}`

contract MUST 遵循 `frontend-query-mutation` skill 的 contract 规则：输入使用 `{ params, query?, body? }` 结构；无输入的 GET 省略 `.input(...)`；path param 使用 `{seq}` 占位写法；类型不使用 `any`。

#### Scenario: contract 注册进 consoleRouterContract
- **WHEN** 开发者在 `web/contract/router.ts` 中查看 `consoleRouterContract`
- **THEN** 存在 `systemManageExtend` 命名空间，包含上述全部端点的 contract 条目，且 `pnpm type-check:tsgo` 通过

#### Scenario: contract 输出类型与后端响应一致
- **WHEN** 以后端 controller 的真实响应结构（如 quota 列表的 `{ list, total, page, page_size }`）作为测试 fixture 消费 contract 类型
- **THEN** 类型检查通过，无需类型断言或 `any` 逃逸

### Requirement: 页面数据访问统一走 consoleQuery
quota-management 页与 system-integration 4 个组件的读操作 SHALL 使用 `useQuery(consoleQuery.systemManageExtend.*.queryOptions(...))`，写操作 SHALL 使用基于 `mutationOptions(...)` 的 `useMutation`；组件内 MUST NOT 保留手写 `useState/useEffect` 的 loading/refetch 编排。

#### Scenario: quota 列表分页查询
- **WHEN** 用户在 quota-management 页切换页码或搜索关键字
- **THEN** 列表通过 query input 变化自动重新获取，loading 状态来自 query 状态而非手写 state

#### Scenario: 无手写请求编排残留
- **WHEN** 执行 `rg "from '@/service/base'" web/app/\(commonLayout\)/system-manage-extend/`
- **THEN** 无任何匹配

### Requirement: 缓存失效逻辑绑定在 service 层 mutation 定义中
有缓存副作用的 mutation（额度设置、dingtalk/oauth2 配置保存、forward-token 创建/删除）SHALL 定义在 `web/service/use-system-manage-extend.ts` 中，并在 mutation 的 `onSuccess` 内通过 `queryClient.invalidateQueries` + 对应 `.key()` 完成失效；组件层 MUST NOT 自行决定失效哪些 query。

#### Scenario: 额度设置成功后列表自动刷新
- **WHEN** 用户在编辑弹窗保存新额度且请求成功
- **THEN** `quotaList` 的 query key 被失效，列表自动重新获取，无组件层手动 refetch 调用

#### Scenario: forward-token 删除后列表自动刷新
- **WHEN** 删除 forward-token 的 mutation 成功
- **THEN** token 列表 query key 被失效并自动刷新

### Requirement: legacy service 封装退役
系统 SHALL 删除 `web/service/system-manage-extend.ts`，并将 `web/models/system-manage-extend.ts` 中的类型并入 contract schema 后删除该文件；如存在本 change 范围外的类型引用，SHALL 改为从 contract 文件导出供给。

#### Scenario: 无 legacy 调用残留
- **WHEN** 执行 `rg "service/system-manage-extend|models/system-manage-extend" web/`
- **THEN** 无任何匹配（测试文件亦不得 mock 已删除的模块）
