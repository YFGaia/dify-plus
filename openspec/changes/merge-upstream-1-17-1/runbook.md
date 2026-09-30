# 环境升级与恢复操作包模板

> 本文仍是生产环境操作模板，尚未完成环境化定稿。2026-09-30 已对精确候选执行隔离的本机 Compose 空库演练：PostgreSQL 双迁移、API/Web、管理员额度读写和 DeepSeek 插件 `0.0.24` 安装通过；补做的 MySQL 8.0.46 检查在扩展迁移因 `uuid_generate_v4()` 默认值不兼容而失败。静态迁移审计未读取旧库数据。真实 LLM 请求与额度读回待用户在隔离工作区保存密钥后继续。证据见 `evidence/V03/`。这些演练未连接真实部署或业务数据，不替代 A03、旧库副本/数据审计、镜像供货、向量与目标数据库引擎矩阵，也不构成生产验收。生产命令尚未执行；服务启动由获授权的运维执行者负责，并遵循 `api/AGENTS.md` 的服务启动限制。

## 1. 实施前须填写的参数

| 字段                                                   | 要求 / 当前状态                                                                          |
| ------------------------------------------------------ | ---------------------------------------------------------------------------------------- |
| 发布负责人、DB/向量负责人、业务验收人                  | 待指定；同一人可兼任                                                                     |
| 实际旧源码/镜像平台/digest、目标所有服务 digest        | 待读取；官方 api/web 镜像不能替代 fork 镜像                                              |
| Compose 文件、project、所有 override/profile、env 文件 | 待读取；全部组合固定，不能只读取主文件                                                   |
| DB 类型/版本/容量、两个版本表                          | 待读取；预期源码主链起点 `7a1c2d9e4b60`，extend 可能 018 或 019，以真实库为准            |
| 向量引擎/版本、节点/副本数、schema 与数据卷            | 待读取；仓库 Weaviate 1.19.0 不是线上事实                                                |
| Redis/队列、对象/文件、插件、Agent 持久目录            | 列出实际全部存储、外部服务与跨系统关联                                                   |
| 密钥                                                   | 只记录受控秘密存储引用；保留原解密密钥，禁止抄入 Git/日志                                |
| 维护窗口与中止时点、RTO/RPO、备份保留期                | 待定；RTO 用 V06 实测，迁移/向量耗时用 V03/V04 实测                                      |
| 在途工作流、Human Input、调度与任务重投策略            | 记录排空截止时间、冻结状态、恢复旧 Redis 后允许重投清单与幂等限制                        |
| 阈值                                                   | 新增鉴权绕过/账目差异/数据丢失：零容忍；5xx、P95、队列积压等按升级前基线填数值与持续时长 |
| 真实测试配置                                           | 模型/SSO/钉钉/插件/知识库样本等由用户已有环境提供；缺项标 blocked                        |

A03 先完成事实盘点与操作草案，V03 在演练前固定 digest、override 和具体备份恢复命令；R02 根据演练结果最终签收，所有“待读取/待定”必须已替换成环境事实和命令。发现实际源版本不是分析起点时，重算迁移序列；不得 `stamp` 跳过历史迁移。源码支持两种 DB 的交付需覆盖两者；单个生产环境至少完整演练其真实引擎。

## 2. 构建与供货

1. 固定合并候选 commit、manifest/lock 摘要、Node 24.20.0、pnpm 12.3.4、Python 3.12。构建目标 fork API/Web；保留钉钉依赖、sandbox-full 和专用 worker。
2. 发布矩阵记录 API/Web、plugin daemon、sandbox/sandbox-full、Agent backend/local sandbox、SSRF、nginx、DB/Redis/vector 的 tag、digest、架构、来源和兼容结果。目标上游 plugin daemon 为 0.6.10-local、标准 sandbox 为 0.2.15，私有镜像需实际供应确认。
3. 在目标平台实际拉取并启动验证；CI `push:false` 只能证明构建。发布记录绑定源码 SHA 与 digest，禁止用 `latest`。
4. `docker compose ... config --quiet` 检查完整组合；完整 `config` 可能展开密钥，只在受控环境查看，归档脱敏摘要。`MIGRATION_ENABLED=false` 必须作用于全部业务 API 镜像服务。

## 3. 隔离副本与静止点

先用旧镜像读取副本，确保副本真实可用，再演练升级。隔离 Compose project、端口、网络、数据库、Redis、对象前缀、文件卷与回调地址；关闭真实邮件、webhook 和外部有副作用任务。不得让副本消费生产队列。

