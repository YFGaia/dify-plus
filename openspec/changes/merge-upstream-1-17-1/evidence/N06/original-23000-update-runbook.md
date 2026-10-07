# 原23000安全更新操作包（准备完成；未执行）

2026-09-30。协调员授权：新隔离localhost:23010的N01受影响发布/安装/缺stats API+browser实测及N07最新稳定source/image通过后，仅更新原API、已有workflow worker和必要gaia到同一已验证image。不得用新23010页面结果替代原用户入口交付。

## 已读取的实际现场

`original-23000-inventory.json`由只读Docker ps/inspect得到，不是旧报告推断。项目`difyplus-upstream1171-quota-selftest-20260930`。

- API和worker旧image为`difyplus-smoke-api:970b704e351f`，实际ID`sha256:5e159ddcab422a4a609e042eec9b17460e07024e03c821f93b7e06ea87647f6b`。两者唯一`/app/api`挂载为named-volume `/app/api/storage`，无source overlay；migration历史容器也无source overlay。
- API已配置`geventwebsocket.gunicorn.workers.GeventWebSocketWorker`，1worker/100connections，`MIGRATION_ENABLED=false`；已有worker只消费`workflow_based_app_execution`，并发/预取1。
- Web 23000→3000，API127.0.0.1:25432→5001；原项目无nginx，保持既有请求/WS入口。
- 四个named卷：`..._quota_selftest_postgres`、`..._quota_selftest_storage`、`..._quota_selftest_redis`、`..._quota_selftest_plugin_storage`。DB、Redis、Web、plugin imageID和挂载完整见inventory。不得改为新23010卷。
- 实际Compose基础`/Users/liuxingwang/go/src/dify-plus/docker/docker-compose.dify-plus.yaml`，再`/private/tmp/difyplus-upstream1171-smoke-20260930/override-quota-selftest.yaml`，再`/private/tmp/difyplus-performance-20260930/override-performance.yaml`；worker还加`/private/tmp/difyplus-performance-20260930/override-worker.yaml`。
- 私有环境文件只引用`/private/tmp/difyplus-upstream1171-smoke-20260930/quota-selftest.env`（0600）；overlay-quota-selftest也0600，含私有配置，禁止复制到tracked证据。三个overlay SHA分别`eedf9c9fe37721d894eca06d690f99a6f6ee59c50113f18bd1634795ff38d985`、`40518c40edac21b35c94794a9052264a704e57340db2e902f9e7e66d7a6b336a`、`4739bd8f06f471c5c9420acea38f84c2a74cf70e8a14d33299d3f3e170e5f31f`。执行前复核，不覆盖。

## 精确Compose组合

以下仅是执行者操作包，审计者未运行这些命令。`original_compose`保留四层；新最后层只定义精确镜像及必要gaia，API的WSworker class不覆盖。凭据不输出。

```bash
original_compose=(docker compose
  --project-name difyplus-upstream1171-quota-selftest-20260930
  --project-directory /Users/liuxingwang/go/src/dify-plus/docker
  --env-file /private/tmp/difyplus-upstream1171-smoke-20260930/quota-selftest.env
  -f /Users/liuxingwang/go/src/dify-plus/docker/docker-compose.dify-plus.yaml
  -f /private/tmp/difyplus-upstream1171-smoke-20260930/override-quota-selftest.yaml
  -f /private/tmp/difyplus-performance-20260930/override-performance.yaml
  -f /private/tmp/difyplus-performance-20260930/override-worker.yaml)
"${original_compose[@]}" config --quiet
```

私有`override-final-image.yaml`由runtime生成（0600），api/worker image均绑定最终已验证ID或immutable本地tag；api保留WS、原secret/DB/Redis/plugin/storage与migrations=false。gaia必须从实际原API resolved env继承同一DB/Redis/secret/plugin参数，通过私有脚本在内存复制而不是打印/手工重录；MODE=worker，queues=extend_high,extend_low，明确并发/预取，ports清空，只挂原storage、正确原networks、无新的depends_on启动。禁止照搬新23010私有env到原栈。静态比较其DB/Redis/secret/storage相同只记录boolean，验证无source overlay/未知queues。确认必要extension task registry，holding队列不在消费范围。

