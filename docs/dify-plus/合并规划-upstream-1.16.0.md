# 合并规划：upstream 1.16.0

> 状态：执行中。本文是本次合并的总控计划，供各执行阶段（sub-agent）作为共享输入。
> 制定时间：2026-07-20。

## 0. 基线事实（已勘察确认）

| 项 | 值 |
| --- | --- |
| 当前分支 | `1.15.0`（与 `origin/1.15.0` 同步，工作区干净） |
| 当前基线 tag | `fork-merged-1.15.0` |
| merge-base(HEAD, upstream 1.16.0) | `3aa26fb637`（= upstream tag `1.15.0`），合并路径干净 |
| 目标上游版本 | tag `1.16.0`（`5c6372d2f7`，正式版；不取 `1.16.0-rc1`） |
| 上游变更规模 | 455 个提交，约 9041 个文件变更 |
| 工作分支 | `merge/upstream-1.16.0`（从 `1.15.0` 分支切出） |
| 安全 tag | `fork-pre-merge-1.16.0`（合并前打在 `1.15.0` 分支头） |

**⚠️ refname 歧义**：`1.15.0` 同时是本地分支名和 tag 名。所有 git 命令引用上游版本时
必须写全 `refs/tags/1.15.0`、`refs/tags/1.16.0`，禁止裸写版本号。

## 1. 目标与成功标准

目标：将 upstream `1.16.0` 合并进 fork，保留全部核心二开功能，冲突处理有据可查，
完成可自动化的自测验证，剩余人工回归项形成清单。

成功标准（全部满足才算完成）：

1. `merge/upstream-1.16.0` 分支上存在合并提交，无未解决冲突，无 `<<<<<<<` 残留。
2. 「与上游差异总表」第 7 节登记的 **10 个计费/额度挂点** 逐项复核通过，坐标登记更新到 1.16.0。
3. 后端：`api/migrations_extend/` revision 链完整；`uv run --project api` 下 lint/类型检查通过；
   extend 相关单测与受冲突波及模块的上游单测通过。
4. 前端：`pnpm lint` + `pnpm type-check` + `pnpm build` 通过（在 `web/` 下）。
5. 容器级验证：middleware 起库后 `flask db upgrade` + `flask extend_db upgrade` 成功，
   api 可启动，登录配置 bootstrap、系统管理路由、额度接口冒烟通过（尽力自动化，做不到的记入人工清单）。
6. 文档同步：README/AGENTS/差异总表（含挂点坐标）/升级检查清单更新到 1.16.0 基线。
7. 产出《1.16.0 人工回归清单》：列出无法自动验证、需人工在 UI 上回归的项。

## 2. 冲突处理原则（执行阶段必须遵守）

1. **上游核心代码 upstream 优先**：`api/core/**`、`api/controllers/**`（非 extend 文件）、
   `web/app/components/**`（非 extend 文件）冲突时先取上游版本，再把 fork 挂点逻辑重新挂回。
2. **fork 专属文件 ours 优先**：文件名含 `extend` 的文件、`api/migrations_extend/`、
   `docker/docker-compose.dify-plus.yaml`、`.gitlab-ci.yml`、`scripts/*.py`、`docs/dify-plus/`、
   fork 版 `README.md`（上游 README 内容归 `README_DIFY.md`）。但要检查上游 API 变化是否
   导致 extend 文件的 import/调用失效，失效必须适配。
3. **挂点文件精细合并**：差异总表第 7 节 10 个挂点所在的宿主文件（wraps.py、apikey.py、
   app_generator.py、generate_task_pipeline.py、persistence.py、token_buffer_memory.py、
   oauth.py、ext_celery.py、feature.py 等）逐个人工级合并：取上游新结构 + 重挂 fork 逻辑。
   历史教训：1.13.3 合并时挂点 9（`add_messages_context`/`control_registers` 链路）曾静默丢失。
4. **配置/部署文件双向合并**：`docker/.env.example`、`api/.env.example`、`web/.env.example`、
   `docker/docker-compose.yaml` 取上游新增项，同时保留 fork 专有变量
   （SSRF 白名单、COOKIE_DOMAIN、FILES_URL、socket.io 相关、ENABLE_EXTEND_QUOTA_RESET_TASKS 等）。
5. **删除类冲突（modify/delete）**：上游删除而 fork 修改过的文件，先弄清上游删除原因
   （重构去向），把 fork 逻辑迁到新去向，而不是简单恢复旧文件。
6. **锁文件/生成物**：`uv.lock`、`pnpm-lock.yaml` 直接取上游，合并后重新安装验证；
   i18n 生成文件冲突取上游后重跑生成脚本，再补 fork 新增词条。
7. 每类冲突的处理决策必须记录到 `docs/dify-plus/合并记录-upstream-1.16.0.md`（执行阶段建立）。

