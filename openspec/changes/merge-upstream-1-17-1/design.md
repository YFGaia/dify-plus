# Design: upstream 1.17.1 合并执行图

## Context

动机与范围见 [proposal.md](proposal.md)。本设计以 2026-09-28 的源码快照为证据，生产运行状态尚未读取。三方比较的精确 SHA、8,120 条上游文件变动、500 条 fork 文件变动、91 个预测冲突记录于 [source-inventory.json](research/source-inventory.json)。统计使用 50% rename detection 与 renameLimit=10000；预测冲突来自临时对象库的 merge-tree，不是工作区 merge 结果。

当前 fork HEAD 比 `fork-merged-1.16.0` 多四次提交；必须保留发布调整、WebApp 每应用认证开关、匿名上下文修复和 Docker 排除 .venv。现有 P4/P6 为未完成提案，其拟议能力不属于当前基线。

## Goals / Non-Goals

**Goals:** 形成可按节点实施、可追踪输入与证据、可阻断故障传播的合并方案；保持现有企业功能并保留 1.17.1 新权限、契约、数据模型；交付独立的源码合并包和环境上线包。

**Non-Goals:** 本阶段不执行源码合并、建分支/工作树、迁移或发布；本轮升级不顺带实施 P4 计费体系改造、P6 管理 UI 标准化、管理权限扩张或匿名计费产品规则变更。原有风险须列明，不能记作验收通过。

## Decisions

### D1. 固定 tag，采用可追溯的正式 merge

目标锁定 `1.17.1` / `8387590ace4a094de812b7847fc6a4c3a27cd52b`，不随 `main` 漂移。执行前重新确认 HEAD 与 tag；若变化，重算差异与所有权。M00 在已授权分支执行正式 merge，逐项核对实际冲突及 owner，冲突结果先以 upstream 版本形成双父 merge 基线提交；上游删除的文件维持删除。原 fork 父提交与 A02 台账保留恢复依据，M01–M07 再按 owner 逐节点适配并各自提交，M08 形成集成候选源码提交。M00 基线未经适配与验证，不可部署或标记升级完成。Git 在 `MERGE_HEAD` 存在时拒绝部分路径提交，提交边界依据见 [commit-cadence.md](research/commit-cadence.md)。不通过额外分支、工作树或多头 cherry-pick 绕开此限制。

### D2. 中央 DAG 作为任务状态事实源

[execution-graph.json](execution-graph.json) 定义节点、依赖、授权、文件范围、资源锁、命令、证据和失败去向；[execution-graph.md](execution-graph.md) 为同一图的可读投影。专项分析中的 B/F/D 编号只是研究切分，以中央图 A/M/V/R/D 编号为准。每个节点状态从 pending → ready → running → passed/failed/blocked；有条件节点允许有证据的 skipped。只有前置全部通过或合法跳过、授权满足且资源锁空闲才可开始。失败阻断所有后继，不能跳过失败的验收。

证据绑定源码提交或树摘要、锁文件、镜像 digest、环境配置摘要、数据库起始/终止 head 和时间。上游输入或对应源文件变化使受影响的验收失效。实施者不得以“已有报告”“命令已列出”更新为 passed。同一文件一个 owner，Git 索引/提交由集成负责人独占；M01–M07 每节点的代码、配套测试、证据和状态各自形成提交，提交前按 owner 路径精确暂存。证据可记录提交前 HEAD/树与提交后校验，不能把提交自身 SHA 写进该提交。冲突清单的 owner 是初始分配，新文件须登记后才能并行编辑。机器可读图是工作说明，不是可以自动获得权限的执行器。

### D3. 先稳定前后端共享契约，再迁移宿主

共同契约节点 A04 固定下列决定：

