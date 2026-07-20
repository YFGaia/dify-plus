# Design: Console 系统管理入口规范化

## Context

`/system-manage-extend/` 是 admin 后台废弃后系统管理能力的唯一前台入口（线路图 Phase 5），当前含两组页面：

- **quota-management**：用户额度分页列表（搜索/分页）+ 单用户额度编辑弹窗，对接 `GET /system-manage-extend/quota-management` 与 `POST /system-manage-extend/quota-management/set`。
- **system-integration**：本地 state tab 切换的 4 个面板——dingtalk（GET/POST 配置 + GET test + POST test-callback）、oauth2（GET/POST 配置 + POST test）、email-api（POST test）、forward-token（GET 列表 / POST 创建 / DELETE `/{seq}` 删除）。

现状偏离仓库规范的具体坐标（调研确认，以工作区当前代码为准）：

| 问题                                                        | 位置                                                                                                                                                                                 |
| ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| legacy 数据层（`get/post/del` + 手写 loading/refetch）      | `web/service/system-manage-extend.ts` 全部 13 个导出函数；5 个页面组件内 `useState/useEffect`                                                                                        |
| 手写 UI（table/分页/弹窗/tab/按钮/输入原生元素 + Tailwind） | `quota-management/page.tsx`（含 `fixed inset-0` 手写遮罩弹窗）、`system-integration/*` 全部                                                                                          |
| `globalThis.confirm` 删除确认                               | `forward-token-list.tsx` `handleDelete`                                                                                                                                              |
| 旧 `base/toast` 的 `Toast.notify`                           | `quota-management/page.tsx`、`email-api-config.tsx`、`forward-token-list.tsx`（dingtalk/oauth2 已迁新 `ui/toast`）                                                                   |
| `catch (e: any)`                                            | `quota-management/page.tsx`、`email-api-config.tsx`、`forward-token-list.tsx`                                                                                                        |
| 硬编码中文                                                  | quota 页分页控件（"每页 / 条，共 N 条 / 第 X 页"）、`email-api-config.tsx` 顶部说明段落                                                                                              |
| 权限口径不一致                                              | 前端 `system-manage-nav-extend/index.tsx` 与 `system-manage-extend/layout.tsx` 用 `isCurrentWorkspaceOwner`；后端 `system_admin_required_extend` 用 `current_user.is_admin_or_owner` |

约束：

- 前置依赖 **P3**（upstream 1.15.0 合并完成）：dify-ui 组件族、`base/ui` overlay 原语与 contract 体系以 1.15.0 合并后的形态为准。
- `web/AGENTS.md` 强制项：contract-first + TanStack Query（`frontend-query-mutation` skill 为权威）；overlay 只用 `@/app/components/base/ui/*` 原语；测试遵循 `frontend-testing` skill。
- 后端 `api/controllers/console/system_manage_extend.py` 的 API 路径、请求/响应结构、权限装饰器均不变——本 change 是纯前端规范化 + 前端权限口径向后端收敛。

## Goals / Non-Goals

**Goals:**

1. 6 个管理端点全部 contract 化并注册进 `consoleRouterContract`，call site 迁 `consoleQuery`，删除 legacy service 封装。
2. 页面 UI 全面复用 base（dify-ui）组件族与 `base/ui` overlay 原语，消除手写表格/分页/弹窗/确认框。
3. Toast 单轨化（新 `ui/toast`），错误处理类型化（清除 `catch (e: any)`）。
4. 硬编码文案清零，`extend.json` 20 语言全量覆盖。
5. 前端入口可见性与后端权限装饰器统一为 admin_or_owner。
6. 测试随迁移重写/补齐，`pnpm lint`、`type-check:tsgo`、相关 vitest spec 全绿。

**Non-Goals:**

- 不改后端 API 行为（路径、参数、响应、装饰器逻辑均保持现状）。
- 不新增系统管理功能（如 code 节点执行控制的管理 UI 属 Phase 4 的 D5 决策，另行 change）。
- 不引入 middleware/服务端渲染层守卫（见 Decisions D4 的说明）。
- 不重构 `web/contract/console/system.ts` 中既有的 login_config fork 侵入。

## Architecture Assessment

### Existing Design Reuse

