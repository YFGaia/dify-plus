# Dify-Plus 1.16.0 基线 → upstream 1.17.1：部署、数据库与运维专项规划

> 规划稿；没有实施、启动容器、访问生产、修改业务仓库。源：fork `1c3368ed1584c4e9b6387a28334552d10c946ab4`，上游旧基点 `5c6372d2f76d240265b92fd27c16bc772ffcb107`，目标 tag `1.17.1` 对象 `8387590ace4a094de812b7847fc6a4c3a27cd52b`。目标源码通过临时对象库只读比较。生产实际部署版本、拓扑与数据量均未核实。下文的版本是**仓库声明**，不能冒充线上现状。

## 1. 首要结论与停机边界

1. fork 直接维护 `docker/docker-compose.dify-plus.yaml`，上游生成器只生成 `docker-compose.yaml`；不能把上游 Compose 整份覆盖。保留 fork 的 `worker-gaia`、`worker-dataset`、`sandbox-full`、私有镜像、系统配置、入口与挂载，逐项移植目标版本契约。merge-tree 已指出 `docker/.env.example`、`docker/envs/core-services/web.env.example`、`docker/nginx/conf.d/default.conf.template` 三个部署相关冲突；Compose 虽未文本冲突，仍须人工语义对齐。
2. **数据库主链与 fork 扩展链分开**。当前共享环境默认 `MIGRATION_ENABLED=true`，API/worker/beat/worker-gaia/worker-dataset/websocket 的 API 镜像入口会先调用 `flask upgrade-db`。该命令在 1.17.1 的 `api/commands/system.py` 只执行 `flask_migrate.upgrade()`，不执行 `extend_db`。正式升级应在停写和备份后以**单一迁移执行者**推进主链，再显式推进扩展链，检查两张版本表，随后以 `MIGRATION_ENABLED=false` 启动所有 API 镜像服务。不要把多容器自动迁移的“skipped”日志当作已成功；Redis 锁只协调并发主链，不代替扩展链或版本核验。
3. 本轮上游新增 17 个 Alembic 文件，主链从 `7a1c2d9e4b60` 至 `c3f1a9b2e6d4`；fork 当前扩展 head 是 `019_webapp_auth_switch`，不是旧 1.16.0 升级文档所写的 `018_drop_recommended_cats`。正式库须先查自己的两张版本表。迁移 `f6e4c5686857` 删除 `agent_runtime_sessions`，`89919253ca7a` 删除 `agent_drive_files` 并改写 JSON，`5578e028b2f2` 合并/删除重复模型配置和凭据且 `downgrade()` 是空实现。回滚必须用升级前的一致性备份；不能依赖 `flask db downgrade`。
4. fork Compose 声明 `weaviate:1.19.0`，当前 Python 客户端是 `weaviate-client==4.20.5`，上游 1.17.1 声明数据库 `1.39.2`、客户端 `4.22.0`。Weaviate 官方说 Python v4 客户端要求服务端至少 `1.27.0`；如果线上真的仍是 1.19，现有声明已不兼容，且从 1.19 到 1.39.2 不能在同一原数据目录直接跳版本。先核实线上 `VECTOR_STORE`、真实服务端版本和数据所在位置；确认为 Weaviate 1.19 时，另立逐 minor、每站最新 patch、含独立备份的向量库升级窗口，再接 Dify API 1.17.1。参见 [Weaviate 迁移说明](https://docs.weaviate.io/deploy/migration) 与 [Python 客户端兼容说明](https://docs.weaviate.io/weaviate/client-libraries/python)。
5. 上游 `.env.example` 已把 Agent v2 样例置 `true`，但目标 web 入口和 `web/env.ts` 的缺省仍是 `false`。fork 当前显式设 `NEXT_PUBLIC_ENABLE_AGENT_V2=false`。移植时继续保留 fork 的显式业务开关；服务容器在 Compose 中存在，不代表 Agent 功能已启用。1.17.1 Agent backend/local sandbox 的认证、运行时、网络隔离已变化，必须作为一个闭环验收。

## 2. 配置和交付变化清单

| 类别 | fork 当前声明 | 1.17.1 上游声明 | 合并决策与验收 |
| --- | --- | --- | --- |
| API/Web | 私有仓 `dify-plus-api/web:1.16.0`，含五类 API 镜像服务 | 官方 `dify-api/web:1.17.1` | 构建 fork 代码的 1.17.1 版本并推私有仓；所有 API 同一不可变 digest，Web 对应同一提交。镜像名称、架构、digest 入发布清单；不得以官方 API/Web 镜像替代 fork 代码。 |
| Python/Node/前端构建 | API `python:3.12-slim-bookworm`、uv `0.8.9`、Node 22.21.0 包；Web Node 22.22.1 | Python 3.12、uv 0.8.9 没变；API Node 包 24.20.0，Web Node 24.20.0；Web `pnpm install --frozen-lockfile --ignore-scripts`；NLTK 下载目标改 `punkt_tab` 与 `averaged_perceptron_tagger_eng` | 移植 Dockerfile/锁文件，构建后检查 Node/Python/uv、NLTK、运行用户与 API/Web 启动；尤其 API 的 JS 执行和 Web 构建。上游删 `ENV EDITION=SELF_HOSTED`，应检查 fork 的 edition 逻辑是否需要 `DEPLOYMENT_EDITION=COMMUNITY`，不能盲删旧业务语义。 |
| Plugin/Sandbox | 私有 plugin daemon `0.6.3-local`、sandbox `0.2.15`、fork `sandbox-full:1.2.2` | 官方 plugin daemon `0.6.10-local`、sandbox `0.2.15` | plugin daemon 须确认私有镜像供应、存储兼容及与 API 协议；标准 sandbox 未变，`sandbox-full` 为 fork 保留并做代码执行验证。保留 plugin 持久目录。 |
| Agent | backend/local sandbox `1.16.0`；`DIFY_AGENT_RUN_RETENTION_SECONDS` 默认 259200；`DIFY_AGENT_SHELLCTL_*`；backend 默认密钥留空 | 两镜像 `1.17.1`，API 增 `AGENT_BACKEND_API_TOKEN` 与 backend 的 `DIFY_AGENT_API_TOKEN` 匹配；runtime backend `local`、`LOCAL_SANDBOX_*`、`SANDBOX_FILES_BASE_URL`；新增 `agent_ssrf_proxy`、两个 internal network、local sandbox home/workspace 命名卷；Agent 样例 retention 7200 且 Compose 不强传该变量 | API/backend/token 双端同一强随机值，保留 fork 强密钥策略；显式决定 259200→7200 的数据保留政策，不能静默改变。保留 `NEXT_PUBLIC_ENABLE_AGENT_V2=false`，如要启用再独立验证 sandbox、文件、工具、暂停续跑。旧 `SHELLCTL_*` 兼容回退仅作过渡。鉴于默认服务无 profile，禁用功能时仍须保证容器可启动或明确服务 profile/依赖的修改。 |
| Agent 网络安全 | `local_sandbox` 直接在 default network，通用 `ssrf_proxy` | local sandbox 离开 default，走 `agent_sandbox_network` 控制链与 `local_sandbox_proxy_network` 出网链，专用代理仅允许 `agent_backend /agent-stub/*`、API `/files/*` 的私网目标；公网经代理 | 移植整个网络拓扑、两份 Squid 模板及入口脚本，检查 API 文件上传下载与禁止任意内网 URL。上游注释说明 sandbox 经 shellctl 到 backend 的通道仍是限制，勿声称沙箱完全隔离。 |
| 通用 SSRF | 两个 `SSRF_PROXY_ALLOW_PRIVATE_*` 已在 fork Compose 和 `.env.example` | 新 `squid-common.conf.template`，共享 ACL；上游样例加入 CIDR 白名单说明 | 现有白名单按真实业务核定，默认留空；不能为了让 Agent 内部文件调用通行而扩大整个通用 SSRF 私网白名单。新增模板须和 fork 私有 Squid 镜像能力一起验证。 |
| Weaviate | fork 镜像 `...weaviate:1.19.0`，`./volumes/weaviate`，默认 `VECTOR_STORE=weaviate`；客户端 4.20.5 | `cr.weaviate.io/...:1.39.2`，客户端 4.22.0 | **分支决策**：线上若用其他向量库，保留其数据并只做对应适配；若用 Weaviate，先确认版本和库健康，再按官方路径逐 minor 升级、验证 schema/对象数/抽样向量检索和 gRPC；旧版小于 1.27 时需要特别验证 v4 客户端切换所需的服务端版本与集合 schema/访问方式。不能把镜像 tag 修改视作数据迁移完成。 |
| Web/Ingress | fork nginx 模板和 `WORKFLOW_GENERATION_TIMEOUT_MS`、Agent v2 false | nginx conf 模板变更，Web 新 `TURNSTILE_SITE_KEY`，移除旧工作流生成超时项 | 保留 fork system-manage、登录/回调、静态与 websocket 路由；按实际是否使用 Turnstile 设置两端配置。替换容器后按历史演练重启 nginx 并做入口 200/登录回跳/WS 验证。 |
| Edition/Redis | 旧 `EDITION` 与现有 Redis/Agent 设置 | worker 队列判断从 `EDITION=CLOUD` 改为 `DEPLOYMENT_EDITION=CLOUD`；Agent 旧 redis prefix/env 从上游 Compose 移除 | 审查 fork edition 配置和队列覆盖值，确认 worker/worker-gaia/worker-dataset 所消费队列覆盖全部生产任务；保持 Redis 数据库、队列、持久化和 key prefix 策略。 |
| 构建 CI | `.github/workflows/build-push.yml` 在非 `langgenius/dify` 仓库只做 `push:false` 的 fork build validate；`docker-build.yml` PR 构建亦 `push:false` | 官方同目录有更新 | 不能把 CI 绿色当作私有镜像已推送。另出发布流水线或有审计的手工构建推送，记录源码 SHA、平台、镜像 digest、SBOM/签名如既有流程要求；在目标主机先验证拉取。 |

## 3. 数据库路径与不可逆点

### 3.1 存量库升级

1. 先记录线上 `DB_TYPE`、版本、`alembic_version`、`alembic_version_extend`、库大小、关键表行数。目标主链从 1.16.0 head `7a1c2d9e4b60` 经 17 个文件到 `c3f1a9b2e6d4`；若线上不是此起点，按实际链诊断，不执行盲目 stamp。fork 扩展链目标 `019_webapp_auth_switch`，可能线上仍在 018，需要实际验证。
2. 迁移前对 `agent_runtime_sessions`、`agent_drive_files`、`provider_model_credentials`、`provider_models`、`tenant_default_models`、`provider_model_settings`、`load_balancing_model_configs` 记录行数、业务键重复情况、加密凭据可解密样本；检查 1.17.1 模型类型迁移的赢家规则（`updated_at` 最新、同时间 `id` 最大）对存量是否符合业务预期。若出现同业务键内容冲突，停在演练门槛由业务负责人选择，不让 migration 隐式裁决。
3. 从真实脱敏或隔离副本演练完整主链。`3c9f8e2a1d7b` 在 PostgreSQL 使用 `CREATE INDEX CONCURRENTLY`，其 Alembic `autocommit_block` 意味整个序列不能假设是一个可回滚事务；记录每步耗时、锁等待、空间增长，失败后先看两张版本表和残留对象，再决定从同一快照重新演练。`89919253ca7a` 会遍历并改写 Agent JSON，`5578e028b2f2` 会清重，数据量影响窗口时长。
4. 正式变更窗口：停入口/调度/worker/所有写入者并确认业务静止 → 对 PostgreSQL/MySQL（按实情）、Redis、向量库、对象/本地文件、plugin daemon 存储作**同一静止点**备份 → 验证备份能恢复 → 仅保留所需 DB/Redis 等中间件 → 以目标 fork API 镜像执行单一主迁移 → 执行 fork 扩展迁移 → 查询版本表、表/索引、行数差异与模型凭据抽样 → 启动服务。`pg_dump` 是库内 MVCC 一致快照，但跨数据库、向量库、文件、Redis 的一致性由停写/快照顺序保证。
5. 推荐两个明确的执行入口：主链用单次 `MODE=migration, MIGRATION_ENABLED=true` 的目标 API 镜像（入口会运行 `flask upgrade-db` 后退出）；扩展链用单次 `MODE=job, MIGRATION_ENABLED=false`，参数 `extend_db upgrade`（入口执行 `flask extend_db upgrade`）。也可在同一隔离 job 顺序执行 `flask db upgrade`、`flask extend_db upgrade`，但必须记录两个命令的退出码与版本表。不可在 `docker compose up -d` 时让多个业务容器抢跑迁移。
6. PostgreSQL 校验示意：`SELECT version_num FROM alembic_version;` 期望 `c3f1a9b2e6d4`；`SELECT version_num FROM alembic_version_extend;` 期望 `019_webapp_auth_switch`（以合并后 fork 实际 head 复核）。`f6e4c5686857` 的旧 Agent session 数据不会自动迁到新 workspace/binding；若线上已有活跃 Agent v2 session，升级前必须单独定停用/导出/清理策略并演练恢复。

### 3.2 全新库

空库以目标 API 镜像跑上游主链到 `c3f1a9b2e6d4`，再跑 fork 扩展链到 `019_webapp_auth_switch`；校验两张版本表、fork 所需表/列、初始管理员创建和登录。全新库仅证明安装路径，不能代替存量库的数据改写、耗时、唯一约束与回滚演练。

## 4. Weaviate 单独决策树

**先核实**生产 `VECTOR_STORE`、`WEAVIATE_ENDPOINT`、容器 image digest、服务端 `/v1/meta` 版本、collection/schema/对象数量、数据卷路径、单/多节点、备份后端与恢复证据。仓库里 `docker/volumes/weaviate/migration1.19.*` 文件只证明本地目录曾有 1.19 相关状态，不证明线上版本。

- 若线上不使用 Weaviate：不要对无关向量库执行 Weaviate 升级；保留原向量库和 Dify 的 `VECTOR_STORE`、凭据与网络配置，做对应检索写入验证。
- 若线上 Weaviate ≥1.27 且低于 1.39.2：按官方迁移文档逐 minor 使用当时最新 patch，经过每一站做 ready、schema、对象数、抽样向量/关键词检索、备份可恢复检查；再上 Dify API 1.17.1。客户端 4.22.0 在 1.39.x 官方兼容表对应的是 4.23.x，虽上游锁定 4.22.0，仍要以 Dify 实际操作测试判断是否可接受，不推断绝对兼容。
- 若线上 <1.27，尤其真为 1.19：**先在专项副本上**制定 1.19→逐 minor→1.39.2 的镜像矩阵与窗口，处理官方注明的 1.23.13 前备份恢复缺陷、1.25 Raft 迁移及 1.26 后 cluster metadata 同步门槛。Docker 单节点 Raft 迁移官方称自动，但仍需查 `/cluster/statistics`；每站保留可恢复备份。不能把旧版 v4 迁移指南中的直接跨版本、在线复制 LSM 文件或某个计数接口命令照抄到生产；若原位逐站升级在副本上不能证明安全，走导出/重建/重索引路径，比较 collection schema、对象/向量数量及同一查询集合的检索结果。v4 客户端至少需 1.27，故服务端升级先于目标 Dify API 接流量；检查 gRPC 50051、批量导入与检索实际行为。需要时安排 v3→v4 使用迁移，不预设原数据 schema 必然自动转换。
- `WEAVIATE_GRPC_ENDPOINT` 以目标源码为准：目标 `docker/.env.example` 是 `grpc://weaviate:50051`。目标 `weaviate_vector.py::_init_client` 对无 scheme 值会补 `grpc://`，`grpc://` 判定为非 TLS，`grpcs://` 判定为 TLS，最终传给 `weaviate.connect_to_custom` 的是 host/port/secure 三元组；故带或不带协议都能解析，实际 TLS 与端口必须按部署网络核实。旧迁移指南示例不能替代这里的目标代码契约。
- 任一阶段数据校验失败：停止向量写入与后续版本推进，恢复该阶段前的向量快照及其配对的关系库/文件状态；不要在已被新版本打开/改写的数据目录上直接降镜像。

## 5. 可执行节点、依赖与门槛

| 节点 | 前置依赖 | 产物 / 通过门槛 |
| --- | --- | --- |
| D0 线上事实盘点 | 维护者只读权限 | 实际 Compose 文件与 project 名、运行 image digest/平台、DB/Redis/向量库类型版本与地址、数据路径/卷、对象存储、Agent v2/归档/协同开关、API 与插件镜像来源；脱敏保存，不记录明文密钥。缺项只阻塞对应生产操作，不阻塞代码规划。 |
| D1 部署代码包 | 合并代码完成 | fork Compose/env/nginx/Squid/Dockerfile/锁文件和 CI 语义对齐，`docker compose config` 可解析、无弱开发默认密钥落到生产、所有服务镜像 tag/digest 可供应、API/Web/agent/plugin 版本矩阵一致；确认 `MIGRATION_ENABLED=false` 发布值。 |
| D2 上线操作包 | D0、D1 | 经复核的停写/备份/恢复/迁移/启动命令、账号权限、时间预算、监控/业务校验 SQL、RTO/RPO、镜像 digest/回滚包、责任人与中止阈值；在隔离环境复演一次。 |
| D3 旧库与空库演练 | D1、可恢复副本 | 旧库主链+扩展链通过并记录耗时和数据变化；空库双链通过；抽检被删除/改写数据、fork 计费/登录/系统管理；恢复演练可将数据与 1.16.0 代码一起启动。 |
| D4 向量库演练 | D0 确认为 Weaviate 或其他库 | 若 Weaviate，完成实际版本至目标的逐站演练；重建知识库索引/检索/写入对照；若其他库，验证对应驱动。向量健康和检索通过才放行 D5。 |
| D5 正式升级 | D1–D4 全部通过，变更窗口 | 单一迁移入口、双版本表达到目标、所有目标镜像按 digest 启动，核心业务和监控通过；记录切换时间点，保留旧镜像与静止点备份。 |

### 部署代码包和上线操作包分开验收

部署代码包是源码中的 Compose、env 样例、Dockerfile、模板、镜像构建定义和目标应用代码，验收其**静态配置与镜像可运行性**。上线操作包是针对某个真实环境的变量清单、版本事实、备份、停写、双迁移与恢复命令、镜像 digest、数据核验和业务验收证据，验收其**可执行与可恢复性**。D1 通过不代表 D2/D3 通过，也不授权把产物直接用于生产。当前仓库 `build-push.yml` 的 fork job 只 build validate，不会推送私有镜像，发布清单必须有单独供货证据。

## 6. 演练与正式上线顺序

1. **隔离演练准备**：复制与生产版本一致的关系库、向量库、Redis（至少 Agent/队列相关键及持久数据）、`./volumes/app/storage`、plugin daemon 存储和对象存储所需前缀，使用独立 Compose project、端口、网络、密钥和存储目录。先证明 1.16.0 旧镜像能读副本；记录基线的知识库文档/对象数、应用、模型、计费样本。
2. **演练升级**：停旧业务写入，保存静止点快照；按 Weaviate 决策树完成向量库路径；构建并供给目标 fork 镜像；只启动必需的 DB/Redis 等；运行唯一主迁移 job，再运行扩展迁移 job；核验两 head 与破坏性表的预期状态；启动 1.17.1 服务且业务服务 `MIGRATION_ENABLED=false`；检查 API/Web/nginx/worker/agent/plugin/SSRF 健康和最小业务路径。
3. **演练回滚**：停止目标所有写入者（包括 beat、异步 worker、plugin daemon、Agent backend、向量写入）并封入口；恢复成对的关系库、向量库、Redis、文件/对象存储快照；切回旧 Compose/env/镜像 digest；先起中间件核对旧库版本和数据，再起旧 API/worker/Web/nginx；检查旧版登录、知识库检索、工作流、计费。只有该闭环通过，正式升级门槛才可打开。
4. **正式变更**：按 D2 的环境专属命令重复演练序列；每个迁移和启动节点记录时间、输出、版本、镜像 digest 与业务验收者。若主迁移失败，在弄清 Alembic 已提交位置之前不重跑、不启动业务容器；若扩展链失败，也不开放新服务。对失败点使用预定义恢复路线，回到静止点整套数据和旧镜像。
5. **观察与清理**：核心业务通过后按窗口观察 Celery 堆积、500/502、登录/CSRF、向量检索延迟与失败率、plugin daemon 和 Agent 错误；确认持久存储与备份保留期。旧快照删除属于单独变更，不与升级窗口绑在一起。

## 7. 最小必做验证

- **配置与供货**：Compose config、所有启用 profile 的依赖拓扑、私有镜像实际 pull/digest、镜像架构、Python/Node 版本、`MIGRATION_ENABLED=false`、强 Agent token 与两端一致性、SSRF 代理模板加载。
- **迁移**：旧库和空库各跑主链+扩展链；两版本表、fork 019 列、17 个主迁移对应表/索引；记录 Agent 表删除前行数和 model types/凭据映射后业务读回。验证恢复旧快照能让旧镜像启动。
- **Dify 业务**：API health、Console 登录 Cookie+CSRF、System Manage 三页、应用创建/发布、API key 额度、一次真实模型计费、workflow 执行、知识库新增与向量检索、上传/下载文件、插件调用、worker/beat 队列处理；若启用了 collaboration/Agent v2/归档，增加各自的真实链路。
- **Agent/SSRF**：Agent 关闭态没有业务入口；启用态用匹配 token 执行一次本地 sandbox 运行、文件传递、工具调用及续跑；代理允许规定的 `/agent-stub/*`、`/files/*`，拒绝任意内网目标，通用 HTTP Request 节点按现有白名单工作。
- **Ingress 与运维**：nginx 入口、Web 静态资源、登录回跳、WebSocket（若启用）、镜像切换后 DNS/502、Celery backlog、Redis/DB/Weaviate 磁盘与连接数，实际监控恢复到基线。

## 8. 仍待真实环境确认

`D0` 必查：目前部署到底是哪个 fork tag/commit、是否由此 Compose 起、运行 Weaviate 是否真的 1.19、是否有外部向量服务、DB 是 PostgreSQL/MySQL 何版本、两版本表当前值、已启用 Agent v2/归档/协同与否、Agent 数据量、`DIFY_AGENT_RUN_RETENTION_SECONDS` 的业务要求、当前 Redis 与文件/对象存储的持久化/备份策略、是否使用内部 HTTP 地址的 SSRF 白名单、现行私有仓推送与拉取权限、生产宿主机架构、可接受停机时间及恢复目标。这些决定正式操作参数和放行门槛；在事实核实前不写生产具体地址、密钥或声称能够无损直升。

## 源码与资料定位

- fork：`docker/docker-compose.dify-plus.yaml`、`docker/.env.example`、`api/docker/entrypoint.sh`、`api/commands/system.py`、`api/migrations_extend/versions/*`、`.github/workflows/build-push.yml`、`docs/dify-plus/升级到1.16.0说明.md`、`docs/dify-plus/1.16.0升级本地容器验证记录.md`。
- 1.17.1 tag：`docker/docker-compose-template.yaml`、`docker/docker-compose.yaml`、`docker/.env.example`、`docker/envs/core-services/dify-agent.env.example`、`docker/ssrf_proxy/*`、`api/Dockerfile`、`web/Dockerfile`、`api/docker/entrypoint.sh`、`api/commands/system.py`、`api/migrations/versions/2026_*`、`api/providers/vdb/vdb-weaviate/pyproject.toml`；对象库 `/private/tmp/dify-1171-plan.LqV0hY/objects.git`。
- 官方外部依据：[Weaviate Migration and Upgrades](https://docs.weaviate.io/deploy/migration)，[Weaviate Python client compatibility](https://docs.weaviate.io/weaviate/client-libraries/python)。
