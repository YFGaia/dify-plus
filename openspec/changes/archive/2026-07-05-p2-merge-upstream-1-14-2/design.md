# Design: 合并上游 1.14.2（Step A）

## Context

- fork 基线：`merge/upstream-1.13.3`（P0 完成后：4 处计费挂点已恢复、无冲突标记残留、6 条计费回归链路全绿、已打 tag `fork-pre-merge-1.15.0`）。
- 目标：upstream tag `1.14.2`（已 fetch 到本地）。1.13.3 → 1.14.2 共 1020 提交；api 2,166 文件（+130,597/-103,591）、web 4,589 文件（+164,306/-131,196）、上游新增 3 个 DB 迁移。
- 本 change 是线路图 Phase 2 两步合并的 Step A；Step B（1.14.2 → 1.15.0，web monorepo/main-nav）由 `p3-merge-upstream-1-15-0` 承担。
- 约束：**同一 merge 状态不可并行编辑**——冲突解决必须由主 agent 顺序完成；合并期间不引入 fork 新功能，只做「解冲突 + 挂点重挂 + 附带的低风险结构改进」。
- 决策 D1（已拍板）：本次保持 fork 自建额度体系，不接入上游 quota v3。

### 上游 1.13.3 → 1.14.2 关键冲击（调研结论）

1. **service 层「显式传 session」重构 + SQLAlchemy 2.0 `select()` 迁移**：FeedbackService、TagService、FileService、ApiKeyAuthService 等大量方法签名从隐式 `db.session` 改为显式 `session: Session` 参数；fork 的 `*_extend` service 若调用这些旧签名会批量报错。
2. **console 控制器注入 user/tenant**：上游 `refactor: migrate remaining console APIs to use injected user/tenant`，controller 方法签名普遍变化；fork extend 控制器与被 fork 侵入的上游控制器需跟进。
3. **quota v3**：`BillingService` 新增 `quota_reserve/quota_commit/quota_release` 两阶段配额 + tenant 级 Redis 锁，面向上游 SaaS 计费。
4. **LLM 配额扣减移入 graph 层**：`ef2b5d6107` 新增 `api/core/app/llm/quota.py` 与 `api/core/app/workflow/layers/llm_quota.py`，重写 `llm_utils.py`——与 fork 节点计费挂点（`layers/persistence.py`）同区域。
5. **dify_graph 演进**：dify_graph→graphon 重命名相关重构，fork 在 `dify_graph` 下的副本文件（如 code 节点 `control_extend.py` 副本）路径可能失效。
6. **依赖与 Python 版本**：走向 3.12，gevent/celery 等升级。

## Goals / Non-Goals

**Goals:**

- 产出可信的 `merge/upstream-1.14.2` 基线：构建/导入/迁移通过，6 条计费链路 + SSO + 应用中心 + 系统管理页回归全绿。
- 10 项计费挂点全部在新代码结构下重挂并登记新坐标（更新 `docs/dify-plus/与上游差异总表.md`）。
- fork extend 代码适配上游新签名模式（显式 session、`select()`、user/tenant 注入）。
- 附带改进：`api/models/model.py` 内嵌的 3 个 extend 模型类外迁独立文件。

**Non-Goals:**

- 不接入上游 quota v3（D1）；不改造 fork 额度表结构与语义。
- 不做 web monorepo 化 / main-nav 迁移 / headlessui 清退（p3 范围）。
- 不做 service_api 签名侵入收窄（contextvar 化）、扣费幂等/原子化加固（p4 范围）。
- 不处置 admin/批量工作流（p1/p5 范围）。
- 不追求上游新功能的启用与调优，只保证合并后不回归。

## Architecture Assessment

### Existing Design Reuse

- 复用 fork 一贯的合并模式（见 `docs/dify-plus/合并规划-upstream-1.13.3.md`）：`git merge <tag> --no-commit` → 机械冲突批量解决 → 高危文件人工逐个核对 → 构建验证 → 回归清单。
- 复用 P0 建立的 6 条计费回归链路清单作为验收门槛（Console 调试扣费、Explore 扣费、WebApp 登录+扣费、Service API 日/月限额拦截、workflow LLM 节点扣费、月初额度重置）。
- 复用双 Alembic 链模式：上游 `api/migrations` + fork `api/migrations_extend`（`flask extend_db upgrade`），本次上游 3 个新迁移与 extend 链无命名冲突，无需调整。
- 复用 `api/configs/extend/__init__.py` 配置挂载模式，不新增配置机制。

