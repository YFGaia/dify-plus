## ADDED Requirements

### Requirement: 三段升级命令链按固定顺序执行

1.15.0 升级 SHALL 按固定顺序执行三段命令链：`flask db upgrade`（上游 24 个新迁移）→ `flask extend_db upgrade`（fork 迁移链）→ `flask backfill-plugin-auto-upgrade`（租户插件自动升级设置回填）。`backfill-plugin-auto-upgrade` MUST 执行——跳过将导致租户插件自动升级设置静默失效。该序列 MUST 写入 `docs/dify-plus/上游升级与回归检查清单.md` 作为强制项。

#### Scenario: 迁移链在测试库演练通过

- **WHEN** 在 1.14.2 基线的测试库上依次执行三段命令
- **THEN** 三段命令全部成功，双 Alembic 链各自到达 head，无迁移冲突

#### Scenario: backfill 结果可验证

- **WHEN** `flask backfill-plugin-auto-upgrade` 执行完成
- **THEN** 已安装插件的租户其插件自动升级设置数据非空，插件自动升级功能正常工作

### Requirement: 环境变量按 19 增 2 删 1 改对齐

`.env` 模板与生产环境变量 SHALL 与上游 1.15.0 对齐：新增 19 项（含 `SSRF_PROXY_ALLOW_PRIVATE_DOMAINS`、`SSRF_PROXY_ALLOW_PRIVATE_IPS`）、删除 2 项、改名 1 项（`UV_CACHE_DIR`）；`docker/docker-compose.dify-plus.yaml` 与 `.env.example` MUST 同步更新。

#### Scenario: env 模板对齐

- **WHEN** 对比 fork 的 env 模板/compose 环境块与上游 1.15.0 基线
- **THEN** 19 增 2 删 1 改全部落实，被删除/改名的旧变量名不再出现

### Requirement: SSRF 私有网络白名单强制配置并验证

上游 1.15.0 的 SSRF 代理默认拒绝私有域名/IP。升级 SHALL 在上线前配置企业内网允许清单（`SSRF_PROXY_ALLOW_PRIVATE_DOMAINS` / `SSRF_PROXY_ALLOW_PRIVATE_IPS`），并 MUST 回归验证 http 节点/工具调用：加白目标可访问，未加白的私有目标仍被拒绝。此项 MUST 作为升级 checklist 强制项。

#### Scenario: 加白内网目标可访问

- **WHEN** 配置白名单后，workflow http 节点请求清单内的内网地址
- **THEN** 请求成功返回，不出现 403 拒绝

#### Scenario: 未加白目标默认拒绝

- **WHEN** http 节点请求不在白名单内的私有 IP/域名
- **THEN** 请求被 SSRF 防护拒绝（安全默认值未被关闭）

### Requirement: 升级完成后打 tag 留档

全量回归通过并合入主线后，SHALL 打 git tag 留档（如 `fork-merged-1.15.0`），作为里程碑 M3 锚点与后续回滚参照。

#### Scenario: tag 存在

- **WHEN** 合入并回归通过后检查 `git tag`
- **THEN** 存在指向合并完成提交的留档 tag
