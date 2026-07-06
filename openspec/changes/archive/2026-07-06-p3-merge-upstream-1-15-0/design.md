# Design: p3-merge-upstream-1-15-0

## Context

- 本 change 是两步合并的第二步（线路图 4.2 节 Step B）。**前置依赖**：`p2-merge-upstream-1-14-2` 完成——即 fork 已站在 1.14.2 基线上，计费挂点清单（P0 建立、P2 更新过一轮坐标）可用，6 条计费回归链路在 1.14.2 上全绿。
- `1.14.2 → 1.15.0` 共 657 个提交（git 已核实）。与 Step A 以"代码内重构"为主不同，Step B 的主要冲击是**仓库结构与构建体系**：
  - **web monorepo 化**：根级 `pnpm-workspace.yaml` + `pnpm-lock.yaml`（`web/pnpm-lock.yaml` 删除），新增 `packages/{contracts,dify-ui,iconify-collections,dev-proxy,jotai-tanstack-form,tsconfig,...}` 7 个子包，依赖版本经 `catalog:` 集中治理；
  - **main-nav 重构**：`web/app/components/header/index.tsx` 被删除（git 已核实），导航实现为 `web/app/components/main-nav/{index.tsx,layout.tsx,routes.ts,components/,...}`；fork 挂在旧 header 上的 4 个挂载点宿主消失；
  - **headlessui 移除**：1.15.0 的 `web/package.json` 已无 `@headlessui/react`（overlay 原语迁入 dify-ui）；fork 的二开组件若仍 import 会编译失败；
  - **api uv workspace**：`api/pyproject.toml` 声明 `[tool.uv.workspace] members = ["providers/vdb/*", "providers/trace/*"]`；`api/core/rag/datasource/vdb/` 只保留基类/工厂/注册表（`vector_base.py`、`vector_factory.py`、`vector_backend_registry.py` 等），66 个具体 provider 迁至 `api/providers/vdb/*`；新增 `dify-agent` editable 包；`requires-python = "~=3.12.0"`；
  - **数据库**：本步新增 24 个上游迁移（git 已核实，`--diff-filter=A`）；`recommended_apps.categories` JSON 列的迁移（`a4f2d8c9b731`）**在 1.14.2 已存在**，故 D4 数据迁移在本步执行时列已就绪；
  - **升级动作**：`flask backfill-plugin-auto-upgrade` 必须在 `flask db upgrade` 后执行；`.env` 按 19 增 2 删 1 改对齐；SSRF 代理默认拒绝私有域名/IP。
- fork 侧约束：`api/Dockerfile` 已自定义（阿里云镜像 + `requirements.docker.txt`）；`docker/docker-compose.dify-plus.yaml` 是集成部署入口；CI 用 `.gitlab-ci.yml`；fork 在 `web/package.json` 增补了 `dingtalk-jsapi`、`papaparse`、`jschardet`、`serwist`、`esbuild-wasm` 五个依赖。
- 架构决策输入（Phase 1 已拍板）：D1 保持自建额度体系不接 quota v3；D4 探索页分类迁移到上游原生 `categories` 列。D2（批量工作流去留）的结论影响 `text-generation/index.tsx` 的处理深度（见 Decisions）。

## Goals / Non-Goals

**Goals:**

- 完成 `git merge 1.15.0`，fork 站上 1.15.0 基线，达成里程碑 M3。
- monorepo 下 `pnpm install / lint / type-check:tsgo / build` 全通过；api 构建（本地 + Docker）与三段迁移链（`db upgrade` → `extend_db upgrade` → `backfill-plugin-auto-upgrade`）通过。
- 4 个 main-nav 挂载点 + account-dropdown 在新导航体系中恢复，owner/普通用户双角色验证通过；额度徽章汇率改读后端配置。
- 二开组件零 `@headlessui/react` 残留。
- 探索页分类切换到上游 `recommended_apps.categories`，fork 两张分类表退役（数据已迁移）。
- SSRF 白名单配置落地并验证 http 节点访问内网成功；`.env` 对齐。
- 6 条计费回归链路 + SSO + 应用中心 + 系统管理页全绿；打 tag 留档。

**Non-Goals:**

- 不接入上游 quota v3（D1）；不收窄 `service_api/wraps.py` 侵入面（Phase 3）；不做扣费幂等/原子化改造（Phase 3）。
- 不废弃 admin 服务、不删 GVA 表（Phase 4）；不做 system-manage-extend 的 contract 化与 UI 规范化（Phase 5）。
- 不重写批量工作流（D2 若决定迁移属 Phase 4 任务；本步只做 text-generation 的合并对位或按删除决策简化）。
- 不追求二开组件对 dify-ui 的全面风格重构——headlessui 清退只做"等价替换"，UI 规范化留给 Phase 5。

