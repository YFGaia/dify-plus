# 环境升级与恢复操作包模板

> 本文保留生产环境操作模板，生产环境化定稿及执行未完成。本轮55owned源码修复、独立审查、完整标准Dockerfile HEAD-lock镜像和隔离本地无外部配置required检查已通过。原23000中心曾真实显示/打开缺stats的已安装发布应用，最新HEAD-lock镜像三服务已更新healthy；最新浏览器刷新因desktop锁屏尚未验，不能称latestUI通过。当前最新组件/克隆绑定与代码留档结果按N06/R02持久记录签收。外部11required条件（SSO、非零模型账务、embedding、generatedAgent）、历史旧库升级/旧版本一致恢复和生产门槛分别保留，见逐项machineledger及external-conditions。

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


## 当前本地交付操作包草案（N02–N06，尚未签收）

1. 唯一集成负责人将当前tracked源码及六项应用中心owned文件（含新tests/handler）归档，保留并记录现有`api/uv.lock`；记录base HEAD、全部归档hash、生成物/锁文件hash与构建平台。不要用旧commit archive遗漏当前修复。完整archive身份和新的image ID/digest由N02/N03实测填写，本草案不声称已有。
2. 从该archive构建完整fork API/Web镜像。记录真实build日志和image ID/platform；正式运行移除source-overlay替代层，比较容器实际关键文件hash。可复用未变Web产物的前提是完整产物/配置hash可追。私有plugin供货不足单列，不把临时上游image替代记录为fork registry通过。
3. 执行者读现有项目/volumes/networks/override的非敏感元数据，列出完整实际组合和角色；保留测试库、模型凭据、插件、文件、旧holding任务。只更新这个隔离项目的已授权服务。重建期间暂停UI写入，重新确认Socket.IO目标/worker/101与真实点击、必需worker队列。
4. 首验现有缺stats且published/installed应用无需DB回填即中心可见/打开，再按六批次完成全部当前适用业务/角色/计费/知识库。仅单测/HTTP200/健康/price0都不能代替业务。每步追加脱敏结果和回读，失败回到对应限定修复；不清理用户其他资源。
5. 在独立新安装环境演练同一一致恢复点的DB/vector/files/plugin/Redis恢复，验证新增样本及实际页面/检索/账目/队列，记录RPO/RTO。历史旧版恢复另原V06/O04，未因本地恢复通过而关闭。
6. 收束时完整填写本地实际compose组合、精确镜像及hash、启动/停写/恢复命令、数据卷清单、已通过/blocked/deferred逐项表；无占位符且重启/恢复真实通过才由N06签收。后续生产放行仍走D00–D04和原runbook生产环境事实。

本地批次具体Input/Expected/Readback及真实样本见`evidence/local-acceptance-batches-2026-09-30.md`。


### Worker交付闭环补充

标准fork主worker的默认队列含workflow_based_app_execution，worker-gaia默认extend_high/extend_low；完整new-install验收不能只启动28.7的workflow-only缩减消费者。发布资产queue源码没有已证明丢失；本地交付包应包含必要worker-gaia或等价明确消费者的可复用override，并记录actual active_queues/registered tasks和真实非零计费task投递/消费。仍保留holding旧debug请求，禁止自动重放。新文档/工具/邮件等批次所需消费者也逐项按实际task queue验证。


### 用户原23000入口交付（新栈验收之后）

已存在本地服务更新授权，不能仅以localhost:23010通过宣称用户原问题解决。N01受影响发布/安装/缺stats实际API及browser与N07最新稳定source/image通过后，先由运行执行者记录原`difyplus-upstream1171-quota-selftest-20260930`实际Compose完整组合、旧镜像ID/config和数据库可恢复备份；核对所操作项目而不是凭名称猜测。

只更新原项目API和必要标准worker-gaia消费者到已验证的精确完整image；禁止重建/替换用户数据库卷、重配模型/App、重录凭据或清理plugin/files/held debug队列。原`/private/tmp/difyplus-performance-20260930/override-performance.yaml`仍是最后层WS覆盖（或已真实验证的等价标准专用WS替代）；丢失它会重现工作流遮罩。业务worker继承原项目正确DB/Redis/SECRET_KEY/存储且不消费holding队列，先核对队列覆盖与旧任务处置再启动。

