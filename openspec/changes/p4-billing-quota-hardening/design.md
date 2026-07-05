# Design: p4-billing-quota-hardening

## Context

前置依赖：p3-merge-upstream-1-15-0 已完成（代码基线为合并 1.15.0 后的 fork）。本设计基于当前代码实测确认的现状：

1. **api_token 注入链**（现状实测）：
   - `api/controllers/service_api/wraps.py::validate_app_token` 在装饰器内执行额度前置校验（账号总额度 `AccountMoneyExtend` + 密钥日/月额度 `ApiTokenMoneyExtend`），随后 `kwargs["api_token"] = api_token` 注入视图；
   - 9 个 controller（`service_api/app/` 下 `completion`、`workflow`、`conversation`、`message`、`file`、`audio`、`site`、`app`、`annotation`）的方法签名因此带 `api_token: ApiToken` 参数（实测约 30+ 处 `api_token` 引用）；
   - 其中 `completion.py`/`workflow.py` 进一步做 `args["api_token"] = api_token` 传入 `AppGenerateService.generate`，由 `api/core/app/apps/{chat,agent_chat,completion}/app_generator.py` 的 `args.get("api_token")` 消费，写 `ApiTokenMessageJoinsExtend` 关联记录；workflow/advanced_chat 路径经 `extras["app_token_id"]`（p0 恢复的挂点）。
2. **扣费写路径不对称**（现状实测）：
   - message 路径：`api/events/event_handlers/update_account_money_when_messaeg_created_extend.py`（Blinker 信号同步 handler）对 `AccountMoneyExtend.used_quota` 做「先 SELECT 再 `update({"used_quota": float(account_money.used_quota) + price})`」——读改写非原子，并发下丢失更新（少扣）；
   - workflow 路径：`api/tasks/extend/update_account_money_when_workflow_node_execution_created_extend.py` 已是原子 UPDATE（`AccountMoneyExtend.used_quota + price` + `synchronize_session=False`），且带 `max_retries=3` 重试——但**无幂等键**，重试成功前若 commit 已部分生效或任务被重复投递会重复扣费。
3. **配置硬编码**（现状实测）：`web/app/components/header/account-money-extend/index.tsx` L11 `const exchangeRate = 6.97`；`api/services/billing_extend.py` L104 `total_quota=15`（带 TODO 注释）；后端配置 `RMB_TO_USD_RATE`（默认 7.26）与 `ACCOUNT_TOTAL_QUOTA` 均已存在于 `api/configs/extend/__init__.py`。
4. **每请求查库的校验**（现状实测）：`api/controllers/console/money_extend.py::money_limit` 与 `api/controllers/web/completion.py::is_money_limit` 每请求 SELECT `AccountMoneyExtend`，代码内有 `# TODO 需要写入缓存，读缓存`；`is_money_limit` 尾部裸 `except: return True`——DB 抖动时全部 WebApp 请求被误判为超限拦截。`wraps.py` 的前置校验同样每请求 3 次 SELECT（tenant owner join、`AccountMoneyExtend`、`ApiTokenMoneyExtend`）。
5. **数据只增不清**：`api_token_message_joins_extend` 每次 API 调用插一行（`record_id` 索引查询只发生在消息/工作流创建后的短窗口内）；三个额度重置任务（`api/schedule/update_account_used_quota_extend.py` 等）用 `.all()` 全表加载做快照。
6. **行为契约（改造红线）**：付款人归因优先级 `message.from_account_id` → `from_end_user_id` 是真实 Account 直接用 → 查 `end_user_account_joins_extend` 最新一条；扣费金额 `total_price`（USD 直接用）或 ÷ `RMB_TO_USD_RATE`；额度记录缺失时扣费即建档（`total_quota=ACCOUNT_TOTAL_QUOTA`）；限额 `-1` 表示不限；事前拦截（装饰器/校验函数）+ 事后异步扣费语义保持。

Stakeholders：fork 维护者（升级冲突成本）、计费准确性（财务）、WebApp/Service API 终端用户（可用性）。

## Goals / Non-Goals

**Goals:**