## Architecture Assessment

### Existing Design Reuse

- **挂点清单与回归基线**（P0 产出、P2 更新）：本步继续作为计费挂点再定位的对照物与验收依据，不另起炉灶。
- **dify-ui overlay 原语**（上游 `packages/dify-ui`）：headlessui 清退的替换目标，复用上游能力而非引入第三方替代（如 radix-ui）。
- **上游 `recommended_apps.categories`**：D4 直接复用上游原生数据模型与查询路径，fork 净删除 `recommended_category_extend` / `recommended_apps_category_join_extend` 两表和 `database_retrieval.py` 侵入。
- **`migrations_extend` 独立 Alembic 链**：分类数据迁移脚本与 drop 迁移继续走 fork 既有迁移链，不混入上游链。
- **`api/configs/extend/__init__.py` 配置挂载模式**：汇率 `RMB_TO_USD_RATE` 已是后端配置，本步只是让前端改读接口下发值，不新增配置机制。
- **「二开部分 Begin/End」注释标记**：main-nav 重做与所有挂点搬迁沿用该可发现性约定。

### Boundaries and Ownership

- **上游 vs fork 的文件边界**：合并原则不变——上游文件以上游为准 + 最小侵入块；fork 能力尽量收敛在 `*extend*` 独立文件。main-nav 挂载点新实现放独立 extend 组件文件（如 `web/app/components/main-nav/components/*-extend.tsx`），main-nav 宿主文件内只留最小挂载行。
- **依赖治理边界**：monorepo 后依赖版本归 catalog 所有。fork 增补依赖若 catalog 已收录同名条目则用 `catalog:`；否则在 `web/package.json` 直接固定版本（不擅自往根 catalog 加条目，减少与上游 `pnpm-workspace.yaml` 的未来冲突面）。
- **锁文件所有权**：根级 `pnpm-lock.yaml` 以上游为基础、由 `pnpm install` 重生成合入 fork 依赖；`web/pnpm-lock.yaml` 删除，任何工具/CI 不得再引用。
- **构建产物边界**：web Docker 构建上下文从 `web/` 扩大到仓库根（workspace 安装需要根 `pnpm-workspace.yaml` + `packages/*`）；`docker/docker-compose.dify-plus.yaml` 的 build context 与 CI 缓存路径随之调整。api Docker 构建需带上 `api/providers/*` 与 `dify-agent`。
- **分类数据所有权**：迁移完成后 explore 分类的唯一数据源是 `recommended_apps.categories`；fork 两张表 drop 后不再有第二写入方。迁移脚本归 `migrations_extend`（fork 拥有其生命周期）。
- **失败与回滚职责**：merge 阶段任何时刻可 `git merge --abort`；合并分支验证不过不合入主线；数据库迁移的回滚依赖升级前快照（见 Migration Plan），分类 drop 迁移安排在数据迁移验证之后单独一个版本，可独立暂缓。

### Options and Rationale

见 Decisions。核心取舍：main-nav 挂载点选择"在新体系重做"而非"把旧 header 文件保活"；分类选择"收敛上游原生列"而非"双轨并存"。

### Quality Attributes

- **安全**：SSRF 默认拒绝是上游的安全增强，fork 通过显式白名单（而非关闭防护）满足内网访问需求；合并同时带入 CVE-2026-41948（路径穿越）修复。
- **可部署性**：monorepo/uv workspace 改变构建方式，Docker 与 CI 适配是硬性验收项；Step B 在独立分支完成全部构建验证后才合入（线路图风险表缓解措施）。
- **可维护性**：D4 落地净减一个长期侵入面（`database_retrieval.py` + 两张表）；挂点坐标更新回 `docs/dify-plus/与上游差异总表.md`。
- **可测试性**：验收以可重复命令链（pnpm 三件套、api 单测、迁移链）+ 既有 6 条计费回归清单表达；main-nav 迁移前截图留档对比。
- **性能**：无预期回退；catalog 与 uv workspace 只影响构建期。

### Complexity and Exceptions

- 不新增任何 fork 自有的抽象、服务、存储或协议。全部结构性复杂度（monorepo、workspace、main-nav）来自上游，fork 的工作是**适配**。
- 唯一新增的一次性资产是分类数据迁移脚本，随 `migrations_extend` 版本链管理，跑完即成历史版本，无长期维护成本。

## Decisions

### DD1：main-nav 挂载点采用"新体系重做"，不保活旧 header

