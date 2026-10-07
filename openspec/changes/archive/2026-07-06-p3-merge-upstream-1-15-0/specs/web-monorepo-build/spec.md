## ADDED Requirements

### Requirement: pnpm workspace 结构以上游为准落地

合并 1.15.0 后，仓库 SHALL 采用上游的 pnpm monorepo 结构：根级 `pnpm-workspace.yaml`（含 catalog 依赖治理）、根级 `pnpm-lock.yaml`、`packages/*` 子包（`contracts`、`dify-ui`、`iconify-collections` 等）完整存在；`web/pnpm-lock.yaml` MUST 被删除且不再被任何脚本/CI 引用。

#### Scenario: workspace 安装成功

- **WHEN** 在仓库根目录执行 `pnpm install`
- **THEN** 安装成功，`web` 与全部 `packages/*` 子包依赖就绪，且仅生成根级 `pnpm-lock.yaml`

#### Scenario: 旧锁文件不再存在

- **WHEN** 检查 `web/` 目录与全仓脚本/CI 配置
- **THEN** `web/pnpm-lock.yaml` 不存在，且没有任何脚本或 CI 步骤引用该路径

### Requirement: fork 增补依赖以 catalog 兼容方式合入

fork 在 `web/package.json` 增补的二开依赖（`dingtalk-jsapi`、`papaparse`、`jschardet`、`serwist`、`esbuild-wasm` 及相应 `@types/*`）SHALL 按"catalog 优先、包内固定兜底"策略合入：上游 catalog 已有同名条目的 MUST 改写为 `catalog:` 引用；catalog 没有的保留包内显式版本；MUST NOT 向根 `pnpm-workspace.yaml` 的 catalog 添加 fork 专有条目。

#### Scenario: 依赖可解析且策略合规

- **WHEN** `pnpm install` 完成后审查 `web/package.json` 与根 `pnpm-workspace.yaml` 的 diff
- **THEN** 5 个 fork 依赖全部可解析安装，catalog 中无 fork 新增条目，且与上游 catalog 同名的依赖使用 `catalog:` 协议

### Requirement: monorepo 下前端质量门禁全通过

合并与依赖合入完成后，web 侧 SHALL 通过全部质量门禁：`pnpm lint`、`pnpm type-check:tsgo`、`pnpm build` 在 monorepo 结构下全部成功。

#### Scenario: 三件套全绿

- **WHEN** 在 monorepo 下依次执行 `pnpm lint`、`pnpm type-check:tsgo`、`pnpm build`
- **THEN** 三个命令全部以退出码 0 结束

### Requirement: Docker 与 CI 适配 workspace 构建

web 镜像构建 SHALL 适配 monorepo：构建上下文包含根级 `pnpm-workspace.yaml`、`pnpm-lock.yaml` 与所需 `packages/*`；`docker/docker-compose.dify-plus.yaml` 的 web 服务 build 配置与 `.gitlab-ci.yml` 的前端构建/缓存步骤 MUST 随之更新并可跑通。

#### Scenario: web 镜像构建成功

- **WHEN** 按 `docker/docker-compose.dify-plus.yaml` 构建 web 镜像
- **THEN** 构建成功，容器内应用可正常启动并提供页面

#### Scenario: CI 前端流水线通过

- **WHEN** `.gitlab-ci.yml` 前端相关 job 在合并分支上运行
- **THEN** 安装、构建步骤在 workspace 模式下全部通过
