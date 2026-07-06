# Proposal: p3-merge-upstream-1-15-0

## Why

本 change 是"两步合并"策略的第二步（Step B），对应总体线路图（`docs/dify-plus/实现线路图-upstream-1.15.0升级与admin废弃.md`）**Phase 2 的 4.2 节**：在 P2（合并 1.14.2）完成后，将 fork 从 `1.14.2` 合并到 `1.15.0`（约 657 个提交，git 已核实）。

这一步集中处理的是**结构性变化**而非常规冲突：上游把 web 转为 pnpm monorepo（根级 `pnpm-workspace.yaml` + `catalog:` 依赖 + `packages/dify-ui` 等 7 个子包，`web/pnpm-lock.yaml` 删除改根级锁文件）、删除了 `web/app/components/header/index.tsx` 并重构为 `web/app/components/main-nav/`（fork 的 4 个导航挂载点宿主消失，需要**重做而非解冲突**）、移除了 `@headlessui/react`（overlay 迁入 dify-ui）、把 `api/core/rag/datasource/vdb/` 的 66 个 provider 文件拆到 `api/providers/vdb/*` 并引入 uv workspace 多包构建（`pyproject.toml` 已确认 `members = ["providers/vdb/*", "providers/trace/*"]`，Python 收窄 `~=3.12.0`）。不合并 1.15.0，fork 将持续偏离上游最新稳定版，后续所有安全修复（含 CVE-2026-41948）与功能演进都无法跟进。

## What Changes

- **合并 upstream `1.15.0`**：`git merge 1.15.0`，机械冲突批量解决，高危冲突文件（线路图 4.3 节清单）逐个人工核对。
- **web monorepo 化适配**（最大单项，**BREAKING**（构建方式变化）：
  - 根级 `pnpm-workspace.yaml` + catalog 依赖落地；fork 在 `web/package.json` 增补的二开依赖（`dingtalk-jsapi`、`papaparse`、`jschardet`、`serwist`、`esbuild-wasm`）以 catalog 兼容方式合入；
  - 锁文件以上游根级 `pnpm-lock.yaml` 为准重装，删除 `web/pnpm-lock.yaml`；
  - `docker/docker-compose.dify-plus.yaml`、web Dockerfile、CI（`.gitlab-ci.yml`）适配 workspace 构建。
- **main-nav 迁移（重做，非解冲突）**：在 `web/app/components/main-nav/` 新体系中重新实现 4 个 fork 挂载点——`account-money-extend`（额度徽章）、`system-manage-nav-extend`（系统管理入口）、`nav-extend`、`draw-nav-extend`；account-dropdown 被上游大幅重写需重挂。顺势修复额度徽章硬编码汇率 6.97 → 改读后端配置（`RMB_TO_USD_RATE=7.26`）。
- **headlessui 清退**：扫描仍 import `@headlessui/react` 的二开组件（`invite-modal`、`system-manage-extend` 页面、`auto-select-extend`、`react-multi-email-extend` 等），替换为 dify-ui overlay 原语。
- **api providers 拆包适配**：fork 引用旧 `api/core/rag/datasource/vdb/` 具体 provider 路径的 import 修正为 `api/providers/vdb/*`；`api/Dockerfile`（fork 已自定义阿里云镜像 + `requirements.docker.txt`）对齐 uv workspace 多包构建与 `dify-agent` editable 包；Python 收窄 `~=3.12.0`。
- **升级动作序列**：`flask db upgrade`（上游 24 个新迁移，git 已核实）→ `flask extend_db upgrade` → **`flask backfill-plugin-auto-upgrade`**（必须执行，否则租户插件自动升级设置失效）；环境变量按 19 增 2 删 1 改对齐（含 `SSRF_PROXY_ALLOW_PRIVATE_DOMAINS/IPS`、`UV_CACHE_DIR` 改名）。
- **SSRF 默认拒绝加白**（**BREAKING**（默认行为变化，最易漏的上线炸弹）：企业内网域名/IP 必须配置 `SSRF_PROXY_ALLOW_PRIVATE_DOMAINS` / `SSRF_PROXY_ALLOW_PRIVATE_IPS` 允许清单并回归 http 节点/工具调用，否则内网请求批量 403。
- **决策 D4 落地——探索页分类迁移**：用上游 `recommended_apps.categories`（JSON 列，1.14.2 已含该迁移）替换 fork 的 `recommended_category_extend` / `recommended_apps_category_join_extend` 两表方案：一次性数据迁移脚本（旧表数据 → categories 列）、删除 `database_retrieval.py` 侵入、`migrations_extend` 出 drop 迁移删两张 fork 表、前端 explore 相关组件改读上游数据源。
- **计费挂点再定位与回归**：沿用 P2 的挂点清单，在 1.15.0 代码上重新核对全部计费挂点位置；web 侧 fork 侵入文件重对位——`text-generation/index.tsx`（fork +719 行批量逻辑，若 P1/D2 决策删除批量功能则此处大幅简化，联动关系写入 design）、`contract/router.ts`、`context/app-context*`、i18n-config extend namespace、`i18n/uk-UA` 策略统一。
- **收尾**：全量回归通过后打 tag。