- **决策**：删除 fork 在旧 `web/app/components/header/` 下的挂载实现，在 `web/app/components/main-nav/` 体系内以独立 extend 组件重新实现 4 个挂载点（`account-money-extend` 额度徽章、`system-manage-nav-extend` 系统管理入口、`nav-extend`、`draw-nav-extend`），account-dropdown 相关的 fork 项在上游重写后的新 account-dropdown 中重挂。
- **替代方案**：保留旧 `header/index.tsx` 不让上游删除（merge 时 keep ours）。否——会形成与上游导航并存的死分支，路由/布局早已不再渲染它，且每次升级都要人肉维持一个上游已放弃的文件。
- **顺势修复**：额度徽章展示的美元换算汇率由前端硬编码 `6.97` 改为读取后端配置下发（后端 `RMB_TO_USD_RATE=7.26`，经现有系统特性/配置接口透出），消除前后端口径不一致。

### DD2：fork 增补依赖的 catalog 策略——"catalog 优先、包内固定兜底"

- **决策**：对 `dingtalk-jsapi`、`papaparse`、`jschardet`、`serwist`、`esbuild-wasm`（含相应 `@types/*`）逐个检查上游 catalog：已有条目→改写为 `catalog:`；没有→留在 `web/package.json` 用显式版本。不向根 `pnpm-workspace.yaml` 的 catalog 添加 fork 专有条目。
- **理由**：根 `pnpm-workspace.yaml` 是上游高频演进文件，fork 往里加条目会制造持续冲突；包内显式版本对 pnpm 完全合法，隔离性好。
- **替代方案**：全部进 catalog（冲突面大）；或全部包内固定（放弃与上游同名依赖的版本对齐）。折中方案兼顾两者。

### DD3：探索页分类一次性迁移 + 分步 drop（D4 落地方式）

- **决策**：分三个动作、两个迁移版本落地——
  1. `migrations_extend` 新增数据迁移版本：把 `recommended_category_extend` / `recommended_apps_category_join_extend` 中的分类关系写入 `recommended_apps.categories`（JSON 数组）；幂等（重复执行不产生重复分类）；
  2. 代码切换：删除 `database_retrieval.py` 侵入，api 读取走上游原生路径；web explore 组件（sidebar、app-card 等 fork 改动处）改读上游 categories 数据源；
  3. `migrations_extend` 再出一个独立 drop 版本删除两张 fork 表——**与数据迁移分离**，在探索页回归通过后执行，保留"暂缓 drop"的余地。
- **替代方案**：双轨并存（fork 表继续为主）——保留 `database_retrieval.py` 侵入与两张表，每次升级继续付冲突税，且与上游 explore 前端渐行渐远。否决。
- **依赖事实**：`categories` 列迁移（`a4f2d8c9b731`）在 1.14.2 已进库，本步数据迁移无前置 schema 风险。

### DD4：headlessui 清退 = 等价替换到 dify-ui，不引入新依赖

- **决策**：以"合并后全仓 `rg '@headlessui/react' web/` 归零"为验收，逐个把二开组件（已知候选：`invite-modal`、`system-manage-extend` 各页、`auto-select-extend`、`react-multi-email-extend`；以扫描结果为准）的 Dialog/Transition/Popover/Listbox 等用法替换为 dify-ui overlay 原语或上游同用途 base 组件。只做行为等价替换，不做视觉重构（Phase 5）。
- **替代方案**：fork 自己把 `@headlessui/react` 加回依赖——短期最省事，但与上游组件体系分叉，且上游 base 组件已不再兼容它的样式上下文。否决。

### DD5：api Docker 构建对齐 uv workspace，保留阿里云镜像定制

- **决策**：以上游 1.15.0 的 `api/Dockerfile` 为骨架重做 fork 的 Dockerfile——采纳其 uv workspace 多包拷贝/安装顺序（`api/providers/*`、`dify-agent`）与 Python `~=3.12.0` 基镜像，再叠加 fork 的定制层（阿里云 apt/pip 镜像源、`requirements.docker.txt` 额外依赖）。fork 定制以"补丁块"形式集中，便于下次升级再叠加。
- **替代方案**：在 fork 旧 Dockerfile 上打补丁——workspace 结构变化太大，逆向补丁比正向重做更易漏。

### DD6：text-generation 处理深度与 D2 决策联动

- **决策**：`web/app/components/share/text-generation/index.tsx`（fork +719 行批量逻辑）按 D2 结论分叉处理：
  - **D2 = 删除批量功能**：本步直接以上游 1.15.0 版本为准，仅保留非批量类 fork 小改（如有），+719 行批量逻辑不再搬迁——合并难度大幅下降；
  - **D2 = 保留/迁移**：本步把批量逻辑对位到上游重写后的组件结构上（照常解冲突搬迁），Flask+Celery 后端迁移仍属 Phase 4。
  - tasks.md 中该任务标注此分叉条件；执行前必须拿到 D2 书面结论。

### DD7：升级动作序列固化为 runbook，backfill 与 SSRF 白名单为强制项