| 契约 | 本轮确定的行为 | 目标挂点 |
|---|---|---|
| 登录配置 | 保留 bootstrap JWT、IP 绑定与 Header/Cookie 兼容；预登录只交付允许公开的 fork 配置，完整 license 受登录保护 | 新 Console browser/server transport、contract-loader、accounts admission |
| system-features | 采用上游公开快照语义，与 fork 双阶段配置合成；不把旧 `{ping:true}` 当完整配置，不通过兼容回退泄露 license | `packages/contracts/console.ts`、`web/features/system-features/*`、后端 feature service |
| WebApp | built-in `NULL/true` 需 Console 登录，false 允许匿名；缺失/非法 app_code 和配置读取失败拒绝匿名放行；环境 WebApp 遵循上游 environment passport | AppSiteService、WebPassportService、新 access-point 卡片、address/auth 服务 |
| Site 写入 | fork 字段从上游 payload 转换隔离；站点和扩展配置一致提交、成功后失效缓存；失败不出现 UI 假成功 | 新 service/repository 事务；禁止额外字段误入上游 Changes 类型 |
| Workspace | `admin_extend/tenant_extend` 在新 summary 契约与实际响应共同提供；现有角色权限保持 | summary normalizer 与 workspace API；模板同步状态从实际列表契约取值 |
| API Key | 保留现有 app key 额度；dataset scope 保留上游绑定、租户隔离和遮罩，已有可用额度能力才显示；environment 未有 fork 计费契约时隐藏/禁用额度输入 | 新统一 api-key modal/table、后端 RBAC 与额度联表；无绑定 dataset key 保持租户全库语义 |
| 账号建档 | 每个实际新账号同事务创建一次额度记录，已有账号不重置；覆盖 setup/邮箱/OAuth/邀请 | 共用账号创建边界，旧入口消除重复写入 |
| 匿名计费 | 以当前 fork 行为作回归对照，显式记录已知付款人/限额缺口，另立修复；不擅自选择应用 owner 作为付款人 | 风险台账与未来独立 change |

重建已删除旧 controller、旧 Console client 或旧卡片会形成双源并绕过上游账户锁/权限，因此采用新宿主承接 fork 行为。实现阶段如新证据要求改变上述行为，应先修改契约与受影响图节点。

### D4. 后端分为基础契约、身份链和计费链

M02 先处理 service/admission/session、模型导出、workspace summary。M03 负责账号、OAuth、WebApp site/登录。OAuth2/Casdoor 必须注册到新 gateway；适配 dict token/id_token 和获认可的 token fallback，保留 state 校验、同源回跳及上游账户锁。M04 负责十项计费/记忆挂点，逐一证明调用仍到达；所有被装饰 endpoint 的参数注入都要审计，包括无文本冲突文件。异步任务提交前物化 account/token ID，避免 worker 读取失效请求上下文。上游 Cloud quota 和 fork 个人额度保持分离。

P4 的扣费幂等、并发读改写与匿名付款人缺陷保持可见。验收要求“不新增重复派发/丢失归因”，不能把尚未实现的 exactly-once 当作已有保证。若基线缺陷影响目标环境业务接受，由负责人决定是否开启独立修复；未获决定不宣称生产可用。

### D5. 前端先 transport，再 UI 和生成物

M05 迁移 `web/service/console/*` 与 packages contracts 聚合；fork 自有 contract segment 与 generated 产物隔离。浏览器 Header 路径、服务端 SSR 可选读取/形状守卫、query cache 边界同时完成。M06 再迁移 built-in WebApp 开关、环境地址、应用中心、模板同步、个人额度与统一 API key modal、system-manage 保活。i18n 补 lo-LA 后共 24 份 extend.json。UI 文案区分 WebApp 启用开关与访问认证开关。

Node 24.20.0、pnpm 12.3.4、Python 3.12 与目标依赖一致；保留钉钉/pypinyin 等 fork 依赖。M01 负责初始锁文件和依赖声明，各 owner 提交依赖需求；M08 开始前 M01 将锁文件写入权移交集成负责人，由 M08 按最终 manifest 统一生成，禁止并发改锁文件。最终 frozen install、Next 和 Vinext 双构建不可用开发页面代替。

### D6. 双迁移单一执行者，数据恢复使用匹配快照

完整跨版本增量是 17 个迁移，不是 release notes 只列出的最近 3 个。目标主链 `c3f1a9b2e6d4`，扩展链 `019_webapp_auth_switch`。仅依赖多容器 `MIGRATION_ENABLED=true` 不会执行 extend 链。由一名执行者先主链再扩展链，业务容器恢复时全部关闭自动迁移。不额外运行已由目标迁移覆盖的 legacy-model-types 手工命令。历史 plugin auto-upgrade backfill 按源库版本与已完成证据判断，不把 1.15 历史序列机械重跑。