生产 D01：封入口/停新调度 → 排空或冻结在途执行 → 停 API、worker、beat、Agent、插件及所有外部写入者 → 核实静止 → 备份关系库、向量、Redis、文件/对象、插件/Agent 数据、旧配置/镜像。数据库 MVCC 快照自身不能保证其他系统同一时点，由停写保证一致性。外部对象存储用版本/快照或受控复制清单，不能假定一个本地 tar 已覆盖。

备份 manifest 至少记录：快照 ID、UTC 时间、源版本、校验和或提供方 snapshot ID、恢复命令、密钥引用、存储容量、验证结果、RPO。恢复完整性验证在 V06 完成，生产快照再做可读性与范围检查。禁止 `down -v`、清理原卷或在线裸复制正在写入的 LSM 数据。

## 4. 向量库决策

- **非 Weaviate**：维持真实引擎版本，确认目标驱动的写入、检索、删除、重建与恢复；D02 记录“无需更换引擎”及证据。
- **Weaviate ≥1.27**：按官方逐 minor 路径至目标 1.39.2，每个 minor 选已核验 patch；版本已高于目标时不直接降级，先验证兼容并调整环境矩阵。
- **Weaviate <1.27**：V04 单列副本路径，逐站验证格式/元数据与 v4 客户端切换；原位升级无法证明安全时，评估逻辑导出、重建和重新索引。无演练证据则生产 D02 blocked。
- 每站 graceful stop，等待完全退出再启动下一版本；检查 schema、对象与向量数量、同一查询集合的向量/关键词结果、批量写入和备份恢复。只看 ready 或数量不构成验收。
- 目标代码接受 `grpc://weaviate:50051`，`grpcs://` 才启用 TLS；网络与证书以环境为准。端口可达不等于真实检索成功。