### Boundaries and Ownership

- **上游代码所有权归上游**：冲突时默认接受上游重构结果（尤其 service/controller 签名、graph 层结构），fork 不逆向保留旧结构。
- **fork 侵入点所有权归 fork**：extend 逻辑（计费挂点、SSO 类、CVE 包装、Events 框架）在上游新结构中重挂，行为语义不变。
- **计费边界**：fork 自建额度体系（`account_money_extend`、`api_token_money_extend` 四表、`forwarding_*`）与上游 `BillingService`（含 quota v3）完全平行——上游 quota v3 代码原样合入但不被 fork 调用路径触发（fork 非 SaaS billing enabled 部署形态下天然不激活；解冲突时不得把 fork 扣费逻辑挂进 quota_reserve/commit/release）。
- **`api/core/app/workflow/layers/` 敏感区**：上游 `llm_quota.py`（provider 配额）与 fork 在 `persistence.py` 的节点计费派发共存于同一 layers 目录；两者语义不同（上游扣 provider credits，fork 按美元扣个人账户），必须保持互不调用。
- **合并执行所有权**：merge 冲突解决 = 主 agent 顺序执行；合并前侦察与合并后分域回归 = 可并行的 sub-agent 任务（详见 tasks.md 并发执行策略）。

### Options and Rationale

- **一步合到 1.15.0 vs 两步**：选两步。1.15.0 的 web monorepo 化与 main-nav 删除属于「重做 UI 挂载点」级别工程，与 api service 层重构混在同一次 merge 会导致回归无法归因。已在线路图定案。
- **quota v3 接入 vs 平行共存**：选平行共存（D1）。fork 按美元金额计费+归因个人账号，与上游按订阅 credits 语义不同；迁移成本高且无业务收益。「评估挂接上游 quota 接口」列为 2.0 长期项。
- **model.py 内嵌 extend 类：原地保留 vs 外迁**：选外迁。`models/model.py` 是历次合并冲突重灾区，3 个内嵌 extend 模型类外迁到独立 `*_extend.py` 文件后，后续合并该文件可直接取上游版本。外迁只动 import 路径不动表结构，风险低、收益长期。

### Quality Attributes

- **可靠性**：以 6 条计费回归链路 + SSO/应用中心回归作为合并完成的硬性门槛；每类冲突解决后跑 py_compile 增量验证。
- **可维护性**：挂点新坐标登记入 `docs/dify-plus/与上游差异总表.md`，作为 Step B 与后续升级的 checklist 输入。
- **可测试性**：`uv run --project api python -c "from app_factory import create_app"` 作为最快冒烟；api 现有单测在合并后跑通（允许上游本身 skip 的除外）。
- **可部署性**：`api/Dockerfile` / `requirements.docker.txt` / `uv.lock` 与上游 Python 3.12 走向对齐；compose 版本号更新；本步不涉及 monorepo 构建体系变化。
- **安全**：保留 fork 的 `controllers/console/feature.py` CVE 防护包装与 `contract/router.ts` CVE 合约（1.13.3 合并已有先例）。

### Complexity and Exceptions

- 不新增抽象、服务、存储、协议。唯一结构变化是 extend 模型类外迁（消复杂度而非增复杂度）。
- 例外说明：合并期间允许临时接受上游代码中 fork 尚未适配的调用点以先达成「可导入」，但提交前必须清零（不允许留 TODO 断链——P0 的教训）。

## Decisions

### DEC-1 不接入 quota v3，物理隔离两套额度体系

- **选择**：上游 `quota_reserve/quota_commit/quota_release` 及 Redis 锁代码按上游原样合入；fork 扣费路径不调用它们，fork 表结构不变。
- **替代方案**：把 fork 额度改挂 quota v3 两阶段接口——否决，语义不匹配（credits vs 美元/个人归因）、迁移需重写全部四条计费线，且 quota v3 在 1.15.0 仍在演进，现在挂接等于追着上游 API 跑。
- **验证**：合并后 grep 确认 fork 代码零引用 `quota_reserve|quota_commit|quota_release`；6 条计费链路回归全绿。

