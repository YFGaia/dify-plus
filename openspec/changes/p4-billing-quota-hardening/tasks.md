# Tasks: p4-billing-quota-hardening

> 前置依赖：p3-merge-upstream-1-15-0 已完成并通过回归。行为契约红线见 design.md Context 第 6 条，所有任务不得破坏。

## 1. 前置核实（主 agent，先于全部实现）

- [ ] 1.1 确认 p3 后基线：`git grep -l '<<<<<<<' -- api/ web/` 为空、p0 的 6 条计费回归链路在当前基线全绿；记录当前对照上游 tag（如 `1.15.0`）供 `git diff` 验收使用
- [ ] 1.2 逐路径盘点 api_token 消费点线程归属（design D1 关键约束）：9 个 controller × 各 generate 路径，确认 `ApiTokenMessageJoinsExtend` 写入与 `extras["app_token_id"]` 赋值是否仍在请求线程内；产出消费点清单（文件、行号、线程归属、改造方式 contextvar 直读/物化到 extras），作为工作项 A 的输入
- [ ] 1.3 核实 p3 后前端额度徽章组件实际位置（`web/app/components/header/account-money-extend/` 或 main-nav 新体系），以及 p3 是否已顺手完成汇率改造（design D7 核对项）；核实 `account_money_extend.account_id` 是否有唯一约束（design Q3），确定 D3 建档竞态兜底方式

## 2. 工作项 A：收窄 service_api 侵入面（最重要）

- [ ] 2.1 新建 `api/libs/api_token_context_extend.py`：`ContextVar[ApiToken | None]` + `set_current_api_token`/`get_current_api_token`/`reset_current_api_token` helper，附模块 docstring 说明线程边界约束；配套单测（set/get/reset、未设置时返回 None、上下文隔离）
- [ ] 2.2 改造 `api/controllers/service_api/wraps.py::validate_app_token`：删除 `kwargs["api_token"] = api_token`，改为装饰器内 set contextvar 并在 `finally` 中 reset；前置校验语义不变（账号超限/密钥日月超限/`-1` 不限）
- [ ] 2.3 按 1.2 清单改造下游消费点：`completion.py`/`workflow.py` 删除 `args["api_token"] = api_token`；`api/core/app/apps/{chat,agent_chat,completion}/app_generator.py` 的 `args.get("api_token")` 改为 `get_current_api_token()`（或按清单物化方案）；workflow/advanced_chat 路径的 `extras["app_token_id"]` 在请求线程内经 contextvar 取值物化
- [ ] 2.4 将 9 个 controller（`completion`、`workflow`、`conversation`、`message`、`file`、`audio`、`site`、`app`、`annotation`）方法签名恢复为上游原样，删除 `api_token` 形参与 fork 注释
- [ ] 2.5 验收：`git diff <upstream-tag> -- api/controllers/service_api/app/` 签名归零；API 密钥调用 completion/chat/workflow 后 `api_token_message_joins_extend` 有新行（硬断言）；`make lint` + `make type-check` 通过

## 3. 工作项 B：扣费幂等与原子化

- [ ] 3.1 改造 `api/events/event_handlers/update_account_money_when_messaeg_created_extend.py`：账号扣费改原子 UPDATE（`AccountMoneyExtend.used_quota + price`，`synchronize_session=False`）+ `rows_updated == 0` 建档 + 按 1.3 结论兜底建档竞态（捕获 IntegrityError 退回 UPDATE）；归因优先级与金额换算逻辑不动
- [ ] 3.2 为 `api/tasks/extend/update_account_money_when_workflow_node_execution_created_extend.py` 增加幂等键：`SET billing:dedup:<task>:<node_execution_id> NX EX 86400`，键存在则记日志跳过；重试前删除键；Redis 连接异常记 warning 后继续执行（design D2）
- [ ] 3.3 编写测试：并发扣费守恒（同账号多线程并发 N 次、金额恰好 N×p）、幂等重试（重复投递只扣一次、失败重试可执行）、缺记录建档（`total_quota=ACCOUNT_TOTAL_QUOTA`）、归因优先级三分支、人民币换算