- **contract 模式**：完全复用 `web/contract/console/billing.ts` 等既有 domain 文件的 `base.route({...}).input(type<...>()).output(type<...>())` 写法与 `router.ts` 的按前缀嵌套注册；无需新机制。
- **query/mutation 模式**：复用 `consoleQuery`（`web/service/client.ts`）与 `frontend-query-mutation` skill 规定的 call-site `useQuery(queryOptions(...))` / 服务层 `use-{domain}.ts` mutation 定义（含失效逻辑）模式，参照 `web/service/use-billing.ts` 等既有实现。
- **UI 组件**：`base/pagination`（含 hook 与既有测试）、`base/input`、`base/button`、`base/switch`、`base/select`、`base/confirm`、`base/loading`、tab 族（`base/tab-slider-new` 等）、`base/ui/dialog`（overlay 原语）均已存在，直接复用，不新增共享组件。
- **i18n**：复用 `extend.json` namespace（`systemManage.*` key 前缀已存在）与 `web/i18n-config/auto-gen-i18n.js` 翻译工具链。
- **测试模式**：复用 `frontend-testing` skill 与仓库既有 spec 组织方式（`__tests__/*.spec.tsx`）。

### Boundaries and Ownership

- **contract 层**（`web/contract/console/system-manage-extend.ts`）：请求/响应 schema 的唯一事实源；`web/models/system-manage-extend.ts` 的类型迁入此处后删除。
- **service 层**（`web/service/use-system-manage-extend.ts`，新建）：持有 mutation 定义与缓存失效知识（哪个 mutation 失效哪些 query key）；组件不得自行 `invalidateQueries`。
- **组件层**：只消费 `useQuery(consoleQuery...queryOptions(...))` 与 service 层 mutation hook，负责 UI 反馈（toast、弹窗开合）。
- **权限**：API 层的强制权限由后端装饰器负责（真正的安全边界）；前端 `system-manage-nav-extend` 与 layout 的可见性判断是 UX 层守卫，两处统一读 `isCurrentWorkspaceManager`。
- **失败处理**：query 失败由组件层依据 `error` 状态渲染/toast；mutation 失败在 call-site `onError` 中 toast；不再有组件内 try/catch 包裹 fetch。

### Options and Rationale

见 Decisions。核心取向：零新增抽象、零新增依赖，全部收敛到既有模式。

### Quality Attributes

- **可维护性**：消灭 legacy service 封装与手写 UI 后，上游删除旧组件（`base/toast`、手写 overlay 模式）不再造成编译失败；contract 成为前后端接口漂移的检测点。
- **可测试性**：mock 层级从 service 函数上移到 query 层，测试不再与实现函数签名耦合。
- **安全性**:不变——API 权限始终由后端装饰器保证；前端权限放宽（admin 可见入口）与后端实际执行口径一致，不扩大攻击面。
- **性能**：TanStack Query 带来缓存与去重，分页查询用 `placeholderData: keepPreviousData` 避免翻页闪烁（实现时按需）。

### Complexity and Exceptions

无新增依赖、服务、存储或协议。唯一新增文件为一个 contract 文件与一个 service hook 文件，均落在既有目录约定内。

## Decisions

### D1: contract 组织——单文件 `system-manage-extend.ts`，router 下挂 `systemManageExtend` 命名空间

- **选择**：新建 `web/contract/console/system-manage-extend.ts`，容纳全部 6 个端点的 contract（quota 2 个 + integration 4 组）；在 `consoleRouterContract` 中注册为 `systemManageExtend: { quotaList, quotaSet, dingtalkConfig, dingtalkConfigSave, dingtalkTest, dingtalkTestCallback, oauth2Config, oauth2ConfigSave, oauth2Test, emailApiTest, forwardTokens, forwardTokenCreate, forwardTokenDelete }`（命名实现时可微调，保持语义清晰）。
- **替代方案**：按 domain 拆两个文件（quota / integration）。**否决理由**：6 个端点共享同一 URL 前缀 `/system-manage-extend/` 与同一权限域，单文件体量小（预计 <200 行），拆分徒增文件数；若未来端点增多再拆不迟。
- **schema 对齐要点**（以后端 controller 为准）：
  - `quotaList`：`GET /system-manage-extend/quota-management`，query `{ page, page_size, keyword? }`，输出 `{ list: QuotaListItem[], total, page, page_size }`。
  - `quotaSet`：`POST /system-manage-extend/quota-management/set`，body `{ account_id, quota }`，输出 `{ result: string }`。
  - `forwardTokenDelete`：`DELETE /system-manage-extend/forward-tokens/{seq}`，path param 用 `{seq}` 写法。
  - dingtalk/oauth2 config 的 `config` 嵌套对象保留索引签名字段时用明确类型替代 `any`（`Record<string, unknown>` 或收窄后的字段联合）。
  - 无输入的 GET（`dingtalkConfig`、`dingtalkTest`、`oauth2Config`、`forwardTokens`）省略 `.input(...)`，不写 `.input(type<unknown>())`。