### DEC-2 计费挂点「先定位、后移植、再登记」三段式

- **选择**：合并前由侦察任务给出每个挂点宿主文件在 1.14.2 的去向（原地/移动/重写/删除）；冲突解决时按侦察结论移植；完成后统一登记新坐标。
- **替代方案**：解冲突时随手处理——否决，这正是 1.13.3 合并丢失 4 处挂点的根因（无清单核对，冲突解完即认为完成）。
- **挂点清单**（10 项，合并时逐项确认新位置并验证行为）：

| #   | 挂点                                                                                                      | 1.13.3 坐标                                                                     | 1.14.2 预判风险                                                                                                                                |
| --- | --------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | workflow 节点计费 Celery 派发（`update_account_money_when_workflow_node_execution_created_extend.delay`） | `api/core/app/workflow/layers/persistence.py` 的 `_handle_node_succeeded`       | **高**：`ef2b5d6107` 在同目录新增 `llm_quota.py` 并重写 llm 节点用量流转，`_handle_node_succeeded` 可能被重构；需确认 token 用量数据源是否位移 |
| 2   | `extras["app_token_id"] = api_token.id` 写入                                                              | `api/core/app/apps/workflow/app_generator.py`、`advanced_chat/app_generator.py` | **高**：上游对 app_generator 做 200-300 行级重写                                                                                               |
| 3   | `ApiTokenMessageJoinsExtend` 关联记录写入                                                                 | `advanced_chat/generate_task_pipeline.py`、`workflow/generate_task_pipeline.py` | **高**：pipeline 同步重写                                                                                                                      |
| 4   | chat/agent_chat/completion 三个 `app_generator.py` 的关联记录写入                                         | `api/core/app/apps/{chat,agent_chat,completion}/app_generator.py`               | 中：同类重写波及                                                                                                                               |
| 5   | `validate_app_token` 额度前置校验 + `EndUserAccountJoinsExtend` 映射                                      | `api/controllers/service_api/wraps.py`（波及 ~9 个 controller、30+ 方法签名）   | **高**：上游每版都动此文件；1.14.2 的 user/tenant 注入重构直接命中                                                                             |
| 6   | API 密钥额度字段联查（`description`、`accumulated_quota`、`day_limit_quota` 等）                          | `api/controllers/console/apikey.py`                                             | **高**：历次合并最难文件，上游持续 `select()` 重构                                                                                             |
| 7   | `message_was_created` handler 注册（消息扣费）                                                            | `api/events/` + `api/events/__init__.py`（fork 自带 Events 框架）               | 中：确认信号仍存在、触发语义未变                                                                                                               |
| 8   | extend beat 任务注册（3 个额度重置定时任务，队列 `extend_low/extend_high`）                               | `api/extensions/ext_celery.py`                                                  | 中：celery 大版本升级 + 上游 beat 结构调整                                                                                                     |
| 9   | `messages_context_handling` 消息上下文处理                                                                | `api/core/memory/token_buffer_memory.py`                                        | 中：上游可能移动/重构该模块                                                                                                                    |
| 10  | `OaOAuth`/钉钉 OAuth 类                                                                                   | `api/libs/oauth.py` + `controllers/console/auth/oauth.py`                       | 中：上游 oauth_access_tokens 体系演进                                                                                                          |

### DEC-3 extend 代码适配方向：跟随上游新模式，而非兼容层

- **选择**：fork `*_extend` service/controller 直接改写为显式 session / `select()` / user-tenant 注入的新写法。
- **替代方案**：写适配 shim 让 extend 代码保持旧写法——否决，shim 本身成为新侵入面，且 Step B 还会再改一轮。

### DEC-4 model.py extend 模型类外迁（附带改进）

- **选择**：`api/models/model.py` 内嵌的 3 个 extend 模型类迁出到独立 `api/models/*_extend.py` 文件，`models/__init__.py` 保持导出兼容；在冲突解决完成、验证通过之后单独提交执行，不与 merge commit 混在一起。
- **替代方案**：原地保留——否决，`model.py` 每次合并都是重灾区，外迁一次性把该文件的 fork 侵入清零。