- 9 个 service_api controller 方法签名相对上游归零（`git diff` 验证），api_token 改经 contextvar 传递。
- 账号扣费在并发下金额守恒（原子 UPDATE）；Celery 扣费任务重试幂等（不重复扣费）。
- 汇率与初始额度单一配置来源；前端不再硬编码。
- 额度校验加短 TTL 缓存；DB 异常时 fail-open 放行并告警，不误拦。
- `api_token_message_joins_extend` 存量增长受控；重置任务分批处理。
- 21 处计费挂点坐标入档 `docs/dify-plus/与上游差异总表.md`，形成升级强制 checklist。

**Non-Goals:**

- 不接入上游 quota v3（`quota_reserve/commit/release`，D1 决策保持自建体系）。
- 不改变计费的行为契约（见 Context 第 6 条红线）。
- 不做转发计费（`billing_forward`/AI 绘图）架构重构，仅修 `billing_extend.py` 的硬编码。
- 不做前端 contract 化整改（Phase 5）、不动 admin（Phase 4）。
- 不引入分布式事务或消息队列级 exactly-once 基础设施。

## Architecture Assessment

### Existing Design Reuse

- **原子 UPDATE 写法**：`update_account_money_when_workflow_node_execution_created_extend.py` L138-152 已有「原子自增 + `rows_updated == 0` 时建档」的成熟模式，message handler 直接对齐，不发明新模式。
- **Redis 复用**：幂等键与校验缓存复用 `extensions.ext_redis.redis_client`；workflow 扣费任务已有 `billing:payer_id:*` 缓存先例（TTL 1 小时、异常降级读库），键命名与容错风格沿用。
- **Celery 队列与开关**：归档任务复用现有 `extend_low` 队列；beat 注册沿用 `ext_celery.py` 的「二开部分」代码块与 p0 引入的 `CeleryScheduleTasksConfig` 风格 `ENABLE_*` 开关，配置挂载 `api/configs/extend/__init__.py`。
- **配置出口**：前端已调用 `GET /console/api/account/money`（`api/controllers/console/workspace/account_extend.py::AccountMoneyApi`），汇率字段直接扩展该响应，不新建端点。
- **迁移链**：新增表（归档表）走 `api/migrations_extend/` 独立 Alembic 链，与上游迁移不冲突。

### Boundaries and Ownership

- **contextvar 归属 service_api 层**：新建 `api/controllers/service_api/token_context_extend.py`（独立 `*extend*` 文件，隔离 fork 逻辑），定义 `ContextVar` 与 `get/set/reset` helper。写入方唯一：`validate_app_token` 装饰器；读取方：service_api controller 内部（如需要）与 `app_generator.py` 的关联记录写入点。**core 层不 import controller 模块**——为避免层次倒置，contextvar 定义放 `libs/` 或 `core/` 可达的中立位置（最终定为 `api/libs/api_token_context_extend.py`，controller 与 core 均可 import）。
- **生命周期职责**：装饰器负责 set 与（`finally` 中）reset，保证请求间不串值；跨线程边界（`app_generator` 的 worker 线程）**不依赖 contextvar 自动传播**——在请求线程内（spawn worker 之前）完成读取与落库，这与现状一致（`ApiTokenMessageJoinsExtend` 写入与 `extras["app_token_id"]` 赋值均发生在请求线程）。实现时必须逐路径核实读取点仍在请求线程内，若上游 1.15 重构后读取点已进入 worker 线程，则在 spawn 前显式取值放入 `application_generate_entity.extras`。
- **幂等键所有权**：每个扣费任务/handler 自己管理自己的幂等键（键含任务名前缀），失败路径负责释放（删除键）以允许重试。
- **缓存所有权**：额度校验缓存由「校验读路径」写入与读取；扣费写路径**不负责失效**（短 TTL 自然过期，见 Decisions D4 的取舍）。
- **归档数据所有权**：归档任务拥有 `api_token_message_joins_extend` 老数据的迁出与删除；扣费链路只负责插入与短窗口查询。

### Options and Rationale