## 3. 执行阶段划分

所有阶段按序执行，前一阶段产物是后一阶段输入。执行者：Fable 5 sub-agent。

### Phase 0：准备（分支与安全点）——主控直接执行

- 在 `1.15.0` 分支头打 tag `fork-pre-merge-1.16.0`。
- 切出工作分支 `merge/upstream-1.16.0`。

### Phase 1：上游变更分析

产出 `docs/dify-plus/上游变更分析-1.16.0.md`，内容至少包括：

- `git diff refs/tags/1.15.0 refs/tags/1.16.0 --stat` 按目录聚合的变更热区；
- 10 个挂点宿主文件在上游 1.15.0→1.16.0 的具体变化（逐文件 diff 摘要）；
- 上游新增迁移列表（`api/migrations/versions/` 新文件），及与 `migrations_extend` 的表/字段冲突排查；
- `docker/.env.example`、compose 文件的新增/改名/删除变量清单；
- 上游对登录/鉴权、explore/应用列表、apikey、service_api wraps、celery 配置的结构性重构识别；
- 预判冲突文件清单（`git merge-tree` 试算），按「fork 专属 / 挂点宿主 / 一般冲突」分类。

### Phase 2：执行合并与冲突处理

- `git merge --no-ff refs/tags/1.16.0`，按第 2 节原则解决全部冲突；
- 建立并填写 `docs/dify-plus/合并记录-upstream-1.16.0.md`（冲突文件清单、每个文件的决策与理由）；
- 完成合并提交；确认无冲突标记残留、fork extend 文件全部存活（对照差异总表 4.3/4.4 的代表文件清单）。

### Phase 3：挂点复核与合并后适配

- 逐项复核 10 个挂点（差异总表第 7 节），失效的重挂；
- 复核升级检查清单第 4/5/6 节列出的全部 extend 文件（import、签名、装饰器共存关系）；
- `migrations_extend` 链检查：down_revision 连续、与上游新迁移无表冲突；
- 后端可导入性验证：`uv run --project api python -c "import app"` 级别的启动冒烟；
- 更新差异总表第 7 节挂点坐标登记（登记 1.16.0 坐标与变化点）。

### Phase 4：后端自动化验证

- 依赖安装/同步（uv sync）；
- lint + 类型检查（按 `api/AGENTS.md` 规定的命令）；
- 运行 extend 相关单测 + 受冲突波及模块的上游单测；
- 记录结果（通过/失败/跳过原因）到合并记录文档。

### Phase 5：前端自动化验证

- `pnpm install`；
- `pnpm lint`（优先 `lint:fix`）+ `pnpm type-check` + `pnpm build`；
- extend 组件相关的现有 Vitest 单测；
- 记录结果到合并记录文档。

### Phase 6：容器级迁移与冒烟验证

- 用 `docker/docker-compose.middleware.yaml` 起中间件（或复用本地已有库，注意隔离，禁止碰生产数据）；
- 顺序执行三段命令链：`flask db upgrade` → `flask extend_db upgrade` → 
  `flask backfill-plugin-auto-upgrade`（若 1.16.0 仍需要）；
- 起 api 进程，冒烟：`/console/api/system-features`、login_config_bootstrap 链路、
  system-manage-extend 路由可达性、额度接口；
- 参照 `docs/dify-plus/1.15.0升级本地容器验证记录.md` 的方法论，产出 1.16.0 版验证记录。

### Phase 7：文档同步与收尾

- 更新：README.md、AGENTS.md、与上游差异总表（基线改为 1.16.0）、上游升级与回归检查清单；
- 产出《1.16.0 人工回归清单》（UI 手工回归项：登录三方、应用中心、额度扣减、系统管理三页等，
  参照升级检查清单第 9 节）；
- 打 tag `fork-merged-1.16.0`（仅本地，不推送，由用户决定推送时机）。

## 4. 回滚路径

- 代码：`git checkout 1.15.0`（分支），或 reset 到 tag `fork-pre-merge-1.16.0`；工作分支可直接删除。
- 本地验证库：一次性容器/卷，销毁即回滚；全程不触碰生产数据库。

## 5. 已知风险登记

1. 上游 1.15.0→1.16.0 变更 9000+ 文件，i18n/锁文件/生成物占比大，需先分析再合并，避免误判冲突规模。
2. 挂点 9（消息上下文链路）有历史丢失前科，Phase 3 必须逐行确认。
3. 上游若继续推进 Pydantic 序列化/UUID 参数化改造，挂点 5/6（wraps.py、apikey.py）大概率再冲突。
4. `backfill-plugin-auto-upgrade` 等一次性命令在 1.16.0 的存续状态需要在 Phase 1 确认。
5. 前端上游若调整路由结构（app router 布局），system-manage-extend 与 apps-center-extend 挂载点需重新对位。
