# Proposal: 合并 Dify upstream 1.17.1

## Why

Dify-Plus 当前基于 upstream 1.16.0，计划合入官方 1.17.1，并保留当前 HEAD 的企业扩展与后续 WebApp 认证修复。此次跨越 1,501 个上游提交，涉及契约层搬迁、显式数据库会话、17 个新增迁移和部署依赖升级，需要有依赖图、证据门槛及可恢复步骤的执行方案。

本 change 当前仅创建规划材料。业务代码、Git 合并、数据迁移、发布均未执行；规划完成不等于产品已完成升级。

## What Changes

- 以 fork `1c3368ed1584c4e9b6387a28334552d10c946ab4` 为分析快照，目标为 upstream `1.17.1` / `8387590ace4a094de812b7847fc6a4c3a27cd52b`，保留可追溯的正式 merge 历史。
- 对 91 个预测冲突路径分配文件所有权；同时检查自动合并文件中的调用、事务、鉴权及扣费语义。
- 迁移登录双阶段协议、前端 contract/transport 挂点和 WebApp 访问开关至新上游结构；覆盖当前基线 tag 后四个提交。
- 适配显式 session、注册账号额度建档、API Token 归因、dataset 级 API Key 权限、工作流计费与记忆上下文。
- **BREAKING（运维）**：处理 Agent 旧表删除、旧模型凭据归一与去重等数据迁移；使用快照恢复方案，不依赖 downgrade。
- **BREAKING（部署）**：将综合 Compose、构建工具链和运行时依赖对齐目标版本；Weaviate 按实际版本和数据格式选择迁移路径，已有卷不能直接更换目标镜像。
- 通过任务 DAG 描述输入、前置依赖、写入范围、执行步骤、验收证据、失败处理及实施/上线授权门。
- 在隔离环境完成构建、双迁移链、旧数据升级、业务回归和恢复演练后，再准备版本说明与生产升级包。

## Capabilities

### New Capabilities

- `upstream-merge-execution-graph`: 合并执行的任务依赖、文件写入所有权、证据有效期、失败恢复和授权门。

### Modified Capabilities

- `upstream-merge-baseline`: 将版本相关的迁移、依赖与前端构建要求更新至 1.17.1，并要求保留当前 fork HEAD 行为。
- `upstream-upgrade-runbook`: 将旧的固定三段升级序列更新为按来源版本判断的单点迁移流程，补齐向量库、存量数据、恢复与发布验收。
- `fork-extension-compatibility`: 将显式 session、controller、注册建档、登录防护与前端挂载要求更新到 1.17.1 的真实入口。

## Impact

- 源码：`api/`、`web/`、`packages/contracts/`、`packages/dify-ui/`、`dify-agent/`、`dify-agent-runtime/` 及相关依赖文件。
- 部署：`docker/docker-compose.dify-plus.yaml`、环境变量、nginx、镜像构建和 CI；现有 Weaviate 镜像声明 1.19.0 与客户端 4.20.5 的版本要求需核实。
- 数据：主 Alembic 链、独立 `migrations_extend` 链、Agent 运行数据、模型凭据、向量库及对象存储的匹配恢复点。
- 验证：六条计费业务链、认证矩阵、系统管理、应用中心、知识库检索、API Key scope、Human Input 和 Agent 路径。
- 并行工作：`p4-billing-quota-hardening`、`p6-console-manage-standardization` 保留为独立 change；在新基线上重审其路径及假设，新增业务改造不混入本轮合并。
- 当前授权仅涵盖分析和规划材料。创建执行分支/工作树、实施合并和生产上线分别按执行图中的授权门处理。
