# R02 本轮本地交付与代码留档执行边界

本文件是操作准备，尚未 stage、commit、tag。执行者是协调员指定的发布人员。先完成 N09/N10 最终运行、原23000最新镜像读回及本地必测门槛；外部条件与旧库/生产门槛必须原样列明。

## 候选与路径

- 代码：`evidence/provider-packaging-regression/latest-source-manifest.json` 中除 `api/uv.lock` 外55个 owned source/test路径。
- 文档与证据：`evidence/release-preflight/owned-delivery-manifest.json` 的明确文件，以及该 manifest 自身。最终证据落盘后重新生成 hash 和数量；禁止使用目录级 broad stage。
- 14个定向 proof logs 当前被 `.gitignore` 忽略，执行者只可按重新审查后的精确文件名单 `git add -f`。私有配置、cookie、备份、cache、wheel、build目录禁止加入。
- 用户既有 `api/uv.lock`、`pnpm-lock.yaml`、其他 change/skill路径不 stage。原3个graph直接引用的历史输入已获协调员授权并通过只读来源/脱敏审查，按 `historical-input-review.json` 的三个精确路径留档，保持原结论与原字节，不能当本轮runtime验收。其他unrelated不扩大。

## 最终检查与提交顺序

1. 重读当前HEAD/branch/index/tag事实，确认无并行index写入。当前基线953cf1；已有tag如出现，不force或覆盖，先核其目标。
2. 验证55source冻结哈希、完整HEAD-lock context3cf5bfa6、47镜像path（46owned生产源码与HEADlock）、38distributions、32entrypoint loads、8trace imports及实际Qdrant清理证据。9test paths已归档于context但按Dockerignore不进入镜像。
3. 验证原23000最终三service同immutableimage、页面/安装API/readback、恢复克隆选定读回。API模型/密钥/价格、用户数据保持；关闭态/启用态与外部条件分开。
4. 生成普通文件与已审ignored日志的两个 NUL-delimited pathspec，分别精确 `git add --pathspec-from-file=... --pathspec-file-nul` 与 `git add -f --pathspec-from-file=... --pathspec-file-nul`。这是未来执行步骤，本文件不执行。
5. 核index路径严格等于批准白名单，用户两个lock和其他路径均未入index；`git diff --cached --check` 和 OpenSpec strict/reference检查通过。所有文字/JSON/日志再次作凭据脱敏检查，不输出匹配值。
6. 提交明确 source/docs/evidence，保存实际commit/tree/index清单。`COMMIT_SHA=953cf1` 是镜像归档基线；通过55ownedsource hash＋原HEADlock把445d59 artifact映射到新codecommit，不能把953称最终源码提交。两上下文唯一差异是用户dirty lock。
7. 仅在 `fork-merged-1.17.1` 不存在时创建留档tag，目标为步骤6已验证的 codecommit，标注 code-ready/local acceptance及未验外部/旧库/生产；不push、生产部署或覆盖tag。
8. 保存实际 `evidence/R02/result.json` 与 `execution.log`（command/exit/assertions/commit/tag事实，不含凭据）。这些实际事实可作独立小文档提交，明确tag指前一codecommit；不得force retag避免自引用。后续只交付记录变化不触发源码业务重复测试。

原R02上线操作包仍依赖 V03–V06 原定义。当前 fresh 安装/当前快照恢复与本地留档不替代旧版本升级/回滚或生产完成。所有分支/工作树维持现状。
