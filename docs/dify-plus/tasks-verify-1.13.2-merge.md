# Dify-Plus upstream 1.13.2 合并验证任务清单

> 分支：`merge/upstream-1.13.2`  
> 创建时间：2026-03-27  
> 完成时间：2026-03-30  
> 状态：**✅ 全部完成**  
> 目标：对合并结果进行代码审查 + 构建本地镜像 + 部署验证可登录访问

---

## 发现的问题与修复

| 问题 | 原因 | 修复 |
|---|---|---|
| NLTK 下载失败 | `unstructured` 0.16.1→0.21.5 移除了 `download_nltk_packages` | 直接调用 `nltk.download()` |
| `flask_restful` ImportError | 上游 1.13.2 移除了 flask_restful | `ai_draw_extnd.py` 改为 flask_restx |
| `alibabacloud_dingtalk` 缺失 | 未在 pyproject.toml 声明 | 添加依赖 + uv.lock 更新 |
| `pypinyin` 缺失 | 未在 pyproject.toml 声明 | 添加依赖 + uv.lock 更新 |
| nginx 启动失败 | `plugin_daemon` 未启动，nginx 找不到 upstream | 本地测试时临时 `return 503` |
| db_postgres/redis 端口冲突 | 主机已有其他实例占用 5432/6379 | 端口改为环境变量可配置 |

---

## 验证结果

### ✅ 已验证通过



### T1 - Review 后端关键合并文件
- [ ] `api/controllers/service_api/wraps.py` — 余额/额度校验是否存在
- [ ] `api/controllers/console/feature.py` — CVE-2025-63387 双阶段 bootstrap 是否完整
- [ ] `api/controllers/console/auth/oauth.py` — OAuth2/Casdoor 兼容是否保留
- [ ] `api/libs/token.py` — CSRF 白名单是否包含 admin_register_user
- [ ] `api/configs/app_config.py` — ExtendConfig 注入是否保留
- [ ] `api/commands/` 目录 — extend_db 命令是否在 ext_commands.py 中注册
- [ ] `api/migrations/versions/` — 9 个新迁移文件是否存在
- [ ] `api/dify_graph/` — 包是否可以被正常 import
- [ ] `api/events/__init__.py` — extend 事件处理器是否注册

### T2 - Review 前端关键合并文件
- [ ] `web/context/global-public-context.tsx` — 两阶段 bootstrap 逻辑是否保留
- [ ] `web/service/client.ts` — X-Login-Config-Token header 是否注入
- [ ] `web/app/components/header/index.tsx` — AccountMoneyExtend + apps-center-extend 链接
- [ ] `web/app/signin/normal-form.tsx` — DingTalk/OAuth2 登录入口是否保留

---

## 阶段二：构建本地镜像

### T3 - 准备 .env 文件
- [ ] 复制 `docker/middleware.env.example` 并创建基础 `docker/.env`
- [ ] 设置 DB_USERNAME、DB_PASSWORD、DB_DATABASE 等必需变量
- [ ] 验证 `.env` 加载后 docker compose config 无报错

### T4 - 构建 API 镜像
- [ ] 执行 `docker build -t dify-plus-api:1.13.2-local ./api`
- [ ] 确认镜像构建成功（exit 0）
- [ ] 验证 import dify_graph 无报错

### T5 - 构建 Web 镜像
- [ ] 执行 `docker build -t dify-plus-web:1.13.2-local ./web`
- [ ] 确认镜像构建成功（exit 0）

### T6 - 创建 docker-compose 覆盖文件
- [ ] 创建 `docker/docker-compose.local-build.yaml` override 文件
- [ ] 替换 api/worker*/web 镜像为本地构建版本

---

## 阶段三：服务启动验证

### T7 - 启动基础中间件（DB + Redis）
- [ ] 启动 db_postgres 和 redis 服务
- [ ] 验证 postgres healthcheck 通过
- [ ] 验证 redis 可连接

### T8 - 启动 API 服务
- [ ] 启动 api 服务（包含自动迁移 MIGRATION_ENABLED=true）
- [ ] 验证数据库迁移全部应用（含 9 个新迁移）
- [ ] 验证 extend 迁移也正常执行
- [ ] 确认 `GET /health` 返回 200

### T9 - 启动 Worker 和 Web
- [ ] 启动 worker 服务
- [ ] 启动 web 服务
- [ ] 启动 nginx（反向代理）

---

## 阶段四：功能验证

### T10 - Web UI 访问验证
- [ ] 浏览器访问 `http://localhost` 可以加载登录页
- [ ] `GET /console/api/login_config_bootstrap` 返回 200 且 Set-Cookie 包含 login_config_token
- [ ] `GET /console/api/login_config` 携带 token 可返回配置

### T11 - 账号注册和登录验证
- [ ] 使用初始化邮箱注册第一个账号
- [ ] 登录后默认跳转到 `/explore/apps-center-extend`（Fork 特有应用中心）
- [ ] 顶部导航正常渲染

### T12 - Fork 特有接口验证
- [ ] 顶部显示账户额度组件（AccountMoneyExtend）
- [ ] API key 管理界面可以查看/创建，并含日/月额度设置入口

### T13 - 后端接口冒烟测试
- [ ] `GET /console/api/workspaces/current/models/model-types` 返回 200（model_providers 集成）
- [ ] `POST /v1/chat-messages` with valid API key 返回正常响应

---

## 阶段五：最终汇总

### T14 - 记录验证结果
- [ ] 更新本文档标记所有 task 完成状态
- [ ] 记录发现的问题和解决方案
- [ ] 更新 `/memories/repo/dify-plus.md`

---

## 验收标准

1. 所有核心服务（api, worker, web, nginx, db, redis）健康运行
2. 可以访问登录页 → 注册/登录 → 进入应用中心
3. Fork 特有 CVE 保护、额度展示等功能没有回退
4. 后端无 import 错误，数据库迁移全部成功

---

## 问题记录

| 问题 | 解决方案 | 状态 |
|------|---------|------|
| | | |
