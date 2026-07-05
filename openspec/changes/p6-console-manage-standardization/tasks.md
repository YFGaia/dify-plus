# Tasks: Console 系统管理入口规范化

> 前置条件：P3（upstream 1.15.0 合并）已完成；启动前先复核 base（dify-ui）组件族与 `base/ui` overlay 原语的最终形态（design.md D3/Open Questions）。

## 1. Contract 与数据层（先行，阻塞其他分组）

- [ ] 1.1 新建 `web/contract/console/system-manage-extend.ts`：按 design.md D1 定义全部 6 组端点的 oRPC contract（对照 `api/controllers/console/system_manage_extend.py` 逐端点核对请求/响应结构；无输入 GET 省略 `.input(...)`；`DELETE .../forward-tokens/{seq}` 用 path param 写法；类型不使用 `any`），并把 `web/models/system-manage-extend.ts` 的类型并入 contract schema。完成标准：`pnpm type-check:tsgo` 通过。
- [ ] 1.2 在 `web/contract/router.ts` 的 `consoleRouterContract` 注册 `systemManageExtend` 命名空间。完成标准：type-check 通过，`ConsoleInputs` 推断正常。
- [ ] 1.3 新建 `web/service/use-system-manage-extend.ts`：按 design.md D2 定义带失效逻辑的 mutation hooks（quotaSet→invalidate quotaList；dingtalk/oauth2 save→invalidate 各自 config；forwardToken create/delete→invalidate token 列表；test 类 mutation 无失效、可留在组件内直接 `mutationOptions()`）。完成标准：失效逻辑全部位于 service 层 `onSuccess`，无组件层 `invalidateQueries`。
- [ ] 1.4 contract 评审（主 agent）：对照后端 controller 与 `services/system_manage_extend.py` 实际返回结构复核 schema、命名与 key 结构；评审通过后才允许启动分组 2-4 的并行改造。

## 2. 页面改造：quota-management（依赖 1.4）

- [ ] 2.1 `quota-management/page.tsx` 数据层迁移：列表改 `useQuery(consoleQuery.systemManageExtend.quotaList.queryOptions(...))`（分页/搜索经 query input 驱动，可用 `placeholderData: keepPreviousData`），额度保存改用 1.3 的 mutation hook；删除手写 `useState/useEffect` loading/refetch 编排与 `catch (e: any)`。
- [ ] 2.2 UI 规范化：表格与操作列样式对齐 base 规范；分页替换为 `base/pagination`（保持 page 从 1 起、page_size 10/30/50/100 语义）；搜索输入/按钮替换为 `base/input`/`base/button`；编辑弹窗改 `base/ui` overlay 原语（保留非负数字校验、Enter 确认、失败提示）。
- [ ] 2.3 Toast 迁新 `ui/toast`；分页控件硬编码中文入 i18n（若 `base/pagination` 自带 i18n 则复用其 key），新增 key 先写 zh-Hans/en-US。
- [ ] 2.4 重写 `quota-management/__tests__/page.spec.tsx`：删除对 `@/service/system-manage-extend` 的 mock，按 design.md D5 改为 QueryClientProvider + mock query/contract 层，覆盖首屏加载、非法输入拦截、保存成功后列表失效刷新三个既有行为。完成标准：`pnpm vitest run` 该 spec 全绿。

## 3. 页面改造：system-integration（依赖 1.4，四个组件可再细分并行）

- [ ] 3.1 `system-integration/page.tsx`：tab 按 design.md D3 保留本地 state，手写 tab 按钮替换为 base tab 组件。
- [ ] 3.2 `dingtalk-config.tsx`：数据层迁 consoleQuery（config query + save/test mutation），输入/按钮/开关布局复用 base 组件；补组件测试（参照 `frontend-testing` skill 模式）。
- [ ] 3.3 `oauth2-config.tsx`：同 3.2（config query + save/test mutation + base 组件 + 测试）。
- [ ] 3.4 `email-api-config.tsx`：test 改 `useMutation(...mutationOptions())`；Toast 迁新 `ui/toast`；顶部硬编码中文说明入 `extend.json`（新增 `systemManage.emailApi.description`，zh-Hans/en-US）；清除 `catch (e: any)`；输入/按钮换 base 组件；补测试。
- [ ] 3.5 `forward-token-list.tsx`：列表迁 query、创建/删除迁 1.3 的 mutation hooks；`globalThis.confirm` 换 `base/confirm`；Toast 迁新 `ui/toast`；清除 `catch (e: any)`；表格/输入/按钮换 base 组件；补测试（含删除确认流）。
- [ ] 3.6 删除 `web/service/system-manage-extend.ts` 与 `web/models/system-manage-extend.ts`（分组 2、3 全部 call site 迁移完成后执行；删除前 `rg "service/system-manage-extend|models/system-manage-extend" web/` 确认范围外引用已改为从 contract 导入）。