见 Decisions。总体原则：全部工作项都不新增服务、不新增外部依赖；唯一 schema 变更是可选的归档表（D5）。

### Quality Attributes

- **正确性/可靠性**：原子 UPDATE 消除丢失更新；幂等键消除重试重复扣费；fail-open 策略把「DB 抖动 → 全站误拦」降级为「短暂放行 + 告警」（事后异步扣费仍会补记，财务风险有限）。
- **性能**：额度校验从每请求 1-3 次 SELECT 降为缓存命中 0 次；重置任务分批避免大表全载 OOM。
- **可维护性/升级成本**：9 个 controller 签名归零是本 change 最大收益——下次上游合并时 service_api 目录理论上零冲突；挂点注册表把隐性知识显性化。
- **可观测性**：幂等跳过、fail-open 放行、归档批次量均打 warning/info 日志（含 account_id/token_id/message_id 上下文）。
- **可测试性**：contextvar helper、幂等键、缓存均为纯函数式小接口，可单测；并发守恒用多线程集成测试。

### Complexity and Exceptions

新增元素仅三项，均为最小面：一个 contextvar 模块（标准库，零依赖）、若干 Redis 键命名空间（`billing:dedup:*`、`billing:quota_check:*`）、一张归档表 + 一个 beat 任务。无新服务、无新协议、无跨模块基础设施。回滚方式见 Migration Plan。

## Decisions

### D1: api_token 传递用 contextvar（而非 flask.g 或保持 kwargs）

- **选择**：`contextvars.ContextVar`，定义在 `api/libs/api_token_context_extend.py`，装饰器 set / `finally` reset，helper `get_current_api_token() -> ApiToken | None`。
- **替代方案 A（flask.g）**：同样能去掉签名侵入，但 `g` 绑定 app context，worker 线程会 push 新 app context 导致取不到值——与 contextvar 的线程局限相同，却额外耦合 Flask，且单测需要 app context。拒绝。
- **替代方案 B（保持 kwargs 注入）**：现状，升级冲突成本已被证明过高。拒绝。
- **关键约束**：contextvar 不跨线程自动传播。现状全部消费点（`app_generator` 写关联记录、`extras["app_token_id"]` 赋值）都在请求线程内，实现时逐路径核实；任何进入 worker 线程后才需要的值必须在 spawn 前物化到 `extras`/args。
- **配套**：`completion.py`/`workflow.py` 的 `args["api_token"] = api_token` 一并移除，`app_generator.py` 消费点改调 `get_current_api_token()`（或维持 extras 物化方案，以逐路径核实结果为准）；9 个 controller 删除 `api_token` 形参与相关注释，回归上游原样。

### D2: 幂等键用 Redis SET NX + TTL（而非去重表）

- **选择**：任务开始时 `SET billing:dedup:<task>:<business_id> 1 NX EX 86400`（business_id = `message_id` 或 `node_execution_id`）；设置失败（键已存在）→ 记日志并直接返回；任务异常进入重试前 `DELETE` 该键，保证重试可执行；commit 成功后保留键至 TTL 过期。
- **替代方案（去重表 + 唯一约束）**：强持久化、Redis 清空也不失效，但每次扣费多一次 DB 写与一张长增表，又回到数据治理问题。考虑到重复扣费窗口仅存在于「Celery 重试/重复投递」场景且 TTL 覆盖重试周期（3 次 × 60s « 24h），Redis 方案足够。拒绝去重表。
- **风险承担**：Redis flush 的瞬间窗口可能放过一次重复投递——概率极低且金额为单条消息费用，接受。
- **适用范围**：workflow 扣费任务（已有重试）必加；message handler 当前是同步 Blinker handler（无重试语义），本 change 若将其保持同步则只做原子化不加幂等键，若改造为 Celery 任务则同套幂等模板（见 Open Questions Q1 → 决议：保持同步，只做原子化）。

### D3: message handler 只做原子化，不改异步

