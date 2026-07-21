# Dify-Plus

## 项目介绍

Dify-Plus 是基于 [Dify](https://github.com/langgenius/dify) 的企业级二次开发发行版。在原有 Dify 的基础上，我们补充了用户额度、密钥限额、应用中心、第三方登录、系统管理等适合企业场景的能力。这些功能原先只在我们企业内部使用，对外交流后发现很多伙伴也遇到相同的痛点，故将二开内容开源，欢迎大家一起交流。

> **1.15.0 起的重大变化**：原先基于 [gin-vue-admin](https://github.com/flipped-aurora/gin-vue-admin)（GVA）的独立管理中心（`/admin` 目录及 `admin-web`、`admin-server` 服务）已**完全移除**，管理能力全部并入 Dify Console 原生的「系统管理」模块。不再需要单独部署管理后台，也不再有第二套登录体系。详见下文[管理模式变更](#管理模式变更1150-起)。

即现在的 *Dify-Plus* = *Dify* + *企业二开功能*（含 Console 原生系统管理），单一技术栈（Next.js + Flask）。

## 分支与版本说明

| 分支 / 标签 | 上游基线 | 状态 |
| --- | --- | --- |
| `main` | Dify 1.12.1 | 稳定，含 GVA 管理后台（旧架构） |
| `1.15.0` | Dify 1.15.0 | 预发布，已完成本地容器全栈验证；GVA 管理后台已移除 |
| `1.16.0`（branch）/ `1.16.0-beta.1`（release tag） | Dify 1.16.0 | **当前 Beta 预发布基线**：后端 lint 与关键单测、前端 lint/type-check/build、全新库迁移双链与 API 冒烟已通过；类型基线债务、全量 Vitest 5 个上游快照基建失败及待执行的 UI/真实集成回归见 [1.16.0 Beta 发布说明](docs/dify-plus/1.16.0-beta发布说明.md) |

以上预发布基线均尚未合并回 `main`。生产环境如需使用，请先在自己的环境完成完整回归（参考 [上游升级与回归检查清单](docs/dify-plus/上游升级与回归检查清单.md)）。

## 名字说明

Dify-Plus，该名字不是说比 Dify 项目牛的意思，意思是想说比 Dify 多做了一些针对企业场景的二开功能而已。

## 功能介绍

### 一、Dify 二开功能

1. 新增：用户额度
   1. 对话余额限制判断
   2. 异步计算用户额度逻辑
   3. 左上角新增使用额度显示
2. 新增：密钥额度设置
   1. 应用 API 调用余额限制判断（日/月限额）
3. 新增：Web 公开页登录鉴权
   1. 支持按应用关闭：应用概览页 Web App 卡片提供「访问认证」开关（默认开启=访问需登录平台账号，关闭=任何人可匿名访问）
4. 新增：管理员同步应用到应用模板（模板中心）
5. 新增：可以鉴权的 cookie
6. 新增：应用中心页面，默认登录落点调整为应用中心
7. 新增：应用使用次数记录、应用中心按照使用次数排序
8. 新增：消息上下文控制与记忆配置
9. 权限调整
   1. 调整：不允许普通成员关闭模型
   2. 调整：空间普通成员不渲染"模型供应商"标签
   3. 调整：非管理员，隐藏密钥显示
10. [新增：钉钉登录](https://github.com/YFGaia/dify-plus/wiki/Ding%E2%80%90Talk-%E4%BD%BF%E7%94%A8%E6%96%87%E6%A1%A3)、OAuth2 登录
11. [新增：sandbox-full，以放开代码执行节点函数限制](https://github.com/YFGaia/dify-plus/wiki/Sandbox%E2%80%90full%E4%BD%BF%E7%94%A8%E6%96%87%E6%A1%A3)

### 二、系统管理（Console 原生，取代原 GVA 管理中心）

- **入口**：Console 顶部导航 →「系统管理」（仅工作空间 owner 可见）
- **能力**：
  - 系统集成：钉钉 SSO、OAuth2、邮箱 API、转发 Token（`/system-manage-extend/system-integration`）
  - 用户额度管理：查看与修改成员额度（`/system-manage-extend/quota-management`）
  - 代码执行控制：sandbox-full 授权名单（`/system-manage-extend/code-execution-control`）

## 管理模式变更（1.15.0 起）

原 GVA 管理后台的功能落点对照如下：

| 原 GVA 管理后台功能 | 1.15.0 落点 |
| --- | --- |
| 钉钉 SSO / OAuth2 / 邮箱 API / 转发 Token 配置 | Console「系统管理 → 系统集成」 |
| 用户额度查看与修改 | Console「系统管理 → 用户额度管理」 |
| 代码执行授权名单（sandbox-full） | Console「系统管理 → 代码执行控制」 |
| Dashboard 费用报表 / 密钥使用分析 | 已移除（如需运营报表可基于 Console 骨架另行扩展） |
| 租户目录、模型供应商网关、转发代理、应用版本管理 | 已移除（确认无外部依赖后随 GVA 下线） |
| 批量工作流处理 | 已移除（保留上游原生 text-generation 批量运行） |
| 后台创建用户 | 已移除（使用 Console 原生成员邀请） |

相应地，部署拓扑不再包含 `admin-web` / `admin-server` 两个服务，GVA 框架数据表（`sys_*`）由扩展迁移 `016_drop_gva_admin_tables` 清理（执行前请先备份，详见升级说明）。

## 部分功能页面展示截图

1. 应用中心

   ![应用中心.jpg](images/dify-plus/应用中心.jpg)

2. API 密钥列表（含日/月限额）

   ![API密钥列表.png](images/dify-plus/API密钥列表.png)

3. 创建 API 密钥

   ![创建API密钥.jpg](images/dify-plus/创建API密钥.jpg)

4. 用户额度显示

   ![用户额度显示.jpg](images/dify-plus/用户额度显示.jpg)

5. 同步至应用模板

   ![同步至应用模版.jpg](images/dify-plus/同步至应用模版.jpg)

6. API 调用测试

   ![API调用测试.jpg](images/dify-plus/API调用测试.jpg)

7. 钉钉登录配置

   ![image](https://github.com/user-attachments/assets/c87b315b-89aa-4bed-882f-c3243c4d9440)

## 启动方式（docker-compose）

```bash
cd docker
cp .env.example .env        # 按需修改配置
docker compose -f docker-compose.dify-plus.yaml up -d
```

- 整合编排文件为 [`docker/docker-compose.dify-plus.yaml`](docker/docker-compose.dify-plus.yaml)，使用 fork 镜像（`ccr.ccs.tencentyun.com/yfgaia/dify-plus-api` / `dify-plus-web`）。
- 1.15.0 新增工作流协作 websocket 服务 `api_websocket`（`collaboration` compose profile），相关变量：`COMPOSE_PROFILES`、`NEXT_PUBLIC_SOCKET_URL`、`NGINX_SOCKET_IO_UPSTREAM`，默认值见 [`docker/.env.example`](docker/.env.example)。
- 1.15.0 起 SSRF 代理**默认拒绝访问私有网段**：如工作流 HTTP 节点/工具需要访问内网，必须配置 `SSRF_PROXY_ALLOW_PRIVATE_IPS` / `SSRF_PROXY_ALLOW_PRIVATE_DOMAINS`。

> Wiki 中的[部署详细步骤（docker‐compose）](https://github.com/YFGaia/dify-plus/wiki/%E9%83%A8%E7%BD%B2%E8%AF%A6%E7%BB%86%E6%AD%A5%E9%AA%A4%EF%BC%88docker%E2%80%90compose%EF%BC%89)与[部署详细步骤（源码）](https://github.com/YFGaia/dify-plus/wiki/%E9%83%A8%E7%BD%B2%E8%AF%A6%E7%BB%86%E6%AD%A5%E9%AA%A4%EF%BC%88%E6%BA%90%E7%A0%81%E9%83%A8%E7%BD%B2%EF%BC%89)基于旧版本（含 GVA 管理后台），1.15.0 及之后版本请以仓库内 `docker/` 目录与升级说明为准。

## 升级说明

- 从 1.15.0 升级到 1.16.0：[升级到 1.16.0 说明](docs/dify-plus/升级到1.16.0说明.md)（两段迁移命令即可，另含登录 API 行为变化说明）。
- 从 1.12.1（或更早）升级：先读 [升级到 1.15.0 说明](docs/dify-plus/升级到1.15.0说明.md)（含 GVA 后台下线步骤、数据库迁移序列、环境变量变更），再按 1.16.0 说明继续。
- 上游升级通用流程与回归范围：[上游升级与回归检查清单](docs/dify-plus/上游升级与回归检查清单.md)。
- 数据库迁移比官方多一条扩展迁移链：`flask db upgrade`（上游）之后需执行 `flask extend_db upgrade`（fork 扩展表）。

## 版本更新说明

1. 会持续跟随 Dify 上游版本合并（当前基线：`1.16.0`）。
2. 为了标识二开的部分，我们特意在注释、文件名、方法名、表名都加上 `extend`，可通过搜索这个关键字查看我们二开的代码。
3. 完整的 fork 与上游差异、架构与运维文档见 [docs/dify-plus](docs/dify-plus/README.md)。

## 用户问的较多的问题

见文档：[Q&A](https://github.com/YFGaia/dify-plus/wiki/Q&A)

## 相关配置说明

- Dify 相关配置说明：https://docs.dify.ai/zh-hans/getting-started/install-self-hosted/environments
- fork 专有配置（额度任务开关、登录集成等）：见 [`api/configs/extend/__init__.py`](api/configs/extend/__init__.py) 与 [二开部署配置与运维说明](docs/dify-plus/二开部署配置与运维说明.md)

## Dify 原版说明

Dify 官方 README（上游 1.16.0 版本）见 [README_DIFY.md](README_DIFY.md)。

## License

本仓库遵循 [Dify Open Source License](LICENSE)（基于 Apache 2.0 附加条件）。
