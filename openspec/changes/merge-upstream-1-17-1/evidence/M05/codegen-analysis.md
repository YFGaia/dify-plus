# M05 11.1 合同生成复现与唯一 owner 复核

分析任务：独立 Sol 只读调查（`/root/m05_codegen_analysis`）及 OPTIONS 最小复现（`/root/m05_extend_codegen_repro`）。两项调查都没有修改主 checkout。以下记录将早期“业务方法触发生成失败”的推测更正为复现出的实际原因。

## 最小复现结论

失败的操作是 OpenAPI 中 `OPTIONS /extend/{path}`，不是 GET、POST、PUT、PATCH 或 DELETE。Console 规范同时包含这五个业务方法和由 Flask-RESTX 暴露的 OPTIONS 预检操作。`filterContractOperations` 只处理 `operationMethods` 中的业务方法；对于 OPTIONS，循环直接跳过，导致该操作留在路径对象中，并进入后续 Console segment 拆分和 oRPC 生成阶段。oRPC 插件因此报 `Symbol finalName has not been resolved yet`。

隔离副本：`/private/tmp/dify-m05-extend-diagnostic/packages/contracts`。在该目录运行 `pnpm --config.verify-deps-before-run=false exec openapi-ts -f openapi-ts.api.config.ts` 的结果：

| 输入 | 结果 | 临时日志 |
|---|---:|---|
| 原始 `extend` segment（含 OPTIONS） | exit 1 | `/private/tmp/dify-m05-extend-all.log` |
| 仅 OPTIONS | exit 1 | `/private/tmp/dify-m05-extend-options-only.log` |
| 五业务方法且删去 OPTIONS | exit 0 | `/private/tmp/dify-m05-extend-all-no-options.log` |
| 各业务方法单独保留且删去 OPTIONS | 五项全部 exit 0 | `/private/tmp/dify-m05-extend-{delete,get,patch,post,put}-no-options.log` |

插件隔离显示 TypeScript 与 Zod 单独生成成功，oRPC 插件触发失败。修改响应 schema、operationId、路径参数和嵌套名称仍不能修复保留 OPTIONS 的生成。五份 API spec 中，`OPTIONS /extend/{path}` 只出现在 Console spec。

复现修复是在临时副本的 `filterContractOperations` 中过滤 `method === 'options'`。原始后端路由和发布 OpenAPI 均不需修改。完整配置在 `/private/tmp/dify-m05-full-codegen-fix/packages/contracts` 运行 `pnpm --config.verify-deps-before-run=false exec openapi-ts -f openapi-ts.api.config.ts`，64 个生成 job 全部成功（exit 0）；extend segment 仍生成五个业务方法，未生成 OPTIONS。临时完整生成新增 7 个 Console segment 和 21 个 shard 文件，另刷新 `router.gen.ts` 与 `orpc.gen.ts` 两个入口。上述结果是隔离生成证据；完整生成物尚未复制到主 checkout。

保留日志的 SHA-256：原始失败 `77a338998954c92d0df694833f91dc0addfc1b2d639033af1497d84aab0758dd`；OPTIONS-only 失败 `0a48f505a4ab5e63c4a4442b844714027197c7691da3ebff44bf94062540cac8`；无 OPTIONS 成功 `9185f65869fd02c9fe477d83f7f191198cc02e5990aca16907ba429d535914fb`；全量修复生成成功 `e385935c0052fdccad438d4647f2139b359ebd3981d052a3462fc0f3bae33f79`。复现 agent：`/root/m05_extend_codegen_repro`。

## 生成 scope 与重复端点 owner

全量生成新增的 7 个有效 Console segment 是 `dingTalk`、`extend`、`installed`、`loginConfig`、`loginConfigBootstrap`、`message` 和 `systemManageExtend`。7 段对应 21 个 shard 文件；登录配置的 6 个 shard和两个 router 入口此前已登记。按仓库根目录 `vite.config.ts` 格式化后，完整生成还会修改 16 个既有输出（含两个已登记入口），并新增 21 个 shard。主 checkout 尚待登记的范围为 14 个既有输出、15 个新增 shard 和共享生成配置 `packages/contracts/openapi-ts.api.config.ts`，总计 30 条路径。临时生成目录最初缺少根格式化配置；复制仓库 `vite.config.ts` 与 `lint.config.ts` 后重跑 `vp fmt`，再和主 checkout 比较得到以上文件范围。`/mcp/oauth/callback` 仅返回 302，被 2xx operation 过滤排除，不构成新的 segment。

`systemManageExtend` 中代码执行控制 GET、POST、DELETE 三项与手写 `web/contract/console/system-manage.ts` 使用完全相同的 HTTP 方法和路径；DELETE 的参数名为 `record_id` / `id`。手写 DTO 精度更高，服务端 POST 实际返回 201，而当前 Swagger 声明为 200。虽然 router 顶层键不同，不会发生 JavaScript 键覆盖，但保留双方会形成双 runtime/DTO owner。采用唯一 owner：保留精确的手写 `systemManage` DTO/runtime，并按方法与完整路径从生成合同排除这三项，保留同一 segment 其余 13 项。仅在 aggregation 隐藏 generated key 不够，因为 loader 可直接加载 shard。手写 POST 对真实 201 的行为尚未验证，M05 测试须覆盖此状态及返回 DTO。

其他新 segment 的对应后端 controller 和 route 已列于前一轮调查：`evidence/M05/codegen-investigation.json` 的 `new_console_segments_reported`。目前主 checkout 仍没有复制完整生成物，11.1 尚未完成。实施前须登记 `packages/contracts/openapi-ts.api.config.ts` 和另外 15 个 generated shard 路径。

## 归因边界

`/extend/{path}` 的五个业务路由及 `options()` 已存在于 fork 1.16.0。M02 未改该 controller、生成配置或依赖锁；`@hey-api/openapi-ts` 在 1.16.0 与 1.17.1 都锁为 0.98.2。现有证据足以证明本次失败由 OPTIONS 进入 oRPC 导致，不足以证明 1.16.0 基线在当时的其它输入上也会失败。生成配置文件也是 upstream 1.17.1 维护的共享文件；实施只增加紧凑的合同过滤规则，不改发布 OpenAPI、后端路由或依赖，后续更新仍需复核此局部改动。
