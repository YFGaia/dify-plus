# M05 只读实施前分析

- 分析节点：M05 Console transport 与前端契约；分析模型：Sol (`/root/m05_analysis`)。
- 分析基线：`3620896b7afe8122f5e4873450031cc942c8c175`；上游固定 SHA：`8387590ace4a094de812b7847fc6a4c3a27cd52b`。
- 分析期间没有修改文件、分支或索引。M05 前置 M03 已 passed；M04 已 passed，可引用但不改变 M05 的契约来源。

## 必须防止的双源与字段漂移

1. `packages/contracts/openapi-ts.api.config.ts` 的 `filterContractOperations` 会保留所有有 2xx 响应的操作，`splitConsoleDocument` 再按 URL 首段生成 Console router。M02 已给 `/login_config_bootstrap` 与 `/login_config` 提供响应 schema（见 `evidence/M02/schema-verification.json` 与 `api/controllers/console/feature.py`），但当前以及固定上游 1.17.1 的 generated router 均没有 `loginConfigBootstrap` / `loginConfig`。第一次刷新合同后，这两项将成为生成 segment；现有 `web/contract/router-extend.ts` 仍手工注册同名顶层键。
2. 集成负责人冻结 M05 方案：生成的 Console contract 是这两个后端端点唯一的类型和运行时路由 owner；移除手工重复端点，只在 fork 扩展中保留确实不在 OpenAPI 的 `systemManage`。动态 loader 应消费生成 loader，并只给 `systemManage` 加 fork loader。禁止依赖对象展开顺序覆盖。
3. M02 后端 `LoginConfigModelExtend.is_custom_auth2` 是 boolean，并无 `is_custom_auth2_button`；当前 `web/features/system-features/extend.ts` 把前者声明为 string，且以 `GetSystemFeaturesResponse` 表示登录配置。M05 必须分离公开快照 DTO 与 fork 登录配置 DTO，按 M02 实际 schema 建型；A04 C03 的按钮字段差异作为限制记录，不得伪造字段。若该按钮是必需产品输入，需交回后端 owner 补齐后再验收。
4. 后端已输出 `admin_extend`、`tenant_extend`，但当前生成的 `CurrentWorkspaceSummaryResponse` 缺少它们。只通过既有 OpenAPI 生成流程更新 generated 类型，不手改 `packages/contracts/generated/**`。

## 冻结的实现决策

- 使用生成合同作为 login_config bootstrap/config 的唯一 runtime 和 DTO owner；fork router 只保留 `systemManage`。
- 公开 `/system-features` 与受 bootstrap 保护的 `/login_config` 使用不同 response type、query/cache key；不把登录配置或受保护 license 字段塞进公开 snapshot。
- bootstrap token 仅驻留浏览器 transport 内存；SSR 不持久化、不跨请求复用。配置响应形状在写入 query cache/hydration 前校验。
- workspace summary 通过正常合同生成获得两个权限位，normalizer/defaults 与 permission atom 保留真实值；缺字段不得静默伪装成授权结果。

## 最小分步实现与验收路径

| 子任务 | 范围与定位                                                                                                                                                                                                                                                                                                                   | 验收                                                                                                                                          |
| ------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| 11.1   | `packages/contracts/console.ts`；`web/service/console/{browser,contract-loader}.ts`；`web/contract/router-extend.ts` 和 fork-only `web/contract/console/system-manage.ts`。刷新 contracts 后去掉两条手写 endpoint，保留 systemManage 专属加载；浏览器先 bootstrap，再对 login_config 注入短期 Header，同时保留 Cookie 路径。 | 生成与手写 top-level router 不重复；Cookie/跨域 Header 两种路径均按顺序工作；403 清掉当前 token 并按明确策略重取；SSR 不接触此 token。        |
| 11.2   | `web/features/system-features/{client,server}.ts` 和其 tests；在任何 query cache/hydration 写入前验证真实 public snapshot 核心 shape。登录配置使用独立 query/options 和身份边界。                                                                                                                                            | `{ping:true}` 与缺少核心字段的 HTTP 200 不入缓存；optional SSR 返回 undefined、hard 读取继续抛错；匿名 SSR 不请求 login_config/license 详情。 |
| 11.3   | 更新生成后调整 `web/context/app-context-defaults.ts`、`app-context-normalizers.ts`、`app-context-extend.ts`；迁移旧 loader/client imports，包括 `web/app/(commonLayout)/system-manage-extend/code-execution-control/page.tsx` 的旧 client 导入。新增/跨 owner 路径先登记。                                                   | admin_extend 与 tenant_extend 从真实 summary 进入 atom；切工作区/登出时随身份更新；既有管理页面角色边界不变。                                 |
| 11.4   | 对照 `evidence/M02/schema-verification.json`、`evidence/M02/result.json`、`evidence/M03/{result.json,handoff.md}` 与 A04 C01–C04/C08 的请求、字段、错误、缓存语义；扩展 Console client、SSR、system-features、workspace context 定向测试。                                                                                   | 公开快照、fork 登录配置、完整 license 各自拥有独立数据键；身份切换无跨账号残留；403/401/409 按所属端点保留，不因兼容回退吞错。                |
| 11.5   | 独立冻结源码和测试提交、绑定输入 hashes/运行命令/结果，独立验收后更新状态。                                                                                                                                                                                                                                                  | 不恢复已删除的旧 REST client/loader 为第二套 transport；证据绑定准确代码 HEAD。                                                               |

