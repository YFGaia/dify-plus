# quota-reset-scheduling Specification

## Purpose

TBD - created by archiving change p0-restore-billing-hooks. Update Purpose after archive.

## Requirements

### Requirement: ext_celery SHALL 注册 3 个额度重置 beat 任务

`api/extensions/ext_celery.py` SHALL 恢复「二开部分 Begin/End」代码块（对照 `origin/main` 约 L188-209），将以下任务加入 `imports` 与 `beat_schedule`：

- `schedule.update_account_used_quota_extend.update_account_used_quota_extend`：`crontab(minute="0", hour="0", day_of_month="1")`，每月 1 号重置账号额度；
- `schedule.update_api_token_daily_used_quota_task_extend.update_api_token_daily_used_quota_task_extend`：`crontab(minute="0", hour="0")`，每天重置密钥日额度；
- `schedule.update_api_token_monthly_used_quota_task_extend.update_api_token_monthly_used_quota_task_extend`：`crontab(minute="0", hour="0", day_of_month="1")`，每月 1 号重置密钥月额度。

任务本体（`api/schedule/*_extend.py`，队列 `extend_low`）已存在，MUST NOT 修改。

#### Scenario: beat 配置包含 3 个 extend 任务

- **WHEN** 应用启动后检查 `celery_app.conf.beat_schedule` 与 `imports`（或查看 celery beat 启动日志）
- **THEN** 包含 `update_account_used_quota`、`update_api_token_daily_used_quota_task_extend`、`update_api_token_monthly_used_quota_task_extend` 三个条目，调度表达式与上述定义一致

#### Scenario: 手动触发重置任务生效

- **WHEN** 手动调用任一重置任务（如 `update_api_token_daily_used_quota_task_extend.delay()`）并等待消费
- **THEN** 对应额度表（`account_money_extend` 或 `api_token_money_extend` 相关表）的周期用量字段被重置

### Requirement: beat 任务注册 SHALL 受配置开关控制

额度重置任务的注册 SHALL 由 `CeleryScheduleTasksConfig` 风格的布尔配置开关控制（`Field` 定义带 description），开关定义在 fork 配置入口 `api/configs/extend/__init__.py`，默认值 MUST 为开启（保持与 `origin/main` 行为一致，部署无需新增环境变量）。

#### Scenario: 默认配置下任务注册

- **WHEN** 未设置任何相关环境变量启动应用
- **THEN** 3 个额度重置任务全部注册进 beat_schedule

#### Scenario: 关闭开关后任务不注册

- **WHEN** 通过环境变量将开关设为 false 后启动应用
- **THEN** beat_schedule 与 imports 中不包含对应的 extend 重置任务
