# 提交节奏与 merge 基线

## 当前输入与边界

- 规划修订前的当前分支：`codex/merge-upstream-1.17.1`；HEAD：`0c80717ead7d0c7a33c66d2fdc668b2a3bdcdafe`。
- 固定上游 tag `1.17.1^{commit}`：`8387590ace4a094de812b7847fc6a4c3a27cd52b`；merge-base：`5c6372d2f76d240265b92fd27c16bc772ffcb107`。当前没有 `MERGE_HEAD`，真实 merge 尚未执行。
- 本次修订开始时，目标 OpenSpec 目录无已修改或未跟踪文件；工作树其他位置存在既有未跟踪工具目录：`.agents/skills/openspec-*`、`.claude/commands/`、`.claude/skills/openspec-*`、`.cursor/`、`.gemini/commands/`、`.gemini/skills/`、`.github/prompts/`、`.github/skills/`、`.tmp/`。本次不处理这些路径。
- A02 台账有 91 行唯一冲突路径，owner 分布为 M01 5、M02 7、M03 5、M04 26、M05 7、M06 36、M07 3、M08 2；这是 merge-tree 预测。M00 必须用真实 merge 的冲突列表逐项复核，差异返回 A02，不把预测当成已执行结果。

## 一次性 Git 行为实验与推论

主任务此前在隔离临时 Git 仓库创建双分支文本冲突，启动 no-commit merge，在 `MERGE_HEAD` 存在时尝试按路径提交。Git 返回：`fatal: cannot do a partial commit during a merge.` 该实验未在本工作树运行；本工作树的 `MERGE_HEAD` 仍不存在。这个结果说明原计划让 M00 保持未提交 merge、再让 M01–M07 各自做部分路径提交无法同时成立。

因此 M00 在已授权分支形成一次双父 upstream merge 基线提交：先核对 91 个预测路径与所有真实冲突及 owner，逐个采用 upstream 1.17.1 的内容，上游删除则保持删除，记录结果；无未解决索引后提交。第一父保留 fork 原始内容，第二父固定到上述 upstream tag；A02 台账保留后续适配路径。M00 基线是非部署中间态，可能暂时缺少 fork 行为，不能视为功能验收或源码候选。

M01–M07 依现有 DAG 与 owner 分工适配，每节点完成后由集成负责人串行、精确暂存该节点代码、配套测试、证据与状态并独立提交。并行编辑不等于并行写 Git 索引。M08 复核冲突与自动合并语义、刷新移交后的锁文件和生成物，只提交集成修复及最终候选源码。V01/V02 和 R01 保持原门槛；A03 阻塞以及后续 V03–D04 环境和生产授权边界保持原状。若任一 fork 能力无法从第一父迁回并通过原矩阵，阻断 M08，不能以 M00 基线代替。

节点证据应记录提交前 HEAD、预期父提交、树摘要、实际结果与状态；提交后读取实际 SHA 供下一节点或外部验收引用。不能把某次提交自身 SHA 写入其自身内容。本次规划修订提交的 SHA 同样仅在提交后报告，不写进本文。全过程不新增分支或工作树，不使用多头 cherry-pick，不执行真实 merge 或生产动作。
