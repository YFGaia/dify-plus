# Proposal: p4-billing-quota-hardening

## Why

fork 的计费/额度体系（用户额度、API 密钥额度、周期重置、转发计费）是历次上游合并中最脆弱、最昂贵的差异面。在合并 upstream 1.15.0（p3-merge-upstream-1-15-0）完成后，存在两类必须解决的问题：

1. **升级冲突成本过高**：`api/controllers/service_api/wraps.py` 的 `validate_app_token` 通过 `kwargs["api_token"]` 把密钥对象注入视图函数，导致约 9 个 service_api controller、30+ 方法签名偏离上游原样。上游对 service_api 的任何改动（1.14/1.15 已发生 200-300 行级重写）都会在这些文件上产生冲突，且解冲突时极易遗漏签名参数导致运行时错误。
2. **已知技术债**：`message_was_created` handler 的账号扣费是「读-改-写」非原子操作，并发下少扣；Celery 扣费任务重试 3 次但无幂等键，重试可能重复扣费；前端额度徽章硬编码汇率 6.97 与后端配置 `RMB_TO_USD_RATE=7.26` 不一致；`billing_extend.py` 硬编码初始额度 15；`money_limit`/`is_money_limit` 每请求查库且 `except: return True` 会在 DB 抖动时误拦全部 WebApp 请求；`api_token_message_joins_extend` 表只增不清；三个额度重置定时任务全表 `.all()` 加载。

本 change 对应总体线路图（`docs/dify-plus/实现线路图-upstream-1.15.0升级与admin废弃.md`）的 **Phase 3 计费/额度兼容改造与加固**，前置依赖 p3-merge-upstream-1-15-0 完成。核心目标：**降低下次上游升级的冲突成本**并修复已知技术债，同时不破坏既有行为契约（付款人归因优先级、扣费金额换算、额度记录缺失建档、限额 -1 不限、事前拦截 + 事后异步扣费语义）。

## What Changes

- **收窄 service_api 侵入面（最重要）**：`validate_app_token` 不再通过 `kwargs["api_token"]` 注入视图，改用 contextvar（或 `flask.g`）传递 api_token；9 个 service_api controller（`completion`、`workflow`、`conversation`、`message`、`file`、`audio`、`site`、`app`、`annotation`）的方法签名回归上游原样，`git diff` 相对上游签名归零。
- **扣费幂等与原子化**：`update_account_money_when_messaeg_created_extend.py` 的账号扣费从「读-改-写」改为原子 UPDATE（对齐 `update_account_money_when_workflow_node_execution_created_extend` 已有写法）；Celery 扣费任务增加幂等键（以 `node_execution_id`/`message_id` 为键的 Redis SETNX 或去重表），重试不再重复扣费。
- **配置统一**：新增后端汇率配置出口 API，前端额度徽章（`account-money-extend`）汇率从硬编码 6.97 改为读取接口（`RMB_TO_USD_RATE=7.26`）；`api/services/billing_extend.py` 硬编码初始额度 15 改读 `dify_config.ACCOUNT_TOTAL_QUOTA`。
- **校验缓存与容错**：`money_limit`（console）与 `is_money_limit`（web）增加短 TTL Redis 缓存，消除每请求查库与代码内 TODO；`is_money_limit` 的裸 `except: return True` 改为精确异常处理，DB 异常时按 fail-open 放行并告警（避免 DB 抖动误拦全部 WebApp 请求）。
- **数据治理**：为 `api_token_message_joins_extend` 设计并落地归档/TTL 策略（定期归档 Celery 任务）；三个额度重置定时任务（`update_account_used_quota_extend`、`update_api_token_daily_used_quota_task_extend`、`update_api_token_monthly_used_quota_task_extend`）从全表 `.all()` 改为分批处理（`yield_per` 或 keyset 分页）。
- **挂点注册表入档**：把全部计费侵入点（约 21 处，含挂点文件、函数、被 hook 的上游函数名）登记进 `docs/dify-plus/与上游差异总表.md`（新增挂点注册表章节），作为每次上游升级的强制 checklist。

不属于本 change 范围：接入上游 quota v3（D1 决策为保持自建体系）、admin 废弃（Phase 4）、前端 contract 化规范整改（Phase 5）。

