# Tasks: p3-merge-upstream-1-15-0

> 前置条件：`p2-merge-upstream-1-14-2` 已完成（fork 站在 1.14.2 基线，挂点清单坐标为 1.14.2 版，6 条计费回归链路全绿）；D2（批量工作流去留）已有书面结论（决定任务 5.1 的处理分支）。

## 1. 合并准备与执行（主 agent 顺序）

- [x] 1.1 确认前置条件：P2 回归全绿、D2 结论在案；从 P2 完成点切出合并分支 `merge/upstream-1.15.0`；对旧 header 导航（owner 与普通用户双角色）截图留档，用于 main-nav 迁移后对比（P2 已归档 `archive/2026-07-05-p2-merge-upstream-1-14-2`，回归以代码级验证全绿收口；D2=删除批量功能（提交 4071db17）；分支已从 `ffb0c5303f` 切出；本地无运行环境无法截图，以代码级基线 `nav-mount-baseline.md` 替代留档）
- [x] 1.2 执行 `git merge 1.15.0 --no-commit`；机械冲突批量解决（蓝图注册、`models/__init__.py`、i18n namespace、锁文件直接取上游等）（79 个冲突：锁文件/uk-UA/AU 测试脚本取上游；uk-UA 8 个 DU 文件按"取上游恢复"处理以消除后续冲突税——languages.ts 中 uk-UA 仍启用，删除策略与之矛盾，已统一为跟随上游）
- [x] 1.3 逐个人工核对高危冲突文件：api 侧 `service_api/wraps.py`（validate_token_quota_extend 保留）、`console/apikey.py`（额度字段/软删/put 全保留+上游 dump_response 路线）、`persistence.py`（计费派发在位）、`*/app_generator.py`（account_id extras 重挂）、`console/feature.py`（CVE login_config JWT 门禁保留、SystemFeatureHealthApi 保留）、`feature_service.py`（钉钉/OAuth2 字段并入新模型）、`account_service.py`/`workspace_service.py`（额度初始化与 admin_extend 标记保留）；`generate_task_pipeline/oauth/token_buffer/ext_celery` 本次干净合并，挂点扫描确认全部在位；web 侧 `contract/router.ts`（loginConfig+systemManage 重挂、上游 snippets 合入）、`contract/console/system.ts`（重写为纯 fork contract，类型迁 `features/system-features/extend.ts`）、`app-context*`（admin_extend/tenant_extend 以 UserProfileWithExtend 重对位）、`i18n` 干净合并
- [x] 1.4 提交 merge commit（允许暂时构建不过），确保 `git grep -l '<<<<<<<' -- api/ web/` 为空——作为后续修复阶段的稳定基点（冲突标记扫描为零）

## 2. 构建体系适配（主 agent 顺序，最大单项）

- [ ] 2.1 web monorepo 落地：根级 `pnpm-workspace.yaml` / `packages/*` 以上游为准；删除 `web/pnpm-lock.yaml`；确认无脚本/CI 残留引用旧锁文件路径
- [ ] 2.2 fork 增补依赖按 DD2 策略合入 `web/package.json`（`dingtalk-jsapi`、`papaparse`、`jschardet`、`serwist`、`esbuild-wasm` 及 `@types/*`）：上游 catalog 已有条目改 `catalog:`，其余包内固定版本；不向根 catalog 加 fork 条目；执行 `pnpm install` 重生成根锁文件；扫描 `next.config.ts` 确认 serwist/esbuild-wasm 配置联动（design 开放问题 4）
- [ ] 2.3 api uv workspace 对齐：`api/pyproject.toml` workspace 成员（`providers/vdb/*`、`providers/trace/*`）与 `dify-agent` editable 包生效；`uv lock` 重生成 `api/uv.lock` 并合入 fork 增补依赖；Python 收窄 `~=3.12.0`；`uv sync --project api` 通过
- [ ] 2.4 按 DD5 重做 `api/Dockerfile`：上游 1.15.0 骨架 + fork 补丁块（阿里云镜像源、`requirements.docker.txt`）；本地构建镜像成功
- [ ] 2.5 web Dockerfile 与 `docker/docker-compose.dify-plus.yaml` 适配 workspace 构建（build context 扩大到仓库根、拷贝 `packages/*`）；web 镜像本地构建成功
- [ ] 2.6 `.gitlab-ci.yml` 适配 monorepo 与 uv workspace（安装/构建/缓存路径）；合并分支上 CI 跑通
- [ ] 2.7 构建门禁初验：`pnpm lint`、`pnpm type-check:tsgo`、`pnpm build` 与 `uv run --project api python -c "from app_factory import create_app; create_app()"` 全部通过（允许后续修复任务再触碰，此处确认构建体系本身成立）