- **决策**：升级执行顺序固化为：备份 → `flask db upgrade`（上游 24 个新迁移）→ `flask extend_db upgrade`（fork 链，含分类数据迁移）→ `flask backfill-plugin-auto-upgrade`（回填租户插件自动升级设置，**跳过即功能静默失效**）→ `.env` 对齐（19 增 2 删 1 改，含 `UV_CACHE_DIR` 改名）→ SSRF 白名单（`SSRF_PROXY_ALLOW_PRIVATE_DOMAINS` / `SSRF_PROXY_ALLOW_PRIVATE_IPS` 配企业内网域名/IP）→ 服务起动与回归。写入 `docs/dify-plus/上游升级与回归检查清单.md`。
- **理由**：backfill 与 SSRF 加白都是"不做不报错、上线才爆"的静默炸弹，必须进 checklist 强制项而非口口相传。

## Risks / Trade-offs

- [monorepo 化导致 fork Docker/CI 构建长期不稳定] -> Step B 全程在独立合并分支进行，`pnpm install/lint/type-check:tsgo/build` + 两个 Docker 镜像构建 + compose 起动全部通过后才合入；CI 首次跑通留存配置快照。
- [SSRF 默认拒绝导致企业内网工具调用批量 403（最易漏）] -> runbook 强制项 + 专项验证任务：配置白名单后用 http 节点/工具实测访问内网目标成功、未加白目标仍被拒。
- [main-nav 重做后交互回退或双角色可见性错误] -> 迁移前对旧 header 截图留档；owner 与普通用户双角色逐项对比验证（额度徽章、系统管理入口可见性、nav/draw 入口）。
- [计费挂点在 1.15.0 的 graph 层演进中再次位移] -> 沿用 P2 挂点清单逐项核对新坐标，合并完成立即跑 6 条计费回归；新坐标登记回差异总表。
- [分类数据迁移错漏导致探索页分类丢失] -> 迁移脚本幂等 + 迁移后 SQL 抽验（分类计数对比）；drop 迁移独立版本、回归通过后才执行；升级前快照可整体回滚。
- [`flask backfill-plugin-auto-upgrade` 漏执行] -> 写入 runbook 强制项；验证任务包含检查租户插件自动升级设置非空。
- [锁文件重装引入依赖漂移] -> 以上游根锁文件为基础增量安装 fork 依赖；`pnpm install` 后跑全量 lint/type-check/build 捕获 API 变化；`api/uv.lock` 同理经 `uv lock` 重生成并全量单测。
- [D2 未拍板阻塞 text-generation 对位] -> DD6 已把两种结论的处理路径都写清；merge 启动前确认 D2 结论（开放问题 1）。

## Migration Plan

1. **准备**：确认 P2 完成且回归全绿；确认 D2 结论；从 P2 完成点切出合并分支 `merge/upstream-1.15.0`；生产库/env 备份快照。
2. **合并与修复**（全部在合并分支）：`git merge 1.15.0 --no-commit` → 机械冲突批量解 → 高危文件人工核对 → 提交 merge commit → 按领域修复（构建体系、main-nav、headlessui、providers、分类迁移、env/SSRF——并发策略见 tasks.md）。
3. **数据库升级演练**：在测试库执行三段命令链（`flask db upgrade` → `flask extend_db upgrade` → `flask backfill-plugin-auto-upgrade`），验证含分类数据迁移结果；drop 迁移在探索页回归通过后执行。
4. **验证**：pnpm 三件套 + 双 Docker 构建 + compose 起动 + api 单测 + 全量回归（计费 6 链路 / SSO / 应用中心 / 系统管理页 / main-nav 双角色 / SSRF 白名单）。
5. **合入与留档**：合并分支进主线；打 tag（如 `fork-merged-1.15.0`）；生产升级按 runbook 执行，需停机窗口（开放问题 2）。
6. **回滚策略**：合入前——`git merge --abort` 或弃分支；合入后未升级生产库——回退到前一 tag；生产库已升级——依赖第 1 步快照整库回滚（`flask db downgrade` 跨 24 个版本不可靠，不作为主回滚手段）；drop 迁移未执行前 fork 分类表数据天然保留。

## Open Questions

1. D2（批量工作流去留）书面结论——决定 `text-generation/index.tsx` 走 DD6 的哪个分支（merge 启动前必须确认）。
2. 生产升级停机窗口时长与时点（线路图开放问题 4）——三段迁移链 + backfill 的耗时需在测试库演练后估算。
3. 企业内网 SSRF 白名单的具体域名/IP 清单——需业务方提供 http 节点/工具当前访问的内网目标全集。
4. fork 增补依赖中 `serwist`/`esbuild-wasm` 在 monorepo 构建下是否有 Next.js 配置联动（`next.config.ts` 的 PWA/wasm 配置）——merge 时扫描确认。