- **选择**：`update_account_money_when_messaeg_created_extend.py` 保持 Blinker 同步 handler，账号扣费改为与 workflow 任务相同的原子 UPDATE + `rows_updated == 0` 建档；建档处补捕获唯一约束冲突（并发首扣竞态：insert 失败则退回原子 UPDATE 重试一次）。
- **替代方案（改为 Celery 异步任务）**：统一两条路径的形态，但改变「消息创建事务后同步记账」的既有语义，引入投递失败丢账风险，且超出「加固」范围。拒绝。
- **注意**：`api_token_money_extend` 部分该 handler 已是原子自增表达式，不动。

### D4: 额度校验缓存——短 TTL、fail-open、不做主动失效

- **选择**：Redis 键 `billing:quota_check:<account_id>`，缓存 `used_quota/total_quota` 快照，TTL 30s（配置项 `QUOTA_CHECK_CACHE_TTL`，默认 30）；应用点：`money_limit`（console）、`is_money_limit`（web）、`validate_app_token` 内的账号额度与密钥日/月额度前置校验（密钥缓存键 `billing:quota_check:token:<app_token_id>`）。Redis 或 DB 异常时：记 warning 告警日志 + fail-open 放行。
- **`is_money_limit` 容错改造**：裸 `except: return True`（fail-closed，误拦）改为 `except SQLAlchemyError`/`RedisError` 精确捕获 → `logger.warning`（含 end_user_id）→ `return False`（fail-open）。**权衡**：fail-open 在 DB 故障窗口内放行可能超限的请求，但事后异步扣费在 DB 恢复后仍会入账，超扣有限且可追；fail-closed 则是全站 WebApp 不可用——可用性损失远大于财务风险。选 fail-open。
- **不做扣费后主动失效**：额度校验本就是「事前拦截 + 事后扣费」的最终一致语义，30s 的脏读窗口与现有语义（扣费异步、消息生成本身耗时数秒）同量级，主动失效增加写路径耦合但收益甚微。拒绝。

### D5: `api_token_message_joins_extend` 治理——归档表 + 定期批量迁移

- **选择**：新增归档表 `api_token_message_joins_archive_extend`（结构同源表，`migrations_extend` 建表），新增 Celery beat 任务（`extend_low` 队列，每日执行，开关 `ENABLE_API_TOKEN_MESSAGE_JOINS_ARCHIVE_TASK`）：按 `created_at < now() - retention` 分批（每批 5000 行）`INSERT INTO ... SELECT` + `DELETE`，retention 配置 `API_TOKEN_MESSAGE_JOINS_RETENTION_DAYS` 默认 180。
- **替代方案 A（直接 TTL 删除）**：最简单，但该表是「密钥 ↔ 消息」的唯一映射，计费争议追溯需要。拒绝。
- **替代方案 B（PostgreSQL 分区表）**：查询与运维最优，但把现表改造成分区表需要重建迁移与停机窗口，对一张纯 fork 表成本过高。拒绝。
- **运行时无影响**：扣费链路只在消息创建后的短窗口查 `record_id`，180 天 retention 远大于窗口。

### D6: 重置任务分批——`yield_per` 流式 + 分批 flush

- **选择**：三个重置任务的快照阶段改 `db.session.query(...).yield_per(1000)`，快照对象按 1000 条一批 `bulk_save_objects`/`add_all` + `flush`；重置阶段保持现有整表 `UPDATE`（本就是单条 SQL，无需分批）。
- **替代方案（keyset 分页）**：适合超大表与可中断恢复，但三张额度表行数 = 账号数/密钥数（万级），`yield_per` 已足够且改动最小。拒绝。

### D7: 汇率出口——扩展 `GET /console/api/account/money` 响应

- **选择**：`AccountMoneyApi` 响应增加 `rmb_to_usd_rate` 字段（值取 `dify_config.RMB_TO_USD_RATE`）；前端 `account-money-extend`（或 p3 迁移后的 main-nav 等价组件）从响应读取汇率，删除 `exchangeRate = 6.97` 硬编码；若响应缺字段则回退默认值并告警（兼容滚动发布）。**核对项**：若 p3 已顺手完成前端改造，本项收敛为「后端出字段 + 前后端核对一致」。
- **替代方案（独立配置端点 `GET /console/api/system/billing-config`）**：更通用但多一次请求与一个新端点，当前仅一个消费方。拒绝。
- **配套**：`billing_extend.py::calculate_user_billing_information` 的 `total_quota=15` 改 `dify_config.ACCOUNT_TOTAL_QUOTA`，删除 TODO。