## 4. 工作项 C：配置统一

- [ ] 4.1 `api/controllers/console/workspace/account_extend.py::AccountMoneyApi` 响应增加 `rmb_to_usd_rate` 字段（`dify_config.RMB_TO_USD_RATE`）
- [ ] 4.2 按 1.3 定位结果改造前端额度徽章：删除 `exchangeRate = 6.97` 硬编码，改读响应字段，缺字段回退默认值；更新 `web/models/common-extend` 的 `UserMoney` 类型；`pnpm lint:fix` + `pnpm type-check:tsgo` 通过（若 p3 已完成前端侧，则只做前后端口径核对）
- [ ] 4.3 `api/services/billing_extend.py::calculate_user_billing_information` 的 `total_quota=15` 改 `dify_config.ACCOUNT_TOTAL_QUOTA`，删除 TODO 注释；`rg` 确认 api 计费链路无残留初始额度魔法数字

## 5. 工作项 D：校验缓存与容错

- [ ] 5.1 新增配置 `QUOTA_CHECK_CACHE_TTL`（默认 30，挂 `api/configs/extend/__init__.py`）；实现额度校验缓存 helper（账号键 `billing:quota_check:<account_id>`、密钥键 `billing:quota_check:token:<app_token_id>`，存额度快照）
- [ ] 5.2 应用缓存到三处读路径：`money_limit`（console）、`is_money_limit`（web）、`validate_app_token` 前置校验（与工作项 A 协调，见 7.2），删除两处 `# TODO 需要写入缓存，读缓存`
- [ ] 5.3 `is_money_limit` 容错改造：裸 `except: return True` 改精确捕获——`RedisError` 降级查库、`SQLAlchemyError` 记 warning（含 end_user_id）后 fail-open 返回 False；`money_limit` 与 wraps 校验采用同一降级策略
- [ ] 5.4 编写测试：缓存命中不查库、TTL 过期重载、超限判断语义不变（含 `-1` 不限与临界值）、DB 异常 fail-open、Redis 异常降级查库

## 6. 工作项 E：数据治理

- [ ] 6.1 `api/migrations_extend/` 新增迁移：建归档表 `api_token_message_joins_archive_extend`（结构同源表）；验证 `flask extend_db upgrade`/`downgrade` 双向可执行
- [ ] 6.2 新增归档 Celery 任务与 beat 注册（`extend_low` 队列、每日执行）：按 `created_at < now() - API_TOKEN_MESSAGE_JOINS_RETENTION_DAYS`（默认 180）分批 5000 行、每批独立事务 `INSERT ... SELECT` + `DELETE`，记录批次日志；新增配置 `API_TOKEN_MESSAGE_JOINS_RETENTION_DAYS`、开关 `ENABLE_API_TOKEN_MESSAGE_JOINS_ARCHIVE_TASK`（默认开启）
- [ ] 6.3 三个重置任务（`api/schedule/update_account_used_quota_extend.py`、`update_api_token_daily_used_quota_task_extend.py`、`update_api_token_monthly_used_quota_task_extend.py`）快照阶段改 `yield_per(1000)` 流式分批落库；重置整表 UPDATE 不动
- [ ] 6.4 编写测试：归档行数守恒（迁移数=删除数）、保留期内数据不动、开关关闭不执行、反向还原可行；重置任务分批后快照全量正确、`used_quota` 归零

## 7. 工作项 F：挂点注册表入档（主 agent 终审）

- [ ] 7.1 以 A-E 改造后的代码为准盘点全部计费侵入点（约 21 处）：`rg` 扫描 `extend`/「二开部分」标记，交叉核对 p0 挂点清单与本 change 新增点（contextvar 模块、幂等键、缓存、归档任务）
- [ ] 7.2 在 `docs/dify-plus/与上游差异总表.md` 新增「计费挂点注册表」章节：每条含挂点文件、函数/位置、被 hook 的上游函数名、用途分类、对应回归链路编号、最近核对版本；附长期技术债清单（float 精度、Events 框架漂移、message handler 同步形态）
- [ ] 7.3 交叉验证：注册表条目与实际代码坐标一一对应；9 个 controller 条目标注「签名已归零」