## 4. 权限口径统一（依赖 1.4，可与分组 2、3 并行）

- [ ] 4.1 `web/app/components/header/system-manage-nav-extend/index.tsx` 与 `web/app/(commonLayout)/system-manage-extend/layout.tsx` 的判断从 `isCurrentWorkspaceOwner` 改为 `isCurrentWorkspaceManager`。
- [ ] 4.2 复核后端 `system_admin_required_extend` 保持 `is_admin_or_owner` 且覆盖全部 6 组端点（预期零代码改动；如装饰器 docstring 与口径描述不一致，仅更新注释）。

## 5. i18n 收尾（依赖分组 2、3 的新增 key 定稿）

- [ ] 5.1 用 `web/i18n-config/auto-gen-i18n.js` 将新增 key 批量翻译到其余 18 个 locale 的 `extend.json`；人工抽查 zh-Hant/ja-JP 质量。
- [ ] 5.2 运行 `pnpm run i18n:check`，extend namespace 无缺失 key；`rg -n "[\u4e00-\u9fff]" web/app/\(commonLayout\)/system-manage-extend/ --glob '!**/__tests__/**'` 确认 JSX 渲染文案无硬编码中文。

## 6. Parallelization Plan（并发执行策略）

- [ ] 6.1 **串行先行**：分组 1（contract + 数据层 + service hooks）是其他所有分组的依赖，由主 agent 或单一 sub-agent 独立完成，1.4 评审通过前禁止启动下游改造——避免 contract 返工造成连锁返工（design.md 风险项）。
- [ ] 6.2 **页面并行**：1.4 通过后，以页面为冲突边界启动并行 sub-agent——sub-agent A 负责分组 2（quota-management 全部任务 2.1-2.4），sub-agent B-E 分别负责 3.2-3.5（每个 integration 组件一个 agent，3.1 归入 B）。context inputs：本 change 的 proposal/design/specs、`web/AGENTS.md`、`frontend-query-mutation` 与 `frontend-testing` skill、分组 1 产出的 contract 与 service hook 文件（只读）。允许编辑范围：各自页面/组件文件、对应 `__tests__`、`web/i18n/zh-Hans|en-US/extend.json`（i18n key 按组件前缀分域避免冲突，如同文件冲突由主 agent 汇总合并）。预期产出：迁移后组件 + 通过的 spec + 新增 key 清单。
- [ ] 6.3 **独立并行**：分组 4（权限统一，前端两处小改 + 后端复核）改动面与页面改造无交集，可由独立 sub-agent 与 6.2 同时进行。
- [ ] 6.4 **主 agent 收尾**：3.6（legacy 文件删除）、分组 5（i18n 批量生成，需汇总各 sub-agent 的 key 清单后统一执行避免 20 文件并发写冲突）与分组 7 最终回归由主 agent 顺序完成。

## 7. Architecture Verification（最终回归，主 agent）

- [ ] 7.1 静态检查：`pnpm lint`、`pnpm type-check:tsgo` 全绿。
- [ ] 7.2 残留扫描：`rg "base/toast" web/app/\(commonLayout\)/system-manage-extend/`、`rg "globalThis.confirm" web/app/\(commonLayout\)/system-manage-extend/`、`rg "service/system-manage-extend|models/system-manage-extend" web/`、`rg "from '@/service/base'" web/app/\(commonLayout\)/system-manage-extend/` 均无匹配；确认无新增 deprecated overlay import（`@/app/components/base/modal` 等旧 overlay 入口）。
- [ ] 7.3 测试回归：`pnpm vitest run` 覆盖 quota-management 重写 spec 与 system-integration 4 个新增 spec，全绿。
- [ ] 7.4 双角色手动验证（对照 specs/console-system-manage-access-control）：owner 与 admin 均可见系统管理入口并正常使用 6 组功能（额度列表/搜索/分页/编辑、dingtalk 保存/测试、oauth2 保存/测试、email-api 测试、forward-token 增删）；普通成员入口不可见、直访 URL 显示无权限提示、直调 API 返回 403。
- [ ] 7.5 contract 一致性抽查：以真实后端响应对照 contract 输出类型（至少 quotaList 与 dingtalkConfig 两个端点），确认无字段漂移与类型断言逃逸。
