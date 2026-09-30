# 可复用本地验收运行包（配置已验，运行待执行）

唯一inventory是`runtime-plan.json`，精确overlay hash在该文件。项目`difyplus-acceptance-20260930`，浏览器/nginx `http://localhost:23010`、直接API `http://localhost:25442`、直接WS `ws://localhost:25443`。所有publish绑定IPv6 loopback [::1]，浏览器API/socket走nginx23010；内部service aliases不变。既有127.0.0.1:23000栈完全独立。

本包是标准fork Compose的隔离overlay，不是生产配置。PG/MySQL/Redis/plugin/files/Qdrant/sandbox/nginx生成配置均独立named volumes；仓库只读配置模板bind，不挂原volumes目录、原DB/Redis或source-overlay。正式main worker与worker-gaia分别覆盖workflow执行和extend计费队列，额外dataset worker保留；协同使用专用api_websocket和nginx路由。Agent及无关服务默认置于排除profile；不宣称Agent启用态通过。邮件显式MAIL_TYPE空，角色测试example.invalid，禁止向真人发送邀请。

## 私有配置与镜像

运行执行者把`private.env.template`复制到`/private/tmp`等未跟踪受控路径，填本地内部服务密钥及实际镜像，不把值写到这里。新内部服务密钥属于专用本地测试配置；用户外部provider密钥保持用户配置，不读取/记录或移入模板。

API image必须来自`/private/tmp/difyplus-acceptance-api-20260930/final-build-manifest.json`最终归档（含四模型UUID修复），且构建完成后记录实际image ID/source hashes才可起API/worker/init/migration。当前构建中的中间镜像不能代替最终candidate。Web可使用实际验证且源码/产物hash不变的完整fork image，仍须记录精确身份；API/Web不能换官方镜像。plugin0.6.10-local/Squid若用本地演练替代来源须明确记录供货限制。

`validate-config.py`只用validation-only占位值，不读真实private env、不启动容器；静态验证config、named-volume、host/ports、WS和队列。占位值不可用于实际运行。

## 顺序（由运行执行者操作）

下面命令第一参数`/absolute/private.env`由执行者换成自己的实际私有文件路径。wrapper固定base/overlay/project/profiles；不得不带overlay执行base，不得使用裸`up`启动全部服务，也不得`down -v`清理数据。

```bash
bash compose.sh /absolute/private.env config --quiet
bash compose.sh /absolute/private.env up -d db_postgres db_mysql redis qdrant ssrf_proxy sandbox
```

此时只允许独立middleware起栈，无迁移/API/业务写入。核对服务健康、项目volumes/network和ports，禁止完整config/env/stdout泄露密钥。特别确认旧23000栈未被修改。完整最终image可用并读回image/hash后，独立完成storage权限和SECRET_KEY seed，才做串行双链迁移：

```bash
bash compose.sh /absolute/private.env up --no-deps --abort-on-container-exit --exit-code-from init_permissions init_permissions
bash compose.sh /absolute/private.env up --no-deps --abort-on-container-exit --exit-code-from init_secret_key init_secret_key
bash compose.sh /absolute/private.env --profile migration up --no-deps --abort-on-container-exit --exit-code-from migration migration
bash compose.sh /absolute/private.env --profile acceptance-mysql-migration up --no-deps --abort-on-container-exit --exit-code-from migration_mysql migration_mysql
```

逐步记录exit code与DB heads：PG、MySQL分别目标主链`c3f1a9b2e6d4`/扩展`020_workflow_run_account`；失败保留现场并停止后继，不盲目stamp/retry。PG与MySQL独立volume/database，默认业务UI使用PG；MySQL ORM/probe/center smoke由执行者明确连接mysql_probe的MySQL配置，不复用用户数据库。migration与mysql_probe为独立profile，普通业务启动不执行它们。

双链与隔离审计通过再起明确业务服务，beat继续关闭：

```bash
bash compose.sh /absolute/private.env up -d --no-deps plugin_daemon api api_websocket web worker worker-gaia worker-dataset nginx
```

实际观察WebSocket101/真实点击、active_queues与注册task/实际投递消费，完整image/source hashes、plugin/KB入库/检索和业务账目。在专用浏览器TaskSpace用localhost入口新建测试workspace/角色账户；不复用127.0.0.1 cookie或原用户model配置。真实provider启用配置由用户/已授权安全方式提供，不能凭本包新空库推断原provider凭据已经存在。

完整业务批次见`../local-acceptance-batches-2026-09-30.md`。独立恢复前暂停beat/触发和全部写入者，对named volumes及DB做同一恢复点备份；不在本包写含密码的dump命令或读取完整队列payload。旧holding任务属于原23000独立Redis，不能迁移、清空或重投它们。新安装恢复与原O04旧版恢复分别记录。

启用隔离 Agent 时，使用同wrapper的 `--profile agent` 并仅在0600私有env填合成内部 server/API/sandbox 字段，字段与格式见 `private.env.template` 及 `../config-output-scope/agent-private-config-contract.json`。Agent inner key明确共用 `LOCAL_ACCEPTANCE_PLUGIN_INNER_KEY`，匹配API实际 `INNER_API_KEY_FOR_PLUGIN`；不要用base Compose输入别名的相等性代替loaded配置检查。此内部传输配置不提供外部模型credentials，真实Agent生成仍须合法model/tool配置及实际运行验收。
