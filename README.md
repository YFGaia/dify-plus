# Dify-Plus

Dify-Plus 是基于 [Dify](https://github.com/langgenius/dify) 的企业场景二开版本，额外引入了一套独立的管理后台，并在 `api/`、`web/`、`docker/` 中沉淀了额度、应用中心、第三方登录、系统集成、批量处理、部署交付等能力。

简而言之：

`Dify-Plus = Dify 主体能力 + 企业二开能力 + 管理后台`

## 当前基线

- 当前工作基线：`1.12.1`
- 上游对照基线：`langgenius/dify v1.12.1`
- 推荐保留的 Git 上游仓库：

```bash
git remote add upstream https://github.com/langgenius/dify.git
git fetch upstream --tags
```

## 仓库结构

| 目录 | 说明 |
| --- | --- |
| `api/` | Dify 后端与二开后端逻辑 |
| `web/` | Dify Web/Console 前端与二开交互 |
| `admin/` | 独立管理后台，基于 gin-vue-admin |
| `docker/` | Dify-Plus 一体化部署编排 |
| `docs/` | 架构图、与上游差异、二开功能与迁移文档 |
| `scripts/` | 文档索引/任务修复等运维脚本 |

## 主要二开能力

### Dify 主链路二开

1. 用户额度体系
   - 对话余额限制
   - API 调用额度统计
   - 左上角额度展示
   - 个人额度查询
2. API Key 限额
   - 日限额
   - 月限额
   - 密钥累计用量统计
3. 应用中心
   - 推荐应用/已安装应用
   - 按使用次数排序
   - 默认首页跳转到应用中心
4. 应用模板同步
   - 管理员同步应用到模板中心
   - 取消同步和状态回收
5. 登录与鉴权扩展
   - Web 公开页登录鉴权
   - 钉钉登录
   - OAuth2 登录
   - 更严格的登录配置 bootstrap 与 cookie/header 校验
6. 上下文与批处理
   - 消息上下文记忆
   - 工作流批量 Excel 处理
7. 权限与体验优化
   - 非管理员隐藏部分密钥信息
   - 普通成员模型权限收敛
   - CSV 编码兼容与批量请求修复

### 管理后台能力

`admin/` 目录是相对 upstream 最大的结构增量，主要提供：

1. JWT 与 Dify 打通
2. 用户同步与租户管理
3. 用户额度调整
4. 费用与使用报表
5. 模型管理与系统集成配置
6. 工作流批处理与调试工具

## 文档导航

详细文档已经整理到 `docs/`：

- [文档索引](./docs/README.md)
- [整体架构图](./docs/整体架构图.md)
- [与上游差异总表](./docs/与上游差异总表.md)
- [二开功能详解-后端与数据层](./docs/二开功能详解-后端与数据层.md)
- [二开数据库与迁移说明](./docs/二开数据库与迁移说明.md)
- [二开功能详解-Web与管理后台](./docs/二开功能详解-Web与管理后台.md)
- [二开页面与交互清单](./docs/二开页面与交互清单.md)
- [二开部署配置与运维说明](./docs/二开部署配置与运维说明.md)
- [上游升级与回归检查清单](./docs/上游升级与回归检查清单.md)

## 架构总览

![dify-plus.png](images/dify_plus.png)

系统由三层组成：

1. `web/` 提供 Dify Console、Explore、WebApp 的用户侧界面。
2. `api/` 承接 Dify 主运行时和二开业务逻辑。
3. `admin/` 提供组织级管理后台，覆盖报表、配额、系统集成与工作流批处理。

更完整的结构关系见 [整体架构图](./docs/整体架构图.md)。

## 部分页面截图

1. 应用中心

   ![应用中心.jpg](images/dify-plus/应用中心.jpg)

2. API 密钥列表

   ![API密钥列表.png](images/dify-plus/API密钥列表.png)

3. 创建 API 密钥

   ![创建API密钥.jpg](images/dify-plus/创建API密钥.jpg)

4. 用户额度显示

   ![用户额度显示.jpg](images/dify-plus/用户额度显示.jpg)

5. 同步至应用模板

   ![同步至应用模版.jpg](images/dify-plus/同步至应用模版.jpg)

6. API 调用测试

   ![API调用测试.jpg](images/dify-plus/API调用测试.jpg)

7. 个人额度修改

   ![个人额度修改.jpg](images/dify-plus/个人额度修改.jpg)

8. 费用报表

   ![费用报表.jpg.png](images/dify-plus/费用报表.png)

9. 密钥使用分析

   ![密钥使用分析.jpg](images/dify-plus/密钥使用分析.jpg)

10. 每月密钥额度花费

   ![每月密钥额度花费.jpg](images/dify-plus/每月密钥额度花费.jpg)

11. 钉钉登录配置

   ![image](https://github.com/user-attachments/assets/c87b315b-89aa-4bed-882f-c3243c4d9440)

## 启动与部署

- Docker Compose：优先参考 [二开部署配置与运维说明](./docs/二开部署配置与运维说明.md)
- 旧版 Wiki：
  - [部署详细步骤（docker-compose）](https://github.com/YFGaia/dify-plus/wiki/%E9%83%A8%E7%BD%B2%E8%AF%A6%E7%BB%86%E6%AD%A5%E9%AA%A4%EF%BC%88docker%E2%80%90compose%EF%BC%89)
  - [部署详细步骤（源码）](https://github.com/YFGaia/dify-plus/wiki/%E9%83%A8%E7%BD%B2%E8%AF%A6%E7%BB%86%E6%AD%A5%E9%AA%A4%EF%BC%88%E6%BA%90%E7%A0%81%E9%83%A8%E7%BD%B2%EF%BC%89)
  - [Q&A](https://github.com/YFGaia/dify-plus/wiki/Q&A)

## 配置说明

- Dify 官方环境变量说明：<https://docs.dify.ai/zh-hans/getting-started/install-self-hosted/environments>
- 管理后台配置：
  - 后端：<https://gin-vue-admin.com/guide/server/config.html>
  - 前端：<https://gin-vue-admin.com/guide/web/env.html>
- Dify-Plus 的新增配置与部署关系，见：
  - [二开部署配置与运维说明](./docs/二开部署配置与运维说明.md)
  - [二开数据库与迁移说明](./docs/二开数据库与迁移说明.md)

## 版本维护说明

1. 仓库持续跟随 Dify 与 gin-vue-admin 演进，但建议始终以明确 tag 作为升级基线。
2. 二开逻辑通常用 `extend` 标识，可通过搜索 `extend` 快速定位。
3. 合并上游前，建议先阅读 [与上游差异总表](./docs/与上游差异总表.md)。

## 联系我们

### email

- toxingwang@gmail.com
- 906631095@qq.com

### 微信交流

1. 微信交流群
   - Dify-plus 官方交流群 1（已满）
   - Dify-plus 官方交流群 2（已满）
   - Dify-plus 官方交流群 3（已满）
   - Dify-plus & Coze 开源交流群 4

     <img width="200" height="583" alt="image" src="https://github.com/user-attachments/assets/3ac4598c-7255-4ade-99aa-8bc0f039797e" />

2. 防止广告进群，添加微信后输入以下代码执行结果

```python
encoded_str = "5Yqg5YWlZGlmeS1wbHVz5Lqk5rWB576kMgo="
decoded_bytes = base64.b64decode(encoded_str)
decoded_str = decoded_bytes.decode("utf-8")
print(decoded_str)
```

3. 微信二维码

<img width="200" alt="image" src="https://github.com/user-attachments/assets/b5aa106e-4bd9-40d7-925f-05c3f5265ef6" />

备注：微信交流群若加不进去，可添加我们微信，我们拉你进群。

### 请作者喝咖啡

<img width="200" alt="image" src="https://github.com/user-attachments/assets/9a1ce3d4-3101-46eb-8a72-0a39db5b836b" />

### Star History

[![Star History Chart](https://api.star-history.com/svg?repos=YFGaia/dify-plus&type=Date)](https://star-history.com/#YFGaia/dify-plus&Date)

## License

版权说明：本项目在 Dify 项目基础上进行二开，需要遵守 Dify 的开源协议。

This repository is available under the [Dify Open Source License](LICENSE), which is essentially Apache 2.0 with a few additional restrictions.