### D2: 失效逻辑归属——新建 `web/service/use-system-manage-extend.ts` 持有 mutation 定义

- **选择**：mutation（quotaSet、dingtalk/oauth2 save、forwardToken create/delete、各 test）统一在 service 层文件中以 `useMutation(consoleQuery...mutationOptions({ onSuccess: invalidate... }))` 形式定义；失效映射：
  - `quotaSet` 成功 → invalidate `quotaList` key；
  - `dingtalkConfigSave` 成功 → invalidate `dingtalkConfig` key（oauth2 同理）；
  - `forwardTokenCreate/Delete` 成功 → invalidate `forwardTokens` key；
  - 各 test 类 mutation 无缓存副作用，不做失效，可直接在组件内 `useMutation(...mutationOptions())`。
- **替代方案**：全部在组件 call site 定义 mutation 并就地 invalidate。**否决理由**：违反 `frontend-query-mutation` runtime-rules（失效知识必须在 service 层 mutation 定义中）。
- query 侧无共享 extra options，按 skill 决策规则直接在组件 call site `useQuery(consoleQuery...queryOptions(...))`，不做 use-* 透传 hook。

### D3: system-integration tab——保留组件内本地 state，改用 base tab 组件

- **选择**：保持 `page.tsx` 内单页四面板 + 本地 `useState` 切换，仅把手写 tab 按钮替换为 base tab 组件（如 `tab-slider-new`，实现时以 1.15.0 合并后可用组件为准）。
- **替代方案**：改为路由级子页面（`system-integration/dingtalk` 等四个 route segment）。**取舍**：路由级可深链、可独立懒加载，但需要新增 4 个 page 文件 + 重定向逻辑，且四个面板同属一个低频管理页、无深链诉求；TanStack Query 缓存已消除切 tab 重复请求的成本。收益不抵改动面，故保留本地 state。若后续出现"分享某个集成配置页链接"的需求，再升级为路由级（改造成本不受本决策影响）。

### D4: 权限口径——前后端统一为 admin_or_owner

- **选择**：前端 `system-manage-nav-extend/index.tsx` 与 `system-manage-extend/layout.tsx` 的判断从 `isCurrentWorkspaceOwner` 改为 `isCurrentWorkspaceManager`（owner + admin 均为 true），与后端 `system_admin_required_extend` 的 `is_admin_or_owner` 完全一致。后端零改动。
- **替代方案**：统一 owner-only——后端装饰器收紧为 `is_owner`。**对比**：
  - admin_or_owner（推荐）：与后端现状一致（零后端改动、无行为回退风险）；便于运营分权（owner 可授权 admin 处理额度调整等日常运营而不移交 workspace 所有权）；额度与集成配置属租户级运营操作，admin 承担符合 Dify 的角色语义。
  - owner-only：权限面最小，但需要改后端装饰器（额外回归面），且 admin 无法分担运营操作，与"统一前台运营入口"的定位相悖。
- **补充说明**：真正的安全边界是后端装饰器（所有 6 个端点已全部覆盖），前端守卫仅是 UX 层（隐藏入口、避免 403 体验）；不引入 Next.js middleware/服务端守卫——角色数据来自登录后 workspace 上下文，middleware 层拿不到可靠角色信息，且 API 已有强制校验，middleware 守卫属重复建设。此结论记录为本 change 的权限模型定论。