## 现状、风险与建议验证

- 当前公开 system-features query key：`['console','systemFeatures','get']`；登录后 license 是独立 `systemFeatures.license.get`。浏览器与 SSR 查询目前都没有先校验成功响应形状；SSR optional/hard 分别位于 `web/features/system-features/server.ts`，应保持 optional 软失败、hard 抛错。server Console transport 使用 request-local `cache` 并转发当前 Cookie/CSRF；不要把身份移入全局 link 缓存。
- logout 当前清除 QueryClient；登录路径主要重置 profile query。需要明示 fork login config 与 summary 的身份失效策略，尤其切账号和 workspace 切换。
- `web/service/console/client.spec.ts`、`server.spec.ts`、`web/features/system-features/__tests__/server.spec.ts`、`web/context/__tests__/console-bootstrap.spec.tsx` 是定向测试宿主；响应 shape fixture 需改为 M02 真实模型。
  -建议测试：bootstrap/config 顺序、Cookie/Header、403 后 token 处理、动态 segment；SSR 并发 Cookie/CSRF 隔离；ping/缺核心字段 shape guard 与 optional/hard/dehydrate；workspace 权限位切换。按 `web/docs/test.md` 策略从 `web/` 运行对应 `vp test run --project unit -- ...`。源稳定后遵循 `web/docs/lint.md` 的 `vp run -w check`；真实跨域 Cookie 仍待 V02 浏览器验收。
- 首次生成可能改动 `packages/contracts/generated/**`、`web/context/app-context-defaults.ts`、`web/context/app-context-extend.ts`、`web/test/console/current-workspace.ts`；代码执行控制页属于跨 M06 owner，须先登记移交或只改引用并登记。新测试文件也先登记在 `research/conflict-ownership.tsv` 与 M05 节点 owner 清单。
- 主要上游冲突面是生成器、Console transport 与生成产物；沿用生成的运行时/DTO、把 fork 自有差异限制在独立 loader/query/context 投影中，可降低后续上游更新冲突。

## 源码证据定位

- `packages/contracts/openapi-ts.api.config.ts:248-252`（成功响应过滤）；`:515-536`（按 Console segment 写生成入口）；`:551-567`（按 URL 首段拆段）。
- `web/contract/router-extend.ts:1-22`（当前重复的手写 loginConfig 键与 systemManage）。
- `api/services/login_config_service_extend.py:12-26`（真实字段类型与实际扩展）；`api/controllers/console/feature.py:39-50,82-109,182-210`（路由 schema 与公开/受保护分界）。
- `packages/contracts/generated/api/console/workspaces/types.gen.ts:840-`（summary 生成类型）；`api/controllers/console/workspace/workspace.py:122-`、`api/services/workspace_service.py:117-118`（后端两个权限字段）。
- `web/features/system-features/server.ts:10-29`、`web/features/system-features/client.ts:1-`（SSR 与客户端查询）；`web/service/console/server.ts:39-57`（request identity cache）。
- `web/context/app-context-normalizers.ts:35-46`、`web/context/app-context-extend.ts:1-`（summary normalize 与权限 atom）。

M05 的编码拆成独立新任务，每完成一个 11.x 子项，由集成负责人补齐结果证据、更新 execution graph/tasks.md 并精确提交；独立验收单独开新 Luna 任务。
