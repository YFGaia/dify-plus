# 冲突解决作战单（任务 2.5 产出，4 份侦察结论汇总）

> 侦察时间：2026-07-05；基线 fork HEAD = 4071db17（含 P0/P1），目标 upstream `1.14.2` = fd98157034。
> 实际 merge 冲突 65 个文件（全量清单见 conflict-list.txt），解决策略与执行结果如下。

## 关键侦察结论（推翻/修正设计预判的部分）

1. **web monorepo 化发生在 1.14.0，不是 1.15.0**：`pnpm-workspace.yaml` + `packages/`（contracts/dify-ui/dev-proxy/iconify-collections/tsconfig）+ pnpm catalog 随本次合并进入仓库；headlessui 在 1.14.1 已清零。p3 的范围需据此修订。
2. **`dify_graph` 在 1.14.2 已外部化为 PyPI 包 `graphon~=0.4.0`**，`api/dify_graph/` 目录不复存在。fork 的两个残留副本（`nodes/code/control_extend.py`、`variable_assigner/common/impl.py`）为孤儿（无引用者），已删除；fork 的 canonical 副本保留在 `api/core/workflow/nodes/code/control_extend.py`。
3. **10 项计费挂点宿主文件在 1.14.2 全部原地存在**，无一删除/移动；主要冲击为 `graphon` 改名、`apikey.py` Pydantic 化、OAuth state 改 base64-JSON、app_generator 的 `_bind_file_access_scope` 重缩进。
4. **service 层「显式传 session」并未成为对外签名要求**，fork extend service 的调用面基本兼容；唯一断链的 `model_provider_service_extend.py`（凭据池重构）在合并前 HEAD 已是坏的，登记为存量债务（p4/p6 处置）。
5. `ef2b5d6107`（llm_quota 移入 graph 层）实际已包含在 1.13.3 中；1.14.2 又重写了 `llm_quota.py`（从节点公开配置解析模型身份），与 fork 计费派发继续互不调用。

## 按域处理策略与结果

| 域           | 文件（代表）                                                                                             | 策略                                                                                                                                                                                                                                                     | 结果             |
| ------------ | -------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------- |
| 构建/依赖    | pyproject.toml / uv.lock / pnpm-lock.yaml / package.json / Dockerfile                                    | 取上游（uv workspace / pnpm catalog）+ 追加 fork 依赖（alibabacloud_dingtalk、pypinyin；serwist、dingtalk-jsapi、lodash-es）；Dockerfile 回归上游 `uv sync --frozen` 流并保留 aliyun 镜像加速（requirements.docker.txt 流废弃删除）                      | ✅               |
| 挂点 1       | `layers/persistence.py`                                                                                  | 保留 fork 派发（`_handle_node_succeeded` 原地），import 切 `graphon.*`；fork 任务文件同步切换                                                                                                                                                            | ✅               |
| 挂点 2-4     | 5 个 `app_generator.py` + 2 个 `generate_task_pipeline.py`                                               | 取上游重写版，按侦察坐标重挂 `extras["app_token_id"]`/`account_id` 与 `ApiTokenMessageJoinsExtend`；workflow pipeline 写入加 `WorkflowStartReason.INITIAL` 防 resume 重复                                                                                | ✅               |
| 挂点 5       | `service_api/wraps.py` + 9 controller                                                                    | 额度前置校验重构为 `validate_token_quota_extend()`（返回 owner join 供归因映射）；controller 的 `api_token` 参数改为 `ApiToken \| None = None` 收窄测试破坏面                                                                                            | ✅               |
| 挂点 6       | `console/apikey.py`                                                                                      | 按上游 Pydantic `ResponseModel` 路线重写，6 个额度字段入 `ApiKeyItem`，LEFT JOIN 联查/POST 建额度记录/PUT 额度编辑/DELETE 软删全保留                                                                                                                     | ✅               |
| 挂点 7       | events                                                                                                   | 上游无结构冲突，fork handler 与 Events shim 原样保留                                                                                                                                                                                                     | ✅               |
| 挂点 8       | `ext_celery.py`                                                                                          | 3 个 extend beat 任务块平移至 enterprise telemetry import 之后                                                                                                                                                                                           | ✅               |
| 挂点 9       | `token_buffer_memory.py`                                                                                 | fork 上下文分割保留；修复合并期重复追加同内容 assistant 消息的问题（name 标记改挂在 assistant 消息本体）；恢复 1.13.3 合并时丢失的 `AppRunner.add_messages_context` 与 `control_registers` 全链路（prompt_transform → simple_prompt_transform → memory） | ✅（含断链修复） |
| 挂点 10      | `libs/oauth.py` + `auth/oauth.py`                                                                        | `OaOAuth.get_authorization_url` 对齐三参数签名并改用 `encode_oauth_state`（否则 invite_token 在上游 `decode_oauth_state` 侧丢失）；casdoor 兼容块在新 callback 结构重插                                                                                  | ✅               |
| api 其他     | model.py / feature.py / account_service.py / feature_service.py / statistic.py                           | 保留 fork 内嵌模型（外迁在 6.2 单独提交）、CVE 包装、额度初始化；`StatisticTimeRangeQuery` 补 `account` 字段；`get_system_features` 的 extend 查询加 db 不可用降级                                                                                       | ✅               |
| web          | router.ts / app-context / normal-form / explore 4 文件 / secret-key-modal / invite-modal / header / chat | 取上游为底重挂全部 extend 块；`@/utils/classnames`→`dify-ui/cn`、旧 base 组件→dify-ui 对应组件（Switch/Select/Slider/Dialog/AlertDialog/toast）批量迁移 11 个 extend 文件                                                                                | ✅               |
| explore 分类 | `database_retrieval.py` + 前端                                                                           | 后端产出上游 `categories` 列表形状、数据源仍为 fork 自建分类表（原生化属 p3）；前端过滤逻辑适配 categories 列表                                                                                                                                          | ✅               |

## 测试适配（fork 行为导致的上游用例调整）

- service_api / web controller 单测：conftest autouse 绕过 fork 额度/登录前置校验（校验逻辑保留生产行为）。
- explore webapp-auth 过滤 5 个用例 skip（fork 应用中心特意移除 enterprise webapp-auth）。
- `SystemFeatureApi` 用例改测 fork 的 `LoginConfigApi`（CVE-2025-63387 门禁）。
- 若干 `unwrap` 助手按上游自身新模式（`test_message.py` 的 bound_self 重绑定）适配 fork 装饰器链。

## 并发事故记录

p5-admin-decommission 会话在本合并进行期间并行写入同一工作区（详见任务 8.2 备注）。本 merge commit 吸收了已被 p5 写入 tracked 文件的 code-execution-control 功能及其配套新文件（无法从合并树中分离），admin/ 删除、compose/entrypoint 清理与 p5 文档改动仍留给 p5 会话提交。
