# Proposal: 合并上游 1.14.2（两步合并策略 Step A）

## Why

fork 当前基线为 `merge/upstream-1.13.3`，落后上游约 1677 提交（至 1.15.0）。一次性消化风险过高，线路图（`docs/dify-plus/实现线路图-upstream-1.15.0升级与admin废弃.md` 4.1 节）确定**两步合并**策略：本 change 是第一步 1.13.3 → 1.14.2，集中消化上游 api 侧的结构性重构；第二步（`p3-merge-upstream-1-15-0`）再处理 web monorepo 化与 main-nav 重构。

1.13.3 → 1.14.2 共 **1020 提交**（api 2,166 文件 +130,597/-103,591；web 4,589 文件 +164,306/-131,196；新增 3 个上游 DB 迁移），核心冲击：

- service 层大规模「显式传 session」重构 + SQLAlchemy 2.0 `select()` 迁移（FeedbackService、TagService、FileService、ApiKeyAuthService 等）——fork 的 `*_extend` service 若复用旧签名会批量报错；
- console 控制器改为注入 user/tenant——fork 的 extend 控制器需跟进同一模式；
- 上游 quota v3（`BillingService` 的 `quota_reserve/quota_commit/quota_release` + tenant 级 Redis 锁）引入，与 fork 自建额度语义重叠；
- `ef2b5d6107 refactor(api): move llm quota deduction to app graph layer` 在 `api/core/app/workflow/layers/` 新增 `llm_quota.py`，与 fork 节点计费挂点（同目录 `persistence.py`）同区域，合并时挂点必然位移；
- `dify_graph` 演进 / dify_graph→graphon 重命名相关重构；
- 依赖升级与 Python 版本走向 3.12。

**为什么现在做**：前置 change `p0-restore-billing-hooks` 已恢复 4 处断链计费挂点并建立 6 条计费回归链路基线，基线可信；越晚合并，fork 与上游漂移越大，1.15.0 的第二步合并窗口越难打开。

## What Changes

- 在 `merge/upstream-1.13.3` 基线上执行 `git merge 1.14.2 --no-commit`，解决全部冲突并提交，产出新基线分支（如 `merge/upstream-1.14.2`）。
- **计费挂点重定位**（本 change 核心风险）：对照 10 项挂点清单（见 design.md），凡被上游移动/重写的挂点，把 fork hook 移植到新位置，并登记新坐标。
- fork `*_extend` service / controller 适配上游「显式传 session」+ `select()` + user/tenant 注入的新签名。
- **不接入上游 quota v3**（决策 D1 已定）：fork 保持自建额度表体系（`account_money_extend` / `api_token_money_extend` 等），quota v3 相关代码只解冲突、原样接受上游实现，不挂 fork 逻辑。
- Python 版本与依赖升级跟进：`api/Dockerfile`、`api/requirements.docker.txt`、`api/uv.lock` 对齐上游 1.14.2。
- 附带改进：`api/models/model.py` 内嵌的 3 个 extend 模型类外迁到独立 `*_extend` 文件，降低后续合并冲突面。
- 执行上游 3 个新增 DB 迁移（`flask db upgrade`）并验证 fork 独立 Alembic 链（`flask extend_db upgrade`）双链并存。
- 全量回归：6 条计费链路、SSO（钉钉/OAuth2/Casdoor）、应用中心、系统管理页、web 构建。
- **不做**（明确排除，属于后续 change）：web monorepo 化 / main-nav 迁移 / headlessui 清退（p3）、service_api 签名侵入收窄与扣费幂等化（p4）、admin 废弃（p5）。

## Capabilities

### New Capabilities

- `upstream-merge-baseline`: 合并到 upstream 1.14.2 后 fork 基线必须满足的整体要求——构建/导入/迁移全通过、双 Alembic 链并存、quota v3 边界（不接入）、Python/依赖对齐、无冲突标记残留。
- `billing-hook-relocation`: 10 项计费/额度挂点在 1.14.2 新代码结构下的重定位要求——每个挂点合并后行为不变（扣费、归因、限额拦截、定时重置、上下文处理、OAuth 扩展），且新坐标被登记。
- `fork-extension-compatibility`: fork extend 代码（api 侧 `*_extend` service/controller、web 侧 contract/context/i18n 挂载）对上游 1.14.2 重构模式的兼容要求。

### Modified Capabilities

（无——`openspec/specs/` 当前为空，无既有 capability 受影响。）

## Architecture Impact

- **合并策略层**：遵循 fork 一贯的「上游为准 + extend 隔离」模式；冲突解决优先接受上游重构，再把 fork 侵入点按新模式重挂，不逆向改造上游代码。
- **计费体系边界**：fork 自建额度体系与上游 `BillingService`（含新 quota v3）继续保持完全平行、不复用；`api/core/app/workflow/layers/` 成为双方共存的敏感区域。
- **数据模型**：上游 3 个新迁移与 `api/migrations_extend/` 独立链无命名冲突，双链继续并存；无 fork 侧 schema 变更（模型类外迁不改表结构）。
- **配置**：fork 配置继续经 `api/configs/extend/__init__.py` 挂载；上游 1.14.2 新增环境变量按 diff 对齐 `docker/` compose 与 `.env.example`。
- **测试/验证模式**：复用 P0 建立的 6 条计费回归链路作为验收门槛；py_compile 全量 + app_factory 导入冒烟 + web lint/type-check/build。

## Impact

- **api 侧**：预计冲突集中在高危文件清单（`controllers/service_api/wraps.py`、`controllers/console/apikey.py`、`core/app/workflow/layers/persistence.py`、`core/app/apps/*/app_generator.py`、`*/generate_task_pipeline.py`、`models/model.py`、`libs/oauth.py` + `controllers/console/auth/oauth.py`、`controllers/console/feature.py`、`core/memory/token_buffer_memory.py`、`events/__init__.py`、`extensions/ext_celery.py`、`services/account_service.py`）。
- **web 侧**：`contract/router.ts`、`contract/console/system.ts`、`context/app-context*`、`config/index.ts`、`i18n-config` extend namespace 注册（1.14.2 阶段 web 结构未 monorepo 化，冲突量小于 api）。
- **构建/部署**：`api/Dockerfile`、`api/requirements.docker.txt`、`api/uv.lock`、docker compose 版本号。
- **数据库**：上游 3 个新迁移；fork `migrations_extend` 链不变。
- **依赖**：Python 走向 3.12、上游依赖批量升级；fork 专属依赖（alibabacloud_dingtalk、pypinyin 等）需在合并 `pyproject.toml` 时保留。
- **前置依赖**：`p0-restore-billing-hooks` 必须已完成（基线计费链路全绿、无冲突标记残留）。