**BREAKING**：无对外 API 破坏。service_api 视图签名变化仅影响内部装饰器契约，对 HTTP 接口无感知。

## Capabilities

### New Capabilities

（`openspec/specs/` 目前为空；兄弟 change p0 定义的 capability 尚未归档，本 change 全部按新增 capability 处理，命名避开 p0 已用名称）

- `service-api-token-context`: service_api 层 api_token 的 contextvar 传递机制——额度校验装饰器不再修改视图签名，controller 保持上游原样。
- `billing-deduction-idempotency`: 扣费的原子性与幂等性——账号额度原子 UPDATE、Celery 重试幂等键，保证并发与重试下金额守恒。
- `billing-config-unification`: 计费配置单一来源——汇率经 API 下发前端、初始额度统一读 `ACCOUNT_TOTAL_QUOTA`，消除硬编码。
- `quota-check-caching`: 额度校验的短 TTL 缓存与 DB 异常降级策略（fail-open + 告警）。
- `billing-data-governance`: 计费数据治理——`api_token_message_joins_extend` 归档/TTL、额度重置任务分批处理。
- `billing-hook-registry`: 计费挂点注册表——21 处侵入点坐标入档，作为升级强制 checklist。

### Modified Capabilities

（无——`openspec/specs/` 中尚无已归档 spec）

## Architecture Impact

- **不新增服务、不新增存储**：contextvar 是 Python 标准库能力；幂等键与校验缓存复用现有 Redis（`extensions.ext_redis.redis_client`，workflow 扣费任务已有 `billing:payer_id:*` 缓存先例）；归档任务复用现有 Celery `extend_low` 队列与 `CeleryScheduleTasksConfig` 风格开关（挂载到 `api/configs/extend/__init__.py`）。
- **模块边界**：contextvar 的定义与读写收敛在 service_api 层（`wraps.py` + 下游取用点），不向 core/services 层扩散；下游消费方（`app_generator.py` 的 `extras["app_token_id"]` 写入等 p0 恢复的挂点）改为从 contextvar 读取。
- **行为契约保持不变**（改造红线）：付款人归因优先级 `message.from_account_id` → `from_end_user_id` 是真实 Account 直接用 → 查 `end_user_account_joins_extend` 最新一条；扣费金额 `total_price`（USD）或 ÷ `RMB_TO_USD_RATE`；额度记录缺失时扣费即建档（`total_quota=ACCOUNT_TOTAL_QUOTA`）；限额 -1 表示不限；事前拦截 + 事后异步扣费语义保持。
- **数据模型**：可能新增一张去重表（若幂等键选去重表方案）或仅用 Redis SETNX（零 schema 变更）；归档策略若选归档表需新增 `migrations_extend` 迁移。具体选型在 design.md 论证。
- **测试模式**：需要并发扣费、幂等重试、限额边界、缓存 TTL 四类新测试；复用 p0 建立的 6 条计费回归链路清单作为整体回归门槛。

## Impact

- **api 代码**：`api/controllers/service_api/wraps.py` 及 `api/controllers/service_api/app/` 下 9 个 controller；`api/events/event_handlers/update_account_money_when_messaeg_created_extend.py`；`api/tasks/extend/update_account_money_when_workflow_node_execution_created_extend.py`（加幂等键）；`api/services/billing_extend.py`；`api/controllers/console/money_extend.py`；`api/controllers/web/completion.py`；`api/schedule/` 下 3 个 `*_extend.py` 重置任务；新增归档任务与汇率配置 API；`api/configs/extend/__init__.py` 新增开关。
- **web 代码**：`web/app/components/header/account-money-extend/index.tsx`（或 p3 迁移后的 main-nav 等价位置）汇率改读接口。
- **数据库**：视 design 选型，可能新增去重表/归档表迁移（`api/migrations_extend/`）；无上游表变更。
- **文档**：`docs/dify-plus/与上游差异总表.md` 新增挂点注册表章节。
- **运行时行为**：并发扣费金额守恒（此前会少扣）；重试不重复扣费；额度校验延迟下降（缓存）；DB 抖动时 WebApp 请求不再被误拦（fail-open）；`api_token_message_joins_extend` 存量增长受控。
- **下游依赖**：挂点注册表是 Phase 4/5 及未来所有上游升级的对照物；service_api 签名归零直接降低下次合并的冲突面。
