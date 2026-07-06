# Tasks: p3-merge-upstream-1-15-0

> 前置条件：`p2-merge-upstream-1-14-2` 已完成（fork 站在 1.14.2 基线，挂点清单坐标为 1.14.2 版，6 条计费回归链路全绿）；D2（批量工作流去留）已有书面结论（决定任务 5.1 的处理分支）。

## 1. 合并准备与执行（主 agent 顺序）

- [x] 1.1 确认前置条件：P2 回归全绿、D2 结论在案；从 P2 完成点切出合并分支 `merge/upstream-1.15.0`；对旧 header 导航（owner 与普通用户双角色）截图留档，用于 main-nav 迁移后对比（P2 已归档 `archive/2026-07-05-p2-merge-upstream-1-14-2`，回归以代码级验证全绿收口；D2=删除批量功能（提交 4071db17）；分支已从 `ffb0c5303f` 切出；本地无运行环境无法截图，以代码级基线 `nav-mount-baseline.md` 替代留档）
- [x] 1.2 执行 `git merge 1.15.0 --no-commit`；机械冲突批量解决（蓝图注册、`models/__init__.py`、i18n namespace、锁文件直接取上游等）（79 个冲突：锁文件/uk-UA/AU 测试脚本取上游；uk-UA 8 个 DU 文件按"取上游恢复"处理以消除后续冲突税——languages.ts 中 uk-UA 仍启用，删除策略与之矛盾，已统一为跟随上游）
- [x] 1.3 逐个人工核对高危冲突文件：api 侧 `service_api/wraps.py`（validate_token_quota_extend 保留）、`console/apikey.py`（额度字段/软删/put 全保留+上游 dump_response 路线）、`persistence.py`（计费派发在位）、`*/app_generator.py`（account_id extras 重挂）、`console/feature.py`（CVE login_config JWT 门禁保留、SystemFeatureHealthApi 保留）、`feature_service.py`（钉钉/OAuth2 字段并入新模型）、`account_service.py`/`workspace_service.py`（额度初始化与 admin_extend 标记保留）；`generate_task_pipeline/oauth/token_buffer/ext_celery` 本次干净合并，挂点扫描确认全部在位；web 侧 `contract/router.ts`（loginConfig+systemManage 重挂、上游 snippets 合入）、`contract/console/system.ts`（重写为纯 fork contract，类型迁 `features/system-features/extend.ts`）、`app-context*`（admin_extend/tenant_extend 以 UserProfileWithExtend 重对位）、`i18n` 干净合并
- [x] 1.4 提交 merge commit（允许暂时构建不过），确保 `git grep -l '<<<<<<<' -- api/ web/` 为空——作为后续修复阶段的稳定基点（冲突标记扫描为零）

## 2. 构建体系适配（主 agent 顺序，最大单项）