不属于本 change 范围：接入上游 quota v3（D1 已决策保持自建体系）、service_api 侵入面收窄（Phase 3）、admin 废弃（Phase 4）、system-manage-extend contract 化（Phase 5）。

## Capabilities

### New Capabilities

（`openspec/specs/` 目前为空，以下均为新增 capability）

- `web-monorepo-build`: web 构建体系 monorepo 化——pnpm workspace/catalog 依赖治理、根级锁文件、fork 二开依赖合入、Docker/CI 构建适配。
- `main-nav-extend-mounts`: main-nav 新导航体系下的 4 个二开挂载点（额度徽章、系统管理入口、nav-extend、draw-nav-extend）与 account-dropdown 重挂，含汇率配置化。
- `headlessui-free-extend-ui`: 二开 UI 组件去 headlessui 化，统一使用 dify-ui overlay 原语。
- `api-providers-workspace`: api 侧 uv workspace 多包构建适配——vdb/trace 拆包 import 修正、Dockerfile 对齐、Python 3.12 收窄。
- `upstream-upgrade-runbook`: 1.15.0 升级动作序列——三段迁移/回填命令链、环境变量对齐、SSRF 私有网络白名单。
- `explore-native-categories`: 探索页分类迁移到上游原生 `recommended_apps.categories`，退役 fork 两张分类扩展表。
- `billing-hook-relocation`: 计费挂点在 1.15.0 代码结构上的再定位与 6 条链路回归。
- `web-extend-realignment`: web 侧 fork 侵入文件在上游重写后的重对位（text-generation、contract、app-context、i18n）。

### Modified Capabilities

（无——`openspec/specs/` 中尚无已归档 spec）

## Architecture Impact

- **构建体系是本次影响面最大的架构变化**：web 从单包变 pnpm monorepo（新增 `packages/contracts`、`packages/dify-ui`、`packages/iconify-collections` 等），api 变 uv workspace 多包（`providers/vdb/*`、`providers/trace/*`、`dify-agent`）。fork 的 Docker/CI 构建脚本全部需要适配；风险缓解按线路图第 9 节——Step B 在单独分支验证构建产物后再合入。
- **导航挂载点从"侵入上游 header 文件"变为"侵入 main-nav 体系"**：这是既有侵入面的重新落位，不新增架构元素；重做时保留「二开部分 Begin/End」注释标记，坐标登记回 `docs/dify-plus/与上游差异总表.md`（挂点注册表，Phase 3 正式化）。
- **数据模型**：上游 27 个新迁移（1.13.3→1.15.0 累计；本步 24 个）与 fork `migrations_extend` 双 Alembic 链**无命名冲突**，继续并存。唯一功能重叠是探索页分类——按 D4 决策收敛到上游 `categories` JSON 列，fork 净删除两张扩展表与 `database_retrieval.py` 侵入（减少长期侵入面，符合"优先复用上游能力"原则）。
- **依赖**：headlessui 清退后二开组件复用 dify-ui overlay 原语（不引入替代依赖）；fork 增补的 5 个 web 依赖经 catalog 治理，避免版本漂移。
- **配置/部署**：`.env` 模板 19 增 2 删 1 改；SSRF 从默认放行变默认拒绝，属于部署层安全默认值变化，必须进入升级 runbook 强制项。
- **计费体系不变**（D1 决策）：仅挂点坐标随上游代码结构位移，语义与表结构均不动。

## Impact

- **web 代码**：根级 `pnpm-workspace.yaml` / `pnpm-lock.yaml` / `packages/*`（来自上游）；`web/package.json`（catalog 化）；`web/app/components/main-nav/` 下新增 extend 挂载组件；删除 fork 在旧 `header/` 下的挂载实现；headlessui 清退涉及的二开组件；explore 相关组件（分类数据源切换）；`text-generation/index.tsx`、`contract/router.ts`、`context/app-context*`、`i18n-config/*`。
- **api 代码**：合并带来的全量上游变更；fork import 路径修正（vdb）；`api/Dockerfile`、`requirements.docker.txt`、`api/uv.lock`；`migrations_extend` 新增分类数据迁移 + drop 迁移；删除 `database_retrieval.py` 侵入。
- **部署/CI**：`docker/docker-compose.dify-plus.yaml`、web/api Dockerfile、`.gitlab-ci.yml`、`.env` 模板与生产环境变量。
- **数据库**：`flask db upgrade`（上游 24 个迁移）+ `flask extend_db upgrade`（分类数据迁移与 drop）+ `flask backfill-plugin-auto-upgrade`（数据回填）；需要停机窗口（开放问题 4）。
- **运行时行为**：SSRF 默认拒绝私有网络（需加白）；插件自动升级设置依赖 backfill；额度徽章汇率从 6.97 改为后端配置值 7.26（展示金额变化）。
- **下游依赖**：前置依赖 P2（`p2-merge-upstream-1-14-2`）完成；本 change 完成即达成里程碑 M3，解锁 Phase 3（计费加固）与 Phase 4（admin 废弃）并行推进。
