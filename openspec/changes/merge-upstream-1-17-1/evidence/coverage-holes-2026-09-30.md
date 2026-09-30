# 明确实际源码与已有证据覆盖缺口

本文件是只读审计，不是失败复现报告。四扩展模型省略ID路径与28.6已证实quota NULL identity机制同形，但未独立运行前不得宣布新失败。

## 四模型实际入口

- `api/models/model_extend.py`四类id仍仅server默认：EndUserAccountJoinsExtend第15行、AppExtend31、MessageContextExtend51、AppStatisticsExtend68。迁移MySQL使用`(UUID())`server默认；已有quota问题显示DDL成功不证明ORM取回UUID成功。
- `api/services/app_generate_service_extend.py:21`缺stats首用创建AppStatisticsExtend不传id；异常按既有行为静默忽略，实际统计写入/后续session状态需验。N01新App事件显式UUID只证明新signal入口，不能替代这个旧缺行入口。
- `api/controllers/service_api/wraps.py:490`登录WebApp用户关联EndUserAccountJoinsExtend不传id。
- `api/controllers/console/app/model_config.py:135`首次记忆retention保存AppExtend不传id；auth switch服务`webapp_auth_service_extend.py:83`已有显式UUID是另一入口。
- `api/core/app/apps/base_app_runner.py:114`超过retention插入MessageContextExtend不传id。

独立app_center执行者在隔离PG/MySQL真实flush/readback/rollback四模型，并保留首次失败如有；runtime后续走真实调用/配置/多轮入口，不能显式传ID绕过。

## 其他具体缺口

28.3只读证据是start/llm/answer3node；传统workflow Tool配置save/refetch/publish/run和plugin/sandbox实际工具执行没有证据。28.xquota UI只有1row，跨页多行编辑/刷新未证实。新App.tags session读取虽被修复，实际tag CRUD绑定与分类/搜索/cache未覆盖。12.6i key create/copy/edit/delete和权限只是mock组件test。M03 OAuth/注册/邀请单位测试明确mock，管理员setup不代表所有账号入口。相应最小步骤已作为现有F01/F02/F04/K01/B01/B03/B09/O01子检查，未扩产品功能。

## Queue归属判断

`api/docker/entrypoint.sh:41`主worker默认含workflow_based_app_execution；`docker/docker-compose.dify-plus.yaml:1033–1042`worker-gaia以MODE=worker、默认extend_high/extend_low消费扩展队列，dataset worker默认dataset/priority_dataset。`api/core/app/workflow/layers/persistence.py:323`派发计费task，该task在`api/tasks/extend/update_account_money_when_workflow_node_execution_created_extend.py:86`绑定extend_high。标准fork Compose源码保留完整split；main worker单独不含extend是设计，不是合并遗失。

V05/28.7缩减测试override明确只消费workflow_based_app_execution；遗漏worker-gaia为本地隔离拓扑选择。实际当前active_queues由运行执行者核验；修复范围是可复用本地启动override/完整必要服务及交付runbook，未有证据要求修改正式queue源码。先前worker只有workflow执行成功并不证明异步非零计费消费。其他独立源审计证据：`n05-design/worker-queue-topology.md`。


## 后续独立运行已确认的缺陷

父协调转达实际双engine检查：PostgreSQL四模型省略ID成功；MySQL四模型均NULL identity FlushError。首次源码风险现升级为confirmed regression。app_center已获授权最小客户端UUID defaults修复与focused regression，修复后重新独立双engine flush/readback/rollback。永久首失败与修后报告路径待执行者交付，不能只用父消息替代最终证据。`api/models/model_extend.py`及新regression路径必须进入N02最终candidate archive/manifest，并在当前API build后增量rebuild完整镜像；当前pre-repair镜像最多作为中间诊断，不作为最终N03验收。