- [x] 2.1 web monorepo 落地：根级 `pnpm-workspace.yaml` / `packages/*` 以上游为准；删除 `web/pnpm-lock.yaml`；确认无脚本/CI 残留引用旧锁文件路径（旧锁文件在 P2 已删，本次扫描无残留引用）
- [x] 2.2 fork 增补依赖按 DD2 策略合入 `web/package.json`（现存清单：`dingtalk-jsapi`、`serwist`+`@serwist/next`+`@serwist/turbopack`、`lodash-es`、`esbuild-wasm` 包内固定版本；`react-papaparse` 已走 catalog；`papaparse/jschardet` P2 已不再直接依赖）；`pnpm install` 重生成根锁文件；开放问题 4 结论：serwist 走 `web/app/serwist/[path]/route.ts` + `@serwist/turbopack`，无 next.config 联动，但构建期需要 `esbuild-wasm`（已补 0.27.2，缺失会导致 `pnpm build` 在 /serwist/[path] 收集页面数据时失败）
- [x] 2.3 api uv workspace 对齐：workspace 成员与 `dify-agent` editable 包生效；`uv lock` 重生成（graphon 0.5.3 等上游精确 pin + alibabacloud_dingtalk/pypinyin fork 依赖合入）；Python `~=3.12.0`；`uv sync` 通过
- [x] 2.4 按 DD5 重做 `api/Dockerfile`：上游 1.15.0 骨架（根上下文 COPY api/... 与 dify-agent/...）+ fork 补丁块（阿里云 apt/pypi 镜像源；`requirements.docker.txt` 已在 P2 退役）；**本地镜像构建被环境阻塞**——宿主机 Clash fake-ip DNS（198.18.0.x → 127.0.0.1:7890）代理未运行导致容器内 apt 全部 500，Dockerfile 本身已按 DD5 核对，留待代理恢复或 CI 验证（9.1）
- [x] 2.5 web Dockerfile 为上游 workspace 版（根上下文，COPY 根锁文件与 packages/*）；`docker/docker-compose.dify-plus.yaml` 镜像号升 1.15.0；web 镜像本地构建同受代理环境阻塞，留待 9.1
- [x] 2.6 `.gitlab-ci.yml` 适配：api/web 构建上下文由子目录改为仓库根（`-f api/Dockerfile .` / `-f web/Dockerfile .`），删除 admin 4 个构建 job 与 2 个 manifest job（p5 已废弃 admin）；CI 实跑留待合并分支推送后验证
- [x] 2.7 构建门禁初验全部通过：`pnpm lint`（0 error；suppressions 经 --suppress-all + --prune-suppressions 重生成，净 -617 行）、`pnpm type-check`（tsgo，修复 5 处：types/feature 残留 import、SearchInput 具名导出、Webhooks 图标改 iconify、base/radio 改 dify-ui radio 原语、.next 陈旧产物）、`pnpm build`（含 system-manage-extend 全部路由）、api `create_app()` 冒烟 + ruff check 全绿

## 3. main-nav 挂载点重做（可 sub-agent 并行，见第 8 节）

- [x] 3.1 额度徽章重做完成【sub-agent a】：新组件 `main-nav/components/account-money-extend.tsx`，挂载于 `main-nav/index.tsx` WorkspaceCard 之下（Begin/End 标记）；硬编码汇率 6.97 已删——后端 `feature_service.py` 的 SystemFeatureModel 新增 `rmb_to_usd_rate`（读 `RMB_TO_USD_RATE` 配置，默认 7.26）经 login_config 下发，前端经 `SystemFeaturesExtend` 读取（旧后端未下发时前端回退 7.26）；行为保持（total=0 不渲染、<10 元红、>50 元黄）
- [x] 3.2 系统管理入口重做完成：`main-nav/components/system-manage-nav-extend.tsx`，仅 `isCurrentWorkspaceOwner` 可见，链接 `/system-manage-extend/system-integration`，segment 高亮，复用 main-nav nav-link 样式约定
- [x] 3.3 `nav-extend`/`draw-nav-extend` 迁移完成：组件迁至 `main-nav/components/{draw-nav-extend,amazon-marketing-nav-extend}.tsx`，宿主保持「注释态预留挂载位、默认关闭」与迁移前等价；另外 logo 链接与 home 路由（`routes.ts`）指向 `/explore/apps-center-extend`，与旧 header 行为一致
- [x] 3.4 account-dropdown：基线与代码复核确认 fork 对其无差异（1.12.1 起即无二开项），新 account-section 无需重挂，零改动
- [x] 3.5 旧 `header/` 下 4 个 extend 目录已删除，全仓无旧路径 import 残留（rg 验证）；main-nav 单测 5 文件 92 用例全过（含 home href 断言更新）

## 4. headlessui 清退（可 sub-agent 并行）

- [x] 4.1 全仓扫描 `rg '@headlessui/react' web/`：零残留——fork 组件（invite-modal、system-manage-extend、auto-select-extend、react-multi-email-extend 等）在 P2 的 dify-ui 批量迁移中已完成清退
- [x] 4.2 无需替换（4.1 扫描为零；本次 merge 中 secret-key-modal/app-card 的 @heroicons 残留 import 也已顺势清除，@heroicons/react 为上游 catalog 自留依赖，不在清退范围）
- [x] 4.3 验收通过：`rg '@headlessui/react' web/ packages/` 归零；`web/package.json` 与根锁文件无该依赖；`pnpm type-check` 通过

## 5. web 侧 fork 侵入重对位（主 agent 顺序，依赖 D2 结论）

- [x] 5.1 `text-generation/index.tsx` 按 DD6 的 D2=删除分支处理：与上游 1.15.0 逐字节一致（diff 为空），批量逻辑不搬迁
- [x] 5.2 contract 重对位完成：`router.ts` 保留 loginConfigBootstrap/loginConfig + systemManage 代码执行控制注册并合入上游 snippets/contracts 结构；`console/system.ts` 重写为纯 fork contract（上游 systemFeaturesContract 由 packages/contracts 生成物接管）；页面请求实测归 9.4 回归
- [x] 5.3 `app-context*` 扩展字段重对位：`UserProfileWithExtend` 类型 + normalizeCurrentWorkspace 透传 admin_extend/tenant_extend，type-check 通过
- [x] 5.4 i18n：extend namespace 在新结构注册完好（i18next-config.ts:46、resources.ts:76/115，合并时干净保留）；`i18n/uk-UA` 策略统一为「跟随上游恢复全量文件」（消除每次升级的 DU 冲突税；languages.ts 中 uk-UA 本就启用，与删除策略矛盾——决策记录于 1.2）

## 6. api providers 拆包与探索页分类迁移（可 sub-agent 并行）

- [x] 6.1 vdb import 扫描完成【sub-agent c】：fork 代码（extend 文件、migrations_extend、configs/extend）对已迁走 provider 的 import 为零；残余 `core.rag.datasource.vdb.*` 引用全部指向仍留在原地的基类/工厂（vector_factory/vector_type），与上游一致；`create_app()` 冒烟通过
- [x] 6.2 分类数据迁移版本 `017_migrate_recommended_cats`（migrations_extend，down_revision=016）：join 表数据合并写入 `recommended_apps.categories`；幂等（合并去重）、fork 表缺失时安全跳过、downgrade no-op（docstring 说明依赖快照回滚）；**scratch 库实测**：迁移正确（原生已有值保序、fork 分类去重追加）、重复执行结果不变、join 对数=迁移后展开对数（4=4）。修复演练发现的问题：revision ID 超 varchar(32) 已缩短
- [x] 6.3 api 侧切换完成：`database_retrieval.py` 与上游 1.15.0 逐字节一致（fork 分类侵入全删）；`recommended_app_service_extend.py` 的同步/取消同步改写 `recommended_apps.categories`（分类来源为应用工作区标签），fork 分类表模型类自 `model_extend.py` 移除
- [x] 6.4 web 侧零改动结论成立：应用中心页走 `/installed/apps`（fetchOpenInstalledAppList），后端切换后响应结构不变（categories + recommended_apps）；explore 上游组件本就读 categories 列；type-check 与 explore 相关测试通过
- [x] 6.5 drop 迁移版本 `018_drop_recommended_cats`（独立版本、链尾）：docstring 显著标注「仅在 9.4 探索页回归通过后执行，此前可停在 017」；downgrade 重建空表结构；scratch 库实测 drop 成功

## 7. 升级动作与环境对齐（可 sub-agent 并行准备，执行主 agent）

- [x] 7.1 env/compose 对齐完成【sub-agent d】：`docker-compose.dify-plus.yaml` x-shared-env 补齐上游新增变量（SERVER_CONSOLE_API_URL、ENABLE_LEARN_APP、NEXT_PUBLIC_ENABLE_FEATURE_PREVIEW、ENABLE_AGENT_V2、MILVUS_SECURE 等、SSRF_PROXY_ALLOW_PRIVATE_IPS/DOMAINS、PLUGIN_MODEL_PROVIDERS_CACHE_TTL、OPENAPI_* 4 项、DEVICE_FLOW/OAUTH_BEARER、NACOS 超时 2 项），清除被删变量（SSRF_REVERSE_PROXY_PORT、SSRF_SANDBOX_HOST 及 squid 容器旧环境）；ssrf_proxy 服务块与上游 1.15.0 一致（保留 fork 镜像源）；`docker compose config -q` 通过；UV_CACHE_DIR 复核结论：1.14.2 已存在、非本步改名项
- [x] 7.2 SSRF 白名单配置准备完成：`SSRF_PROXY_ALLOW_PRIVATE_IPS/DOMAINS` 进入 compose 共享环境块与 squid 容器环境（生效层为 squid 的 squid.conf.template ACL），`.env.example` 补带中文注释条目（CIDR/通配写法说明）；**具体内网目标清单待业务方提供后填入生产 .env（设计开放问题 3，唯一遗留人工项）**
- [x] 7.3 测试库三段命令链演练通过（本机 pgvector scratch 库 dify_p3_cat_test，演练后已清理）：`flask db upgrade` 到 head d9e8f7a6b5c4 → `flask extend_db upgrade` 到 018（含 017 分类数据迁移，抽验计数一致、幂等复跑结果不变）→ `flask backfill-plugin-auto-upgrade` 正常完成（空库 0 租户为预期；生产执行后需验证租户策略行数非零）
- [x] 7.4 升级序列与强制项已写入 `docs/dify-plus/上游升级与回归检查清单.md` 第 0 节（备份→三段命令链（含 017/018 分步执行指引）→分类抽验 SQL→env 对齐→SSRF 白名单→起动回归→回滚路径；backfill 与 SSRF 加白标注【强制】）

## 8. Parallelization Plan（并发执行策略）

- [x] 8.1 **顺序阶段（主 agent）**：第 1 节合并冲突解决与第 2 节构建体系适配已由主 agent 顺序完成（merge commit 72f5def043、构建适配 c70da08082），期间无 sub-agent 写入
- [x] 8.2 **并行修复阶段**：按计划派出 3 个 sub-agent（模型 Fable 5）并行执行 (a) main-nav 重做、(c) 分类迁移+vdb import、(d) env/SSRF 对齐；(b) headlessui 清退经扫描确认 P2 已完成、零残留，无需派发。每个 sub-agent 输入为对应小节+design 决策+spec，产出编辑+自验+摘要，主 agent 汇总后统跑全量门禁
- [x] 8.3 **冲突边界**：第 5 节由主 agent 在 1.3 高危核对中顺序完成（text-generation 走 D2=删除分支与上游一致、contract/app-context/i18n 重对位见 5.x 记录）；sub-agent 均被明确禁止修改锁文件、Dockerfile、CI、`pnpm-workspace.yaml`
- [x] 8.4 **并行回归阶段**：本地无运行环境（无 dify compose 起动、Docker 被代理环境阻塞），分域回归按 P1/P2 先例降级为主 agent 代码级验证 + 全量单测（见 9.1-9.5 各项记录），未再派 sub-agent；运行时分域回归清单保留在 runbook（检查清单第 0.7 节）供部署环境并行执行

## 9. Architecture Verification（全量回归与收尾）

- [x] 9.1 构建门禁终验：`pnpm install / lint / type-check / build` 全通过；api 单测 14582 passed / 0 failed（8 个失败已修：6 个为上游新测试未适配 fork 行为——apikey 额度联查/请求上下文、persistence 计费属性、chat runner 记忆上下文、EndUserType 枚举守卫、以及 merge 引入的 account-setting `<div>` 破坏 a11y 的真实回归；2 个为环境/性能波动复跑即过）；web 测试 28777 passed / 0 failed（修复 4 个 fork 行为适配 + system-features CVE 流 mock）；**Docker 双镜像本地构建被宿主机代理环境阻塞（Clash fake-ip DNS 代理未运行，容器内 apt 全 500），Dockerfile/CI 已按 DD5 静态核对，实际构建留待 CI 或代理恢复后执行**；CI 流水线待推送后验证
- [x] 9.2 计费 6 链路代码级验证全绿（本地无运行环境，按 P1/P2 先例执行）：money_limit 装饰器在 console/explore/web 5 个入口在位、validate_token_quota_extend 日/月限额分支在位、persistence 计费派发在位（含新单测）、message_was_created handler 与 3 个 beat 任务在位、fork 零引用 quota v3、persistence 与 llm_quota 互不调用；挂点 1.15.0 坐标已登记回差异总表（与 1.14.2 一致 + 装饰器共存说明）；**运行时扣费回归留待部署环境按《计费回归基线清单》执行**
- [x] 9.3 main-nav 双角色代码级验证：对照 `nav-mount-baseline.md` 逐项核对——额度徽章（汇率改读后端 7.26、阈值行为不变）、系统管理入口 owner-only、nav/draw 预留位注释态、account-dropdown 零差异；main-nav 单测 92 用例全过；**运行时双角色截图对比留待部署环境**
- [x] 9.4 功能回归代码级验证：SSO（oauth.py/auth 本次干净合并，P2 已实测代码链路）、应用中心（分类迁移 scratch 库实测数据一致；页面数据结构不变、相关测试过）、系统管理页（3 条路由构建产物在位、代码执行控制 contract 重挂）、批量运行不适用（D2=删除）；**运行时回归留待部署环境**
- [x] 9.5 SSRF 白名单验证步骤已固化进 runbook（加白目标可访问 + 未加白 403 双向验证）；需要部署环境与业务方白名单清单，**留待生产/预发执行**
- [x] 9.6 drop 迁移（018）已在 scratch 库演练（migrate→验证→drop 全链通过）；生产按 runbook 分步执行：先停在 017，探索页回归通过后升到 head
- [x] 9.7 合并分支已合入主线（main fast-forward）并打 tag `fork-merged-1.15.0`；生产升级按 runbook（检查清单第 0 节）在停机窗口执行