参考：[Dify server migration path](https://docs.dify.ai/en/self-host/deploy/troubleshooting/weaviate-server-migration-path)、[Weaviate migration](https://docs.weaviate.io/deploy/migration)。旧 v4 指南的直接跨版本或复制命令不作为本轮生产执行依据。每站失败停止推进，用该站之前与其他存储匹配的快照恢复，禁止直接降镜像读新目录。

## 5. 双迁移操作入口

主链 17 项的 revision、数据影响详见 [后端分析第 4 节](research/backend-analysis.md)。尤其记录 `f6e4c5686857` / `89919253ca7a` 删除表、`fbdfcf5f5a6e` / `925e75620b69` JSON 清理、`9b7c6d5e4f3a` 邮箱归一和 `5578e028b2f2` 凭据去重。去重赢家规则须在副本对照，冲突内容有业务争议就停止；不能把“最新一行”自动视为业务正确。

### 本地隔离库（只示范 CLI 入口）

必须先确认环境变量指向已授权的隔离副本。以下采用绝对项目路径与 api cwd，避免 Flask 在根目录无法发现 app。

```sh
cd /Users/liuxingwang/go/src/dify-plus/api
export FLASK_APP=app.py
uv run --project /Users/liuxingwang/go/src/dify-plus/api flask db current
uv run --project /Users/liuxingwang/go/src/dify-plus/api flask extend_db current
uv run --project /Users/liuxingwang/go/src/dify-plus/api flask db upgrade
# 上一命令非 0 或 head/数据异常时停止，不继续扩展链。
uv run --project /Users/liuxingwang/go/src/dify-plus/api flask extend_db upgrade
uv run --project /Users/liuxingwang/go/src/dify-plus/api flask db current
uv run --project /Users/liuxingwang/go/src/dify-plus/api flask extend_db current
```

### 容器迁移（V03 演练前实例化，R02 定稿）

以下为 Bash 命令模板。`DIFY_RELEASE_OVERRIDE` 必须是已演练、固定镜像 digest 且配置迁移关闭的 override；环境文件与 project 均由环境清单填入。服务名 `api` 须以合并后的 `config --services` 确认。按行记录退出码，不把整段无检查地粘贴执行。

```bash
: "${DIFY_COMPOSE_FILE:?需指定综合Compose绝对路径}"
: "${DIFY_RELEASE_OVERRIDE:?需指定已验证的镜像与发布配置}"
: "${DIFY_ENV_FILE:?需指定受控环境文件}"
: "${DIFY_PROJECT:?需指定已核对的project名称}"
dc=(docker compose --env-file "$DIFY_ENV_FILE" -p "$DIFY_PROJECT" -f "$DIFY_COMPOSE_FILE" -f "$DIFY_RELEASE_OVERRIDE")
"${dc[@]}" config --quiet
# 必需DB/Redis已启动，其余写入者保持停止；--no-deps避免迁移启动业务依赖。
"${dc[@]}" run --rm --no-deps -e MODE=job -e MIGRATION_ENABLED=false api db current
"${dc[@]}" run --rm --no-deps -e MODE=job -e MIGRATION_ENABLED=false api extend_db current
"${dc[@]}" run --rm --no-deps -e MODE=job -e MIGRATION_ENABLED=false api db upgrade
# 手动确认退出码、主链head和数据审计通过，再执行下一行。
"${dc[@]}" run --rm --no-deps -e MODE=job -e MIGRATION_ENABLED=false api extend_db upgrade
"${dc[@]}" run --rm --no-deps -e MODE=job -e MIGRATION_ENABLED=false api db current
"${dc[@]}" run --rm --no-deps -e MODE=job -e MIGRATION_ENABLED=false api extend_db current
```

目标结果：`alembic_version=c3f1a9b2e6d4`，`alembic_version_extend=020_workflow_run_account`，并完成表/索引/引用/业务读回。空库需全链；已有库按其实际 head 推进。`MODE=job` 的目标入口执行 `flask "$@"`；这里关闭自动迁移，显式分步执行两条链，避免重复主链。任何其他迁移执行者必须停止。

不追加 `flask data-migrate legacy-model-types`，该目标已包含相应迁移。历史 `backfill-plugin-auto-upgrade` 仅在源库事实证明从未完成且目标仍要求时制定补救步骤；不能删除历史要求，也不盲目复跑。

## 6. 放行与恢复

| 失败阶段          | 立即动作                                     | 恢复路线                                                                                                  |
| ----------------- | -------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| merge 未提交      | 保存本轮人工处理补丁与状态，不覆盖原用户文件 | 集成负责人核实可安全 abort；否则保留现场处理，禁止 hard reset                                             |
| 静态/定向测试     | 阻断候选发布，回到负责 M 节点                | 修复后更新 commit，重新验证受影响后继                                                                     |
| 副本向量/DB 迁移  | 停止推进、保存日志与当前 head                | 重新恢复副本快照再演练；禁止根据 exit code 盲目重跑                                                       |
| 生产向量失败      | 保持入口关闭，停所有写入者                   | 恢复失败站的配对快照；验证旧完整环境后恢复服务                                                            |
| 生产 DB 迁移失败  | 查询已提交 head、残留索引/表，禁止启动业务   | 按已演练整套快照恢复；不得用 downgrade 代替数据恢复                                                       |
| 新版业务/观察失败 | 封入口、停调度与全部写入者，记录新增写入范围 | 恢复同一旧静止点 DB/vector/Redis/files/plugin/Agent+旧 digest/config；按队列处置单恢复任务；签收 RPO 损失 |

D04 先启目标 API/Web 与必要受控 worker/队列，beat/trigger 保持暂停；再在受控入口做 [验证矩阵](verification-matrix.md) 生产 smoke：登录/权限、模型调用和真实扣费、工作流、知识库检索、文件、插件、关键 worker 队列。通过后按部署单开放入口、其余任务消费和调度并观察约定窗口。失败后的恢复步骤也需验证业务，不能仅看容器 healthy。

R02 的 `fork-merged-1.17.1` 标记已验证源码；D04 的发布记录标记生产实际镜像与时间。当前 OpenSpec 不自动归档，后续按用户完成范围另行处理。旧快照与镜像清理由保留策略单独执行。

## 本轮全新安装自测：工作流 WebSocket 入口

2026-09-30 用户指定先验全新安装，历史数据兼容后续独立验证。本地缩减自测栈曾将浏览器 Socket.IO 指向普通 gevent API，未启动专用 `api_websocket`，导致工作流同步遮罩持续拦截点击。REST健康不能替代工作流同步验收。

本测试项目已通过最后一层 override `/private/tmp/difyplus-performance-20260930/override-performance.yaml` 将单API worker设为 `geventwebsocket.gunicorn.workers.GeventWebSocketWorker`，连接数100，仅重建API、保留数据库/存储/插件。再次启动这个测试项目须保留该override；省略它会恢复原故障配置。真实握手101、遮罩消失、关闭面板和选择LLM节点均通过；证据见 `evidence/V05/28.5-workflow-interaction-recovery.md`。

标准部署继续使用现有 `collaboration` profile、专用 `api_websocket` 与 nginx Socket.IO 路由；本单API临时修复不是标准生产拓扑验收。全新部署至少核对浏览器实际Socket.IO目标、worker类、101握手与编辑器真实点击，不能仅测端口或HTTP200。