更新后真实打开`http://127.0.0.1:23000/explore/apps-center-extend`，此前遗漏的已安装已发布App应显示且可打开；同时确认Socket.IO101/真实点击、用户App和模型配置保持、实际image/hash对应最终candidate，保留回退旧API image/config和数据备份恢复路线。失败保留现场并按已保留锚点恢复，不用新23010结果替代旧入口验收。具体现场命令由执行者从实际完整Compose组合形成，不在此模板猜测/写入密钥。

原23000现场与精确Compose/备份/三服务更新/回退操作包已准备：`evidence/N06/original-23000-inventory.json`、`evidence/N06/original-23000-update-runbook.md`。父追加授权已有workflow worker与API、必要gaia一起使用同一最终image；私有env/WS保持，先新栈受影响N01与N07最新候选验收后执行，不等待无凭据KB/非零计费全矩阵。该准备阶段历史现已完成：原23000实际备份与N08 0567三服务更新、浏览器中心卡片显示/打开已通过；同一静止点隔离PG/plugin/MySQL/storage双clone恢复与API选定读回已通过，持久结果见N06两份runtime JSON。当前N09/N10最新445d三服务已同镜像启动healthy，双heads/zero非terminal/targetqueues-unacked0已读回；最新center组件及clone绑定按最终N06proof签收。此前pre-updateinspect超时且无数值snapshot，不把post-update0当先前静止证明。最新浏览器锁屏未刷新，历史N08UI保留但不冒充latestUI。

当前API正式本地releasecandidate采用仓库原 `api/Dockerfile` 与原HEAD锁完成的完整image `difyplus-acceptance-api:953cf1c-n10-headlock-3cf5bfa6` / `sha256:445d59dbbab0f377a63c0b6a731ca25b00c35a2ba4019f1846881ba247a65172`。context SHA为 `3cf5bfa660806d4306dae0558b7a9b8a6797898a449274c60180e901f6ed02f9`；55ownedsource/test加HEAD原锁，47镜像path（46production源码+锁）全hash一致，9测试按Dockerignore不入镜像。实际449包版本、38provider distributions、32entrypointloads、8traceimports、QdrantCRUD清理均通过。dirty用户锁candidate另保留manifest并版本映射完全一致，seed仅历史辅助。隔离URL localhost，23010/25442/25443绑定`[::1]`，六业务services同immutableimage启动、N09正负scope/HumanInput/真实Socket/合法AgentJWE及配置文件actual检查已通过。

当前完整交付门槛：N09上下文UUID兼容与N10默认Provider安装独立源码review → 完整锁/context/canonical原Dockerfile镜像 → 同资源正向/foreign scope、provider registry/import与无embedding的隔离Qdrant CRUD → 原23000限定三服务最新镜像及卡片/受影响选定读回。既有备份/一致恢复证据继续有效（schema/data未变）；无新风险不重复整套dump。外部SSO/真实非零模型收费/高质量embedding、旧库破坏性迁移和旧版整套恢复、生产授权/观察分别保留原门槛，不由本地keyword或0token workflow替代。

最终源码与镜像绑定：先验证N09/N10 dirty候选完整defaultgroups及受影响业务；再按HEAD原锁+55owned source生成独立canonical完整镜像，实际defaultgroups安装/38distribution/32vectorentrypoint/8traceimport和包版本对照。保留用户既有dirtylock未stage，原R02显式owned提交与不覆盖留档tag仅在验证后执行。最终releaseimage应绑定实际新sourcecommit及精确lock/context，COMMIT_SHA=base953只表示归档基线，不能标作最终提交；既有镜像通过55sourcehash＋HEAD锁映射最终ownedcodecommit；只补label无需重构建，不将953称最终commit。未变范围按源码/hash/包版本证据复用，实际R02结果/日志可另文档提交，tag指前一个codecommit不force重指。

实际代码留档：source implementation `ee72b000d076449dc0cbd5ccb7fb92c4fb7b017c`，补齐唯一旧M08结构log后的clean code-ready `e91c4d89fc6090ebd339acaf9be5ce853c29f544`；annotated `fork-merged-1.17.1` 指后者不覆盖。两commit55产品source相同，HEAD842锁匹配正式445d镜像，clean普通git archive引用/OpenSpecstrict通过。实际结果与编排检查边界见 `evidence/R02/result.json` 和 `execution.log`；这些事实docs提交不移动tag，不将code-ready标为最新UI/外部/旧版恢复/生产完成。