## 更新前恢复锚点

1. 重新保存sanitized container/image/mount/confighash清单；在权限0700私有备份目录保存原Compose所有层与env副本、旧image可回退tag/imageID、WS/worker overlays；记录sha/mode，不存tracked配置内容。`docker image save`旧API/worker image可存该私有目录，不删除本地旧image。
2. 查询当前workflow/extend active/reserved任务与broker counts；安全排空/暂停新增请求再停原API、已有worker及已有gaia（此时不存在）。不要重放或删除hold。原3debug任务仍在`difyplus_debug_hold_20260930`，TTL=-1；只记录数量及既有完整-envelope SHA一致，禁止打印payload。
3. DB使用PostgreSQL15自带`pg_dump` custom-format逐实际数据库，以及`pg_dumpall --globals-only`，凭据在容器内引用既有env；导出到私有0600文件。先由resolved config/只读SQL核对实际API/plugin数据库名，不假定默认dify。停止相关writer后形成逻辑一致快照；PG运行时禁止tar裸复制其data目录。dump list/校验和/源heads/容量记录并在新隔离恢复目标实际恢复验明可读。
4. 用户App/模型/账户/统计和quota基线只存必要count/hash、既有应用标识，provider凭据及内容不导出tracked证据。保留原storage的加密key和文件；plugin/storage复制需要对应writer暂停。Redis任务/hold要RDB或停止Redis后的冷备恢复锚点，不能只备SQL；不删除queue/卷。完整N06恢复演练单列，不将设计当passed。
5. 更新窗口禁止重新初始化账号/key或迁移用户库。三源码修复无新增schema；原API与worker `MIGRATION_ENABLED=false`，不执行init/migration服务。若实际schema差异成为首个失败，保留现场另派任务，禁止临时对原库回填stats/覆写模型App。

## 有条件更新与验收

```bash
updated_compose=("${original_compose[@]}" -f /private/tmp/difyplus-original-delivery-20260930/override-final-image.yaml)
"${updated_compose[@]}" config --quiet
"${updated_compose[@]}" up -d --no-deps --pull never api worker worker-gaia
```

最终override路径需执行者实际创建后才能使用。仅上述三服务；无`down -v`，无整个project裸up，DB/Web/plugin/Redis容器/image/卷保持。实际containerimageID、12overlay关键源码hash（无需source bind）、WSclass、queues、DB/Redis/secret同源boolean与挂载对照保存sanitized结果。

在用户原`http://127.0.0.1:23000/explore/apps-center-extend`实际查看：此前已发布且已安装的Quota smoke chat卡片出现、打开正确App。核对installed接口有对应row、缺stats容错排序、原App/模型配置基线hash、Socket.IO101/实际交互、worker/gaia正常且不消费hold。角色/全matrix主要在新栈完成；原栈只做必要低影响回归，不写用户配置或以人工回填遮盖缺陷。

## 回退

若image/source/WS/应用中心第一断点失败，先停新gaia且不删任何卷；以原四层组合和已保留旧image运行`up -d --no-deps --pull never api worker`，恢复原WS/worker配置并确认旧imageID。新gaia回退可仅stop/remove其服务容器，不删volumes/network/queue。原数据未改时只回退image/config；涉及验证样本或异常写入时，先保留失败现场，按已演练的私有DB/files/plugin/Redis共同恢复方案处理，禁止贸然覆盖用户库或恢复hold为可执行状态。记录回退结果/数据基线；无实际回退/恢复证据则N06不passed。

协调员调度更新：原23000用户页面修复不必等待无凭据的高质量KB/非零计费整个矩阵。必须先N01受影响实际功能gate与N07最新稳定candidate image通过，再按上述可回退三服务更新/原URL验收；整个N05/N06及goal不能因原页面已修即提前完成。