## 8. Parallelization Plan（并发执行策略）

- [ ] 8.1 **分工边界**：任务组 2-6 对应 6 个工作项中的 5 个实现项（工作项 F 依赖全部完成），彼此文件边界如下、可由多个 sub-agent 并行实现：
  - **A（任务组 2）**：写 `api/libs/api_token_context_extend.py`、`api/controllers/service_api/wraps.py`、`api/controllers/service_api/app/*`、`api/core/app/apps/*/app_generator.py`；读 proposal/design/specs、1.2 清单
  - **B（任务组 3）**：写 `api/events/event_handlers/update_account_money_when_messaeg_created_extend.py`、`api/tasks/extend/update_account_money_when_workflow_node_execution_created_extend.py` 及其测试
  - **C（任务组 4）**：写 `api/controllers/console/workspace/account_extend.py`、`api/services/billing_extend.py`、前端徽章组件与类型文件
  - **D（任务组 5）**：写 `api/controllers/console/money_extend.py`、`api/controllers/web/completion.py`、缓存 helper、配置文件——**注意 D 的 5.2 也要改 `wraps.py`**
  - **E（任务组 6）**：写 `api/migrations_extend/`、`api/schedule/*_extend.py`、新归档任务文件、`ext_celery.py` beat 注册、配置文件
- [ ] 8.2 **冲突协调（强制）**：工作项 A 与 D 均修改 `api/controllers/service_api/wraps.py`——两项 MUST 合并为同一 sub-agent 工作流（先 A 后 D 顺序执行），或 D 在 A 合入后基于新 wraps.py rebase 实施；`api/configs/extend/__init__.py` 被 D/E 同时追加配置项，由主 agent 合并解决（追加式冲突，风险低）
- [ ] 8.3 **sub-agent 输入约定**：每个 sub-agent 的 context inputs 为 proposal.md、design.md、对应 capability spec、1.x 前置核实产出；预期产出为代码改动 + 对应测试 + 自测通过记录（`make lint`/`make type-check`/目标测试，web 侧 `pnpm lint:fix`/`pnpm type-check:tsgo`）；不得改动分工边界之外的文件
- [ ] 8.4 **主 agent 顺序职责**：任务组 1 前置核实 → 分发 A-E → 合并分支/解决 `wraps.py` 与配置文件冲突 → 统一回归（任务组 9）→ 工作项 F 挂点注册表终审（任务组 7）

## 9. Architecture Verification（主 agent 统一回归）

- [ ] 9.1 签名归零验收：`git diff <upstream-tag> -- api/controllers/service_api/app/` 确认 9 个 controller 无签名差异；`wraps.py` 为 service_api 目录唯一 fork 差异点
- [ ] 9.2 四类专项测试全绿：并发扣费守恒、幂等重试、限额拦截边界（`-1` 不限/临界/缺记录建档）、缓存 TTL 行为（对应 3.3、5.4、6.4 及 A 的关联记录断言）
- [ ] 9.3 p0 的 6 条计费回归链路全绿：Console 调试扣费、Explore 扣费、WebApp 登录+扣费、Service API 日/月限额拦截、workflow LLM 节点扣费、月初额度重置
- [ ] 9.4 静态检查与构建：`make lint` + `make type-check` + `make test`（api）、`pnpm lint` + `pnpm type-check:tsgo` + `pnpm build`（web）
- [ ] 9.5 迁移与回滚验证：归档表迁移 upgrade/downgrade 双向通过；反向 `INSERT ... SELECT` 还原演练一次；确认所有新 Redis 键带 TTL（回滚无残留清理负担）
- [ ] 9.6 行为契约回归断言：归因优先级三分支、USD/RMB 换算、缺记录建档 `total_quota=ACCOUNT_TOTAL_QUOTA`、限额 `-1` 不限、事前拦截+事后异步扣费语义各有至少一条测试覆盖（防止 A-E 任一改造破坏红线）