### D8: 挂点注册表——落在《与上游差异总表》新章节

- **选择**：`docs/dify-plus/与上游差异总表.md` 新增「计费挂点注册表」章节，表格列：挂点文件、函数/位置、被 hook 的上游函数名、用途（归因/校验/扣费/重置）、对应回归链路（p0 的 6 条链路编号）、最近核对版本。本 change 完成后全部条目坐标以改造后代码为准（如 wraps.py 条目更新为 contextvar 方案）。
- **替代方案（独立新文档）**：多一个文档入口，升级 checklist 分散。拒绝——差异总表已是升级时的必读文档。

## Risks / Trade-offs

- [contextvar 在某条路径上跨线程取不到值，关联记录静默丢失 → 密钥额度不再累计] -> 实现时对 9 个 controller × 每条 generate 路径逐一核实读取点线程；消费点取不到值时打 warning 日志（带 request path）；回归验证以「API 调用后 `api_token_message_joins_extend` 有新行」为硬断言。
- [fail-open 在 DB 故障窗口放行超限请求] -> 事后异步扣费恢复后补记；告警日志可聚合监控；窗口内超扣金额与故障时长线性有界。已在 D4 论证接受。
- [Redis 不可用时幂等键退化] -> 幂等键 SET 失败（连接异常，而非键已存在）按「无法确认幂等」处理：记 warning 后继续执行（宁可极小概率重复，不可停止扣费）；与既有 `billing:payer_id` 缓存的降级风格一致。
- [归档任务与在线扣费竞争锁] -> 归档只处理 `created_at` 超过 180 天的行，与在线写入无行级重叠；分批 + 每批独立事务限制锁持有时间。
- [9 个 controller 签名回归时误删上游同名参数或漏删 fork 注释] -> 验收用 `git diff upstream/<tag> -- api/controllers/service_api/app/` 逐文件核对签名归零；保留 wraps.py 单文件为唯一 fork 差异点。
- [message handler 原子化后 float 精度问题被放大] -> 沿用现有 float 语义不在本 change 扩大战场（换 Decimal 属行为变更），注册表中登记为长期项。

## Migration Plan

1. **部署顺序**：先 api（含新迁移 `flask extend_db upgrade` 建归档表）→ 后 web（前端读汇率字段，带默认值回退，天然兼容先后顺序）。
2. **配置**：新增 `QUOTA_CHECK_CACHE_TTL`、`API_TOKEN_MESSAGE_JOINS_RETENTION_DAYS`、`ENABLE_API_TOKEN_MESSAGE_JOINS_ARCHIVE_TASK`（默认值使行为安全：缓存 30s、retention 180 天、归档任务默认开启）。
3. **回滚**：代码回滚即恢复旧行为；归档表数据可通过反向 `INSERT ... SELECT` 还原；Redis 键均带 TTL 自清理，无需清理动作；无不可逆迁移（归档表 DROP 前先确认已还原）。
4. **验证门槛（上线前）**：并发扣费守恒测试、幂等重试测试、限额拦截边界测试（含 `-1` 不限、临界值、缺记录建档）、缓存 TTL 行为测试全绿；p0 的 6 条计费回归链路全绿；`git diff` 确认 9 个 service_api controller 签名相对上游归零。

## Open Questions

- Q1（已决议，见 D3）：message handler 是否改异步？——否，保持同步只做原子化。
- Q2：p3 合并后 `account-money-extend` 组件的实际位置（header 还是 main-nav 新体系）需在实现时以当时代码为准定位；本设计按「等价组件」表述。
- Q3：`account_money_extend.account_id` 是否已有唯一约束（影响 D3 建档竞态的兜底方式）——实现时核实迁移定义，无则以「捕获 IntegrityError 退回 UPDATE」兜底，不新加约束（避免存量脏数据阻塞迁移）。