## 3. main-nav 挂载点重做（可 sub-agent 并行，见第 8 节）

- [ ] 3.1 在 `web/app/components/main-nav/` 体系重新实现 `account-money-extend` 额度徽章（独立 extend 组件 + 宿主最小挂载行 + Begin/End 标记）；顺势删除硬编码汇率 `6.97`，改读后端配置下发的 `RMB_TO_USD_RATE`（7.26）
- [ ] 3.2 重新实现 `system-manage-nav-extend` 系统管理入口：管理角色可见、普通成员不可见，点击进入 `/system-manage-extend/` 正常
- [ ] 3.3 重新实现 `nav-extend` 与 `draw-nav-extend` 挂载点，行为与迁移前一致
- [ ] 3.4 account-dropdown 二开项在上游重写后的新实现中重挂
- [ ] 3.5 清除 fork 在旧 `header/` 目录下的挂载实现与全部残留 import

## 4. headlessui 清退（可 sub-agent 并行）

- [ ] 4.1 全仓扫描 `rg '@headlessui/react' web/` 产出 fork 文件清单（已知候选：`invite-modal`、`system-manage-extend` 各页、`auto-select-extend`、`react-multi-email-extend`）
- [ ] 4.2 逐文件替换为 dify-ui overlay 原语或上游同用途 base 组件，保持交互行为等价（打开/关闭、焦点、Esc/遮罩、选项选择），不做视觉重构
- [ ] 4.3 验收：`rg '@headlessui/react' web/` 归零；`web/package.json` 与根锁文件无该依赖；`pnpm type-check:tsgo` 通过

## 5. web 侧 fork 侵入重对位（主 agent 顺序，依赖 D2 结论）

- [ ] 5.1 `text-generation/index.tsx` 按 DD6 分叉处理：D2=删除 → 以上游版本为准、批量逻辑不搬迁；D2=保留 → +719 行批量逻辑对位到上游重写后结构并验证批量运行可用
- [ ] 5.2 `contract/router.ts`、`contract/console/system.ts` 的 fork 注册在上游 contract 迁入 `packages/contracts` 后重对位；依赖 fork contract 的页面请求正常
- [ ] 5.3 `context/app-context*` 扩展字段重对位，类型检查通过
- [ ] 5.4 i18n：extend namespace 注册在新 i18n 结构上恢复（`i18n-config/i18next-config.ts`、`resources.ts`）；`i18n/uk-UA` 按 fork 既定策略统一处理

## 6. api providers 拆包与探索页分类迁移（可 sub-agent 并行）

- [ ] 6.1 扫描 fork 代码对 `api/core/rag/datasource/vdb/` 已迁走 provider 的 import 并修正为 `api/providers/vdb/*` 新路径；`create_app()` 冒烟通过、无孤儿 import
- [ ] 6.2 编写 `migrations_extend` 分类数据迁移版本：`recommended_category_extend` / `recommended_apps_category_join_extend` → `recommended_apps.categories` JSON 列；脚本幂等（重复执行不产生重复分类项）
- [ ] 6.3 api 侧切换：删除 `database_retrieval.py` 的 fork 分类侵入，改走上游原生 categories 查询路径
- [ ] 6.4 web 侧切换：explore 相关组件（sidebar、app-card 等 fork 改动处）改读上游 categories 数据源；更新相关测试（`explore/sidebar/__tests__` 等）
- [ ] 6.5 编写 `migrations_extend` 独立 drop 迁移版本（删两张 fork 分类表）——与 6.2 分离，标注"仅在任务 9.4 探索页回归通过后执行"

## 7. 升级动作与环境对齐（可 sub-agent 并行准备，执行主 agent）