Agent runtime/drive 表删除、JSON 删除、preset outputs 清理和模型凭据去重均审计前后数据；PostgreSQL 并发索引带 autocommit，不能假设整个升级可事务回滚。快照须覆盖关系库、向量库、Redis/任务状态、文件/对象、插件存储、旧镜像/config 和解密所需密钥的受控引用。回滚先停所有写入者，再恢复同一静止点数据；新版本期间的数据损失范围按 RPO 明确，绝不承诺镜像回切即可无损。

### D7. 向量库与发布参数按实际环境实例化

仓库 Weaviate 1.19.0 不是线上事实。确认使用 Weaviate 才进入升级分支；非 Weaviate 走对应驱动检索验证。Weaviate 按实际版本逐 minor、每站验证，<1.27 在副本专项演练，必要时导出重建；不提供未经演练的生产跨版本一条命令。`grpc://`/`grpcs://` 与端口按目标代码解析和实际 TLS 配置确定。静态声明对齐不等于数据升级完成。

保留 fork Agent 功能显式关闭与 259200 秒保留策略，除非环境负责人另行选择；迁移目标 Agent token、网络/SSRF 拓扑和持久卷，启用环境必须验全链。私有 CI 仅 build validate 的结果不算供货，发布单必须有镜像推送和目标主机可拉取证据。

## Risks / Trade-offs

| 风险 | 控制和阻断点 |
|---|---|
| 91 个文本冲突掩盖自动合并断链 | M08 按十挂点与新宿主做语义复核，V05 用真实业务验证 |
| 大规模工具链变化拉长排障 | M01 先锁定工具链；重型检查共用 heavy_compute 锁，先定向后全量 |
| 新注册无额度、OAuth 失效 | A04 共同契约、M03 同事务初始化、V01/V05 多入口对账 |
| WebApp 公开行为、旧清单强制登录冲突 | 更新矩阵，保留四个 tag 后提交；匿名计费债务单列 |
| Agent 旧数据迁移没有完整回填 | V03 记录丢失范围并取得业务数据处置决定；不能只看 migration exit code |
| 模型归一删凭据/邮箱别名碰撞 | V03 在副本列冲突组与选中结果；异常先阻断，再确定处置 |
| 升级时间、真实版本未知 | A03/R02 填环境参数和演练耗时，超窗口不放行 D00 |
| 快照恢复会丢切换后写入 | D01 设静止点，D04 观察窗口与 RPO；V06 证明整套恢复 |
| P4/P6 并发改同宿主 | 暂停共享文件实施，先完成本轮，再重新规划 P4/P6 |

## Migration Plan

顺序与命令见 [execution-graph.md](execution-graph.md) 和 [runbook.md](runbook.md)。源码链：授权与冻结 → 非部署 merge 基线提交 → 基础/身份/计费/前端/部署逐节点适配提交 → 集成候选源码提交 → 静态与定向验证 → 代码候选。环境链：真实盘点 → 镜像与空库 → 存量双迁移及向量演练 → 业务与恢复演练 → 上线操作包 → 生产授权 → 停写快照 → 向量/双迁移 → 放量观察。生产阶段任何失败均执行 runbook 对应恢复路线。

## Open Questions

这些均为部署参数，不改变本轮保留行为的决定；A03 先准备事实与命令草案，V03 固定 digest/override 并执行演练，R02 签收最终操作包。

- 实际部署的源码/镜像 digest、DB 类型/版本/双 head、向量库类型/版本/数据量；支持两种数据库时分别演练，单一生产引擎至少完整覆盖实际使用引擎。
- 旧 Agent runtime/drive 是否有必须保留的业务数据；若有，单独数据导出/转换方案通过后才能迁移。
- 维护窗口、可接受停机、RTO/RPO、监控阈值、备份保留期、业务验收人和真实测试账号/模型/OAuth 配置。
- 环境 API Key 额度若需新增支持、匿名付款人规则若需变更，均进入单独产品变更；不以未答复推定授权。