## Risks / Trade-offs

- [挂点 1 所在的 `_handle_node_succeeded` 被上游 graph 层重构彻底重写，token 用量数据源位移导致扣费金额错误] -> 侦察任务先输出 `git log --follow` + diff 结论；移植后用「手动触发 workflow、断言 `account_money_extend.used_quota` 增量与 token 单价一致」验证，而不只验证「有扣费」。
- [wraps.py / apikey.py 冲突解决时误删 fork 字段或校验分支] -> 逐文件对照 1.13.3 fork 版本的 fork-only diff 清单核对；回归链路 4（Service API 限额拦截）必须包含日限额与月限额两个拦截用例。
- [`*_extend` service 隐式依赖旧 `db.session` 语义，改显式 session 后事务边界变化引发双写/漏写] -> 适配时逐个确认事务提交点；计费回归含「扣费与消息写入同事务」检查。
- [celery 大版本升级导致 extend beat 任务静默不注册] -> 验收含 `celery inspect registered` / beat schedule 列表断言 3 个 extend 任务存在（P0 已有先例检查方式）。
- [dify_graph→graphon 重命名使 fork 在 `dify_graph` 下的副本文件成为孤儿] -> 侦察任务盘点 fork 在 `api/dify_graph/` 的全部文件去向；孤儿文件删除或迁移，禁止留双份。
- [合并规模超预期（1.13.3 时预估 3 个冲突实际 111 个）] -> 不做冲突数预估承诺；按域分批解决 + 每批 py_compile，进度按「域完成」而非「文件数」汇报。
- [Python 3.12 / 依赖升级破坏 fork 专属依赖（alibabacloud_dingtalk、pypinyin 等）] -> `pyproject.toml` 合并沿用 1.13.3 的脚本合并方式保留 fork 依赖；`uv lock` 后跑导入冒烟。
- [取舍：本步不修 service_api 30+ 签名侵入] -> 接受一次性对齐成本，收窄工作明确划给 p4，避免本 change 范围膨胀。

## Migration Plan

1. 前置检查：确认 `p0-restore-billing-hooks` 已归档/完成，`git grep -l '<<<<<<<' -- api/ web/` 为空，工作区干净，tag `fork-pre-merge-1.15.0` 存在。
2. 从 `merge/upstream-1.13.3` 切出 `merge/upstream-1.14.2` 分支。
3. `git merge 1.14.2 --no-commit`；按 tasks.md 分域顺序解决冲突。
4. 依赖与构建对齐：`pyproject.toml`（保留 fork 依赖）→ `uv lock` → `api/Dockerfile` / `requirements.docker.txt`。
5. DB：`flask db upgrade`（上游 3 个新迁移）→ `flask extend_db upgrade`（fork 链，应为 no-op 通过）。
6. 验证门槛（全过才允许 merge commit 定稿）：py_compile 全量 → app_factory 导入 → api 单测 → web `pnpm lint` + `pnpm type-check:tsgo` + `pnpm build` → 6 条计费链路 + SSO + 应用中心 + 系统管理页回归。
7. 提交 merge commit；随后单独提交 model.py extend 模型外迁；更新 `docs/dify-plus/与上游差异总表.md` 挂点新坐标。
8. **回滚策略**：merge commit 定稿前可随时 `git merge --abort` 归零；定稿后发现严重问题则 `git reset --hard fork-pre-merge-1.15.0` 所在基线（分支未推送共享前），或保留分支不合入主线。DB 升级在专用环境先演练，生产执行前备份。

## Open Questions

- 挂点 1 的最终宿主：`_handle_node_succeeded` 在 1.14.2 是否仍存在于 `layers/persistence.py`——由合并前侦察任务给出确定答案（本设计仅给出预判）。
- 上游 1.14.2 阶段 `dify_graph`→`graphon` 重命名推进到什么程度、fork 副本文件是否已受影响——侦察任务确认。
- api 单测在 1.14.2 的基线通过率（上游自身是否有已知失败）——合并后首次跑测时以「不劣于上游 tag 本身」为准。
