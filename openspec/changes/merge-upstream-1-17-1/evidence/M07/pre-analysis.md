# M07 实施前只读分析

- 节点：M07 综合部署和 CI 对齐
- 分析任务：`/root/m07_deploy_analysis`，Sol（`gpt-6-sol`）
- 分析输入基线：`ef55de14766ce2aba660c379f7b9b507cfc5561e`
- 上游目标：`8387590ace4a094de812b7847fc6a4c3a27cd52b`（tag `1.17.1`）
- 依赖：M01 已通过；本分析只读，没有改部署代码、启动容器或访问真实环境。

## 实施边界和顺序

M07 可在本地完成配置和 CI 对齐，但不等于镜像已供货、旧库已升级或生产已验收。手工更新 fork 综合 Compose，保留 fork 的私有 API/Web 镜像、`worker-gaia`、`worker-dataset`、`sandbox-full`、业务开关、nginx 路由和存储挂载；不要用上游生成的 Compose 覆盖 fork 专用拓扑。改动应集中在部署配置和模板，逐项对应 1.17.1 契约，避免在上游运行时源码中增加不必要的 fork 分支。

迁移当前由 `api/docker/entrypoint.sh` 在 `MIGRATION_ENABLED=true` 时自动执行 `flask upgrade-db`；该入口目前只推进主 Alembic 链。Compose 的共享默认值会让多个 API 镜像服务都执行该入口。实现必须选择一个单独的迁移执行者顺序升级主链和扩展链，再令业务 API、worker、beat、websocket 等容器关闭自动迁移；失败时留存版本表与残余对象状态，不重试未知的部分迁移。

## 部署契约审查

| 区域 | 已确认的实施依据 | 保留或验收边界 |
|---|---|---|
| 镜像与工作进程 | 当前 Compose 使用 fork 私有 API 镜像，并有 `worker-gaia`、`worker-dataset` 与 `sandbox-full` | 目标 API/Web 镜像必须从 fork 构建并保持同一发布版本；私有镜像供应和 digest 尚未验证 |
| Agent 认证 | 1.17.1 在 API 与 Agent backend 两侧使用匹配的 API token；当前 Compose 沿用旧 shellctl token 配置 | 增加新变量时不记录真实密钥；样例密钥应留空或明确是占位；后续须验证两端相同且 Agent 关闭开关仍可启动 |
| Agent 网络和文件 | 上游新增隔离网络、专用 SSRF proxy、local sandbox home/workspace 卷，以及 `/files` 和 agent-stub 代理规则 | 逐项对齐 Compose、Squid 模板和 env 样例；不扩大通用私网白名单，也不声称 sandbox 完全隔离 |
| Agent 策略 | fork Compose retention 为 259200 秒，两个 env 样例当前有 Agent v2 开关开启 | 保留 fork retention 与 Agent v2 关闭策略；使 Compose 与样例一致，不让容器存在被误解为功能已启用 |
| Plugin daemon | fork 使用私有 `0.6.3-local` 镜像并挂载持久存储；上游声明更新到 `0.6.10-local` | 不假设私有新镜像已发布；保留持久卷并补齐目标版本需要的网络、队列和配置 |
| Web/Ingress | fork 已有内部 Console API 地址、Socket.IO 路由；Web Dockerfile 已包含 Node 24.20.0 和 Next/Vinext 构建路径 | 保留登录回跳、fork 管理路由和 websocket；只移植有依据的 nginx/env 变化，不机械重写已对齐的 Dockerfile |
| 其他环境 | fork Weaviate 声明为 1.19.0；1.17.1 上游声明 1.39.2；fork 客户端和数据迁移不能由 tag 修改代替 | M07 不宣称完成向量库升级；真实版本、数据路径、逐站副本演练留给 A03/V04 |
| CI 与供货 | fork 的 `build-push.yml` 和 `docker-build.yml` 均以 `push: false` 执行构建验证 | 将 CI build validate 与私有镜像发布分开记账；不以绿色 build 证明镜像可拉取 |

## 实施路径登记

三条原始冲突路径继续由 M07 独占：`docker/.env.example`、`docker/envs/core-services/web.env.example`、`docker/nginx/conf.d/default.conf.template`。另外 15 个非冲突写入候选路径已在 `research/conflict-ownership.tsv` 标记为 registered，并列入 `execution-graph.json` 的 `registered_additional_paths`。若实现发现需要改 API/Web Dockerfile 或 `web/docker/entrypoint.sh`，须先登记精确路径再修改。

具体结论、任务门槛和写入路径清单同时记录在 `pre-analysis.json` 与 `path-registration.json`。代码步骤 13.1–13.6 仍未完成；当前编码前置技能 `karpathy-guidelines/SKILL.md` 在技能目录中不可用，等待用户提供路径或确认可按目录摘要继续。