### D5: 测试 mock 层级——mock 到 query/contract 层

- **选择**：重写 `quota-management/__tests__/page.spec.tsx`：删除对 `@/service/system-manage-extend` 的 mock，改为在 `QueryClientProvider` 包裹下 mock oRPC 客户端请求层（按 `frontend-testing` skill 与仓库既有先例选择 MSW 或 mock `@/service/client`），断言行为不变（首屏加载、非法输入拦截、保存成功后列表刷新）。system-integration 4 个组件按同一模式补测试。
- **替代方案**：继续 mock service 函数。**否决理由**：service 封装将被删除，且 mock 实现函数使测试与数据层实现耦合。

### D6: i18n——补 key + 工具链批量翻译

- **选择**：新增 key（分页控件文案若 `base/pagination` 自带 i18n 则直接复用其 key，无需新增；email-api 说明段落新增 `systemManage.emailApi.description`），先写 `zh-Hans`/`en-US`，用 `web/i18n-config/auto-gen-i18n.js` 生成其余 18 语言，`pnpm run i18n:check`（`check-i18n.js`）验证无缺失。
- 硬编码验收：`rg -n "每页|共.*条|第.*页" web/app/\(commonLayout\)/system-manage-extend/` 为空。

### D7: Toast 与错误类型化

- 旧 `Toast.notify`（`base/toast`）全部替换为新 `ui/toast` 的 `toast.success/error`；`catch (e: any)` 模式随数据层迁移自然消失（错误经 query/mutation 的 `error: Error` 通道），残留处统一 `error instanceof Error` 收窄（沿用 dingtalk-config 现有 `getErrorMessage` 模式或提取到组件内共享）。

## Risks / Trade-offs

- [P3 未完成时 dify-ui 组件形态不确定，提前实现会返工] -> 本 change 严格排在 P3 之后启动；design 中组件名（如 `tab-slider-new`）在实现首日以 1.15.0 合并后代码为准复核。
- [contract 输出类型与后端实际响应漂移（oRPC contract 仅编译期约束，无运行时校验）] -> 迁移时逐端点对照 `system_manage_extend.py` 与 `services/system_manage_extend.py` 的实际返回结构核对；测试 fixture 采用真实响应形状。
- [admin 角色获得系统管理入口可见性，属权限行为变化] -> 该放宽与后端既有执行口径一致（admin 本就能直接调 API）；在验收中用 owner/admin/普通成员三角色手动验证，并在 change 说明中向运营方明示。
- [删除 `web/models/system-manage-extend.ts` 可能有本 change 范围外的引用] -> 迁移前 `rg "models/system-manage-extend"` 全仓确认引用面；如有范围外引用（如 signin 流程复用 OAuth2Config 类型），改为从 contract 导出类型统一供给。
- [分页组件行为差异（`base/pagination` 与手写分页的 page/pageSize 语义）] -> 迁移时保持后端参数语义（page 从 1 起、page_size 白名单 10/30/50/100），必要时用组件的受控 props 适配。
- [并行改造多个组件时 contract 返工造成连锁返工] -> tasks 中将 contract+数据层设为先行串行项，UI 并行改造只在 contract 评审通过后启动（见 tasks 并发执行策略）。

## Migration Plan

纯前端重构，随常规发版上线，无数据迁移、无部署顺序要求。

- 上线顺序建议单 PR 或按"contract+数据层 → 页面改造"两个 PR；每个 PR 独立通过 lint/type-check/vitest。
- 回滚：git revert 即可，无状态残留。
- 上线后验证：owner/admin/普通成员三角色分别登录，验证入口可见性（前两者可见、成员不可见）与 6 个端点功能正常（额度列表/编辑、dingtalk/oauth2 保存与测试、email-api 测试、forward-token 增删）。

## Open Questions

- （实现期确认，不阻塞规划）1.15.0 合并后 base tab 组件族与 `base/ui/dialog` 的最终 API 形态；若 `base/pagination` 未内置 i18n 文案，则分页 key 落入 `extend.json`。
- （实现期确认）quota-management 测试的 mock 手段选 MSW 还是 mock `@/service/client`——以当时仓库 `frontend-testing` skill 与既有 spec 先例为准，二者均满足 D5 的层级要求。