- [ ] 7.1 `.env` 模板与 `docker/docker-compose.dify-plus.yaml` 按 19 增 2 删 1 改对齐（含 `SSRF_PROXY_ALLOW_PRIVATE_DOMAINS/IPS` 新增、`UV_CACHE_DIR` 改名）；被删/改名旧变量不再出现
- [ ] 7.2 收集企业内网 SSRF 白名单清单（design 开放问题 3，需业务方提供 http 节点/工具访问的内网目标全集）并配置
- [ ] 7.3 在测试库演练三段命令链：`flask db upgrade`（24 个新迁移）→ `flask extend_db upgrade`（含 6.2 分类数据迁移）→ `flask backfill-plugin-auto-upgrade`；验证双链各自到 head、分类数据 SQL 抽验计数一致、租户插件自动升级设置非空
- [ ] 7.4 升级序列与强制项（backfill、SSRF 加白）写入 `docs/dify-plus/上游升级与回归检查清单.md`

## 8. Parallelization Plan（并发执行策略）

- [ ] 8.1 **顺序阶段（主 agent）**：第 1 节合并冲突解决与第 2 节构建体系适配必须由主 agent 顺序完成——两者触碰全局共享文件（锁文件、Dockerfile、compose、CI、`package.json`/`pyproject.toml`），是后续一切修复的地基，不可并行
- [ ] 8.2 **并行修复阶段（merge commit + 构建体系成立后）**：以下四块相互独立、无共享文件，可各派一个 sub-agent 并行执行——(a) 第 3 节 main-nav 四挂载点重做（范围：`web/app/components/main-nav/**` extend 文件 + 旧 header 清理）；(b) 第 4 节 headlessui 清退（范围：扫描清单内 fork 组件文件）；(c) 第 6 节探索页分类迁移 + vdb import 修正（范围：`migrations_extend`、`database_retrieval.py`、web explore 组件、fork 的 vdb import）；(d) 第 7.1/7.2 env 与 SSRF 对齐准备（范围：env 模板、compose 环境块、白名单清单整理）。每个 sub-agent 的 context inputs：本 tasks.md 对应小节 + design.md 对应 Decision + 相应 spec 文件；产出：编辑完成 + 各自门禁自验（type-check/冒烟）+ 变更摘要；主 agent 汇总后统一跑第 2.7 全量门禁解决交叉影响
- [ ] 8.3 **冲突边界**：第 5 节（text-generation、contract、app-context、i18n）与主 agent 的高危文件核对（1.3）存在同文件交叠，由主 agent 顺序完成，不委派；任何 sub-agent 不得修改锁文件、Dockerfile、CI、`pnpm-workspace.yaml`——发现需要时上报主 agent 决策
- [ ] 8.4 **并行回归阶段**：第 9 节分域回归可并行——计费 6 链路（a）、SSO + 应用中心 + 系统管理页（b）、main-nav 双角色 + UI 对比（c）、SSRF/http 节点（d）四个域各派一个 sub-agent 或测试人员并行执行，产出各域通过/失败清单，主 agent 汇总放行

## 9. Architecture Verification（全量回归与收尾）

- [ ] 9.1 构建门禁终验：`pnpm install / lint / type-check:tsgo / build` 全通过；api/web 双 Docker 镜像构建成功且经 `docker/docker-compose.dify-plus.yaml` 起动正常；CI 全流水线绿；api 单测通过
- [ ] 9.2 计费回归 6 链路全绿：Console 调试运行扣费、Explore 运行扣费、WebApp 登录+扣费、Service API 密钥日/月限额拦截、workflow LLM 节点扣费、月初额度重置（beat 3 任务在位）；挂点新坐标登记回 `docs/dify-plus/与上游差异总表.md`
- [ ] 9.3 main-nav 双角色验证：owner 与普通用户分别对照 1.1 截图逐项核对（额度徽章数值与 7.26 汇率、系统管理入口可见性、nav/draw 入口、account-dropdown 二开项）
- [ ] 9.4 功能回归：SSO 登录（钉钉/OAuth2/Casdoor）、应用中心（探索页分类展示与过滤和迁移前一致）、系统管理页各功能、（若 D2 保留）批量运行
- [ ] 9.5 SSRF 白名单验证：http 节点访问加白内网目标成功、未加白私有目标仍被 403 拒绝
- [ ] 9.6 执行 6.5 的 drop 迁移（前置：9.4 探索页回归通过）；确认应用运行无报错
- [ ] 9.7 合并分支合入主线；打 tag（如 `fork-merged-1.15.0`）；生产升级按 runbook（7.4）在停机窗口执行（备份 → 三段命令链 → env/SSRF → 起动回归）
