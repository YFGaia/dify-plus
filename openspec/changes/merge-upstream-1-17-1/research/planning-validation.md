# 规划材料校验记录

日期：2026-09-28。结论：规划材料齐备，全部实施节点仍为 pending。本记录不表示源码合并、测试、迁移或部署通过。

## 1. 本轮执行过的检查

- `openspec validate merge-upstream-1-17-1 --strict`：通过，无归档预演告警。修正了原场景遗漏及 RENAMED header 格式，保留历史验收意图。
- `openspec status --change merge-upstream-1-17-1 --json`：proposal/design/specs/tasks 四类材料均完成；CLI 的 complete 指规划 artifact 完成。
- Python 对 JSON/Markdown/TSV 做结构校验：27 个唯一节点、34 条依赖边、无环、所有依赖存在；122 项实施任务均未勾选；每个节点有输入/输出/授权/范围/步骤/验收/失败去向。
- 91 个预测冲突全部恰好有一个解决 owner，均对应有效的解决和验收节点；和 source inventory 集合一致。
- 所有规划内本地 Markdown 链接目标存在，未发现行尾空白。校验摘要见 [planning-checks.json](planning-checks.json)。
- `git diff --check`：通过；原有 tracked 文件无修改。新文件为本次规划文档和 change 目录，原未跟踪目录保留。
- 复核 HEAD、upstream base 和 fork baseline tag 与 source inventory 一致。

## 2. 独立审阅及修订

由三个 Sol/high 子代理分别分析后端、前端、部署；前端与部署代理再交叉审阅中央计划。修订如下：

| 问题                                    | 已落实的修订                                                                  |
| --------------------------------------- | ----------------------------------------------------------------------------- |
| 演练命令等到 R02 才定稿，形成程序性循环 | A03 准备环境草案；V03 冻结实际 digest/override 和命令后演练；R02 依据结果签收 |
| 异步业务 smoke 时 worker 尚未启动       | D04 先启受控 worker/必要队列，beat/trigger 暂停；smoke 后开放其余消费与调度   |
| 向量兼容证据早于目标 API 镜像           | V04 依赖 V03，最终候选镜像固定后验证客户端与向量路径                          |
| 只读环境盘点被分支授权绑住              | A03 改为独立的获授权环境只读节点；代码与分支仍受 A00 控制                     |
| WebApp transport 无冲突文件漏分配       | M06 补 base/fetch/share/use-share 四文件所有权                                |
| 锁文件负责人和生成时点不一致            | M01 初始处理，M08 前显式移交集成负责人统一最终生成                            |
| 候选提交后还要求改测试                  | 对应 M 节点迁移/补充用例，M08 冻结；V01/V02 只运行，改用例则返回并生成新候选  |
| browser 验证缺命令                      | V02 补 browser 定向命令，并要求确认实际收集用例；零用例不得标通过             |
| P4/P6 提案与当前实现混淆                | 专项研究和中央契约均明确保留当前行为，后续重构/权限扩张另行决策               |

## 3. 证据来源与可重现范围

官方目标为 [1.17.1 release](https://github.com/langgenius/dify/releases/tag/1.17.1)，精确 SHA 见 [source-inventory.json](source-inventory.json)。读取目标源码使用临时 bare 对象库；`merge-tree --write-tree --name-only --messages` 返回冲突状态码 1 为预期结果，只写临时对象，没有对主仓执行 merge。

三方比较参数：upstream base `5c6372d2f76d240265b92fd27c16bc772ffcb107`，fork `1c3368ed1584c4e9b6387a28334552d10c946ab4`，target `8387590ace4a094de812b7847fc6a4c3a27cd52b`，Git 2.53.0，rename threshold 50%、diff/merge.renameLimit=10000。后续执行者使用这三个对象可重建清单；临时目录不是长期依赖。原始路径清单和冲突消息已保存到本目录。

## 4. 未执行事项

没有创建执行分支或工作树，没有源码 merge/commit、应用测试、依赖安装、构建、启动服务、读取生产凭据、真实业务调用、数据迁移或部署。生产配置、数据库/向量引擎实际版本和业务接受标准仍须 A03/R02 实例化。既有匿名计费和 P4 非幂等债务未修复。
