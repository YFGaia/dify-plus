# T1000 Dify / Dify Plus 工作记录

更新时间：2026-06-17

本文档用于沉淀 T1000 服务器上 Dify、Dify Plus、MinerU 文档解析流水线、模型服务和权限验证相关工作。原始 Dify README 保留在本文档后半部分。

## 1. 部署现状

- 旧版官方 Dify 保留在 `/home/yu/projects/dify-rag/docker`，当前不直接改动，作为稳定环境继续保留。
- Dify Plus 采用旁路部署方式，目录为 `/home/yu/projects/dify-plus/docker`，Compose 文件为 `docker-compose.dify-plus.yaml`，容器名前缀为 `difyplus_sidecar-*`。
- Dify Plus 对外访问地址：`http://118.196.65.83:18090`。
- Dify Plus 管理中心地址：`http://118.196.65.83:18091/admin/login`。
- 管理中心曾出现无法进入的问题，原因是 admin-web 前端请求 `/admin/api/...`，但 nginx 只代理了 `/api/...`。已通过 `docker/admin-web/my.conf` 增加 `/admin/api` 代理，并在 Compose 中为 `admin-web` 挂载该配置。

## 2. Dify Plus 迁移和基础验证

已在 Dify Plus 中复刻旧 Dify 的主要应用和知识库，便于对比 Plus 版本能力。

已迁移或复刻的应用：

- 超级大脑
- 专属智能客服
- 每日新闻摘要-国内
- 知识库2 + 聊天机器人
- 知识库 + 聊天机器人3

已迁移或复刻的知识库：

- 企业演示知识库
- 部门测试知识库2
- Convert to Markdown 1
- Untitled 1
- MinerU 中文文档知识库

迁移过程中已处理文件、向量数据、模型供应商凭据、租户密钥和 Plus `SECRET_KEY` 相关问题，并验证本地 Embedding 与 Rerank 服务可被 Plus 容器访问，包括 Ollama `bge-m3` 和 Xinference `bge-reranker-v2-m3`。

## 3. MinerU 文档解析流水线

目标是在 Dify 中通过流水线创建知识库：先调用自部署 MinerU 服务完成文档解析，再进入常规知识库流程，包括分段、Embedding、Rerank 和检索。

已经完成或处理的问题：

- 确认可以走自部署 MinerU 服务，而不是只依赖外部插件形态。
- 按中文文档优先场景配置 Embedding 与 Rerank 模型。
- 将流水线参数从纯英文调整为中文 + 英文的表达方式，并补充默认值。
- 修复默认值只显示但不提交的问题，避免用户必须手动重新输入数字才能进入下一步。
- 分析默认一次最多上传 5 个文件的限制，判断该限制主要是为了控制解析、队列、存储、Embedding 和 Rerank 阶段的资源压力。
- 处理 DOC 等非标准输入格式的体验问题，方向是由流水线前置兼容或转换，而不是要求上传者手动预处理。
- 排查 DOC 文件长时间排队问题，并结合队列、Worker 并发和资源消耗分析处理能力上限。
- 解释 MinerU 是否使用多核：取决于服务自身 Worker、进程数和并发配置，不等于默认无限制占满所有 CPU。

## 4. 资源与运行观察

已围绕 Dify、MinerU、Embedding 和 Rerank 做过资源消耗观察，重点关注：

- 上传文件数量增加时，MinerU 解析阶段对 CPU、内存和队列的影响。
- 文档解析后进入知识库流程时，Embedding 与 Rerank 对本地小模型服务的调用压力。
- Dify Plus 旁路部署不应影响旧 Dify 的稳定运行。

后续如果要提高一次上传数量，建议先做小步压测，再调整上传上限、Worker 并发、队列超时、容器资源限制和模型服务并发，避免解析阶段和向量化阶段互相拖垮。

## 5. 账号、角色和权限验证

Dify / Dify Plus 当前不是完整的任意 RBAC 系统。已确认：

- 工作区角色主要是固定角色，例如 `owner`、`admin`、`editor`、`normal`、`dataset_operator`。
- 知识库可以通过 `dataset_permissions` 做部分成员授权。
- 应用级“只看某几个应用”的能力不是官方默认完整能力，本次为了测试做了账号级白名单补丁。
- 模型权限没有完整的逐用户模型白名单，本次主要通过额度方式做近似验证。

已创建的测试账号，密码以本机部署环境记录为准，不写入仓库：

- `t1000-admin@test.local`：管理员角色，可访问全部 5 个知识库，额度 100000。
- `t1000-editor@test.local`：编辑角色，知识库范围为企业演示知识库、Convert to Markdown 1、MinerU 中文文档知识库，额度 20000。
- `t1000-kb@test.local`：知识库操作角色，知识库范围为部门测试知识库2、MinerU 中文文档知识库，额度 10000。
- `t1000-normal@test.local`：普通角色，仅企业演示知识库，额度 1000。
- `t1000-limited@test.local`：普通角色，额度设置为已耗尽，用于额度限制测试。
- `t1000-twoapps@test.local`：普通角色，用于“只能使用两个应用”的测试。

5 个知识库已调整为 `partial_members`，并保留 owner 的完整访问能力。

## 6. 应用可见性和应用中心问题

用户希望验证“一个账号只能看到两个应用”的效果。Dify 默认工作室应用列表和探索 / 应用中心不是同一套逻辑，因此分别做了处理。

当前给 `t1000-editor@test.local` 和 `t1000-twoapps@test.local` 配置的两个可见应用：

- 超级大脑
- 专属智能客服

已修改和验证的逻辑：

- 工作室应用列表过滤：`api/services/app_service.py`
- 应用详情访问拦截：`api/controllers/console/app/wraps.py`
- 应用中心推荐 / 已安装应用空指针修复和账号过滤：`api/services/recommended_app_service_extend.py`
- 探索页已安装应用过滤：`api/controllers/console/explore/installed_app.py`

曾遇到的问题：

- “探索 / 应用中心”显示没有 Web Apps。原因是它不是工作室应用列表，而是使用已安装应用 / 推荐应用数据。
- `/console/api/installed/apps` 曾出现 500，原因是扩展代码假设 `installed_app` 一定存在，实际可能为空。已增加空值跳过逻辑。
- 为 `t1000-twoapps@test.local` 补充了两个应用的 installed / recommended 记录，并将相关应用设为可公开和启用站点。

注意：当前应用白名单是测试级硬编码补丁，适合验证效果；如果后续要长期使用，应改造成数据库配置或管理后台可配置能力。

## 7. 当前代码改动摘要

与本次 Dify Plus 验证相关的主要改动：

- `docker/admin-web/my.conf`：修复管理中心 `/admin/api` 代理。
- `docker/docker-compose.dify-plus.yaml`：为 `admin-web` 增加 nginx 配置挂载。
- `api/services/app_service.py`：增加指定账号的工作室应用列表白名单过滤。
- `api/controllers/console/app/wraps.py`：增加指定账号访问非白名单应用详情时的拦截。
- `api/services/recommended_app_service_extend.py`：修复已安装应用空指针问题，并增加指定账号应用中心过滤。
- `api/controllers/console/explore/installed_app.py`：增加探索页已安装应用的账号级过滤。

## 8. 后续建议

- 将账号级应用白名单从代码常量迁移到数据库配置，避免以后每次调整账号和应用都要改代码。
- 为应用中心、工作室应用列表和 API 访问统一一套权限判断逻辑，避免前端显示与后端访问不一致。
- 对 MinerU 流水线做一次包含 PDF、DOC、DOCX、图片 PDF 和扫描件的批量压测，记录不同文件类型下的耗时、CPU、内存、队列等待和失败率。
- 如果要突破默认 5 文件上传限制，应同步调整队列、Worker 并发、模型服务并发和超时配置，并保留回退方案。
- 对 Dify Plus 的二开增强能力继续做对比测试，重点看管理中心、权限体系、应用中心、知识库权限和计费 / 额度模块是否稳定。

---

# Upstream Dify README

![cover-v5-optimized](https://github.com/langgenius/dify/assets/13230914/f9e19af5-61ba-4119-b926-d10c4c06ebab)

<p align="center">
  📌 <a href="https://dify.ai/blog/introducing-dify-workflow-file-upload-a-demo-on-ai-podcast">Introducing Dify Workflow File Upload: Recreate Google NotebookLM Podcast</a>
</p>

<p align="center">
  <a href="https://cloud.dify.ai">Dify Cloud</a> ·
  <a href="https://docs.dify.ai/getting-started/install-self-hosted">Self-hosting</a> ·
  <a href="https://docs.dify.ai">Documentation</a> ·
  <a href="https://udify.app/chat/22L1zSxg6yW1cWQg">Enterprise inquiry</a>
</p>

<p align="center">
    <a href="https://dify.ai" target="_blank">
        <img alt="Static Badge" src="https://img.shields.io/badge/Product-F04438"></a>
    <a href="https://dify.ai/pricing" target="_blank">
        <img alt="Static Badge" src="https://img.shields.io/badge/free-pricing?logo=free&color=%20%23155EEF&label=pricing&labelColor=%20%23528bff"></a>
    <a href="https://discord.gg/FngNHpbcY7" target="_blank">
        <img src="https://img.shields.io/discord/1082486657678311454?logo=discord&labelColor=%20%235462eb&logoColor=%20%23f5f5f5&color=%20%235462eb"
            alt="chat on Discord"></a>
    <a href="https://reddit.com/r/difyai" target="_blank">  
        <img src="https://img.shields.io/reddit/subreddit-subscribers/difyai?style=plastic&logo=reddit&label=r%2Fdifyai&labelColor=white"
            alt="join Reddit"></a>
    <a href="https://twitter.com/intent/follow?screen_name=dify_ai" target="_blank">
        <img src="https://img.shields.io/twitter/follow/dify_ai?logo=X&color=%20%23f5f5f5"
            alt="follow on X(Twitter)"></a>
    <a href="https://www.linkedin.com/company/langgenius/" target="_blank">
        <img src="https://custom-icon-badges.demolab.com/badge/LinkedIn-0A66C2?logo=linkedin-white&logoColor=fff"
            alt="follow on LinkedIn"></a>
    <a href="https://hub.docker.com/u/langgenius" target="_blank">
        <img alt="Docker Pulls" src="https://img.shields.io/docker/pulls/langgenius/dify-web?labelColor=%20%23FDB062&color=%20%23f79009"></a>
    <a href="https://github.com/langgenius/dify/graphs/commit-activity" target="_blank">
        <img alt="Commits last month" src="https://img.shields.io/github/commit-activity/m/langgenius/dify?labelColor=%20%2332b583&color=%20%2312b76a"></a>
    <a href="https://github.com/langgenius/dify/" target="_blank">
        <img alt="Issues closed" src="https://img.shields.io/github/issues-search?query=repo%3Alanggenius%2Fdify%20is%3Aclosed&label=issues%20closed&labelColor=%20%237d89b0&color=%20%235d6b98"></a>
    <a href="https://github.com/langgenius/dify/discussions/" target="_blank">
        <img alt="Discussion posts" src="https://img.shields.io/github/discussions/langgenius/dify?labelColor=%20%239b8afb&color=%20%237a5af8"></a>
</p>

<p align="center">
  <a href="./README.md"><img alt="README in English" src="https://img.shields.io/badge/English-d9d9d9"></a>
  <a href="./README_CN.md"><img alt="简体中文版自述文件" src="https://img.shields.io/badge/简体中文-d9d9d9"></a>
  <a href="./README_JA.md"><img alt="日本語のREADME" src="https://img.shields.io/badge/日本語-d9d9d9"></a>
  <a href="./README_ES.md"><img alt="README en Español" src="https://img.shields.io/badge/Español-d9d9d9"></a>
  <a href="./README_FR.md"><img alt="README en Français" src="https://img.shields.io/badge/Français-d9d9d9"></a>
  <a href="./README_KL.md"><img alt="README tlhIngan Hol" src="https://img.shields.io/badge/Klingon-d9d9d9"></a>
  <a href="./README_KR.md"><img alt="README in Korean" src="https://img.shields.io/badge/한국어-d9d9d9"></a>
  <a href="./README_AR.md"><img alt="README بالعربية" src="https://img.shields.io/badge/العربية-d9d9d9"></a>
  <a href="./README_TR.md"><img alt="Türkçe README" src="https://img.shields.io/badge/Türkçe-d9d9d9"></a>
  <a href="./README_VI.md"><img alt="README Tiếng Việt" src="https://img.shields.io/badge/Ti%E1%BA%BFng%20Vi%E1%BB%87t-d9d9d9"></a>
</p>


Dify is an open-source LLM app development platform. Its intuitive interface combines agentic AI workflow, RAG pipeline, agent capabilities, model management, observability features and more, letting you quickly go from prototype to production.

## Quick start
> Before installing Dify, make sure your machine meets the following minimum system requirements:
>
>- CPU >= 2 Core
>- RAM >= 4 GiB

</br>

The easiest way to start the Dify server is through [docker compose](docker/docker-compose.yaml). Before running Dify with the following commands, make sure that [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/) are installed on your machine:

```bash
cd dify
cd docker
cp .env.example .env
docker compose up -d
```

After running, you can access the Dify dashboard in your browser at [http://localhost/install](http://localhost/install) and start the initialization process.

#### Seeking help
Please refer to our [FAQ](https://docs.dify.ai/getting-started/install-self-hosted/faqs) if you encounter problems setting up Dify. Reach out to [the community and us](#community--contact) if you are still having issues.

> If you'd like to contribute to Dify or do additional development, refer to our [guide to deploying from source code](https://docs.dify.ai/getting-started/install-self-hosted/local-source-code)

## Key features
**1. Workflow**:
Build and test powerful AI workflows on a visual canvas, leveraging all the following features and beyond.


https://github.com/langgenius/dify/assets/13230914/356df23e-1604-483d-80a6-9517ece318aa



**2. Comprehensive model support**:
Seamless integration with hundreds of proprietary / open-source LLMs from dozens of inference providers and self-hosted solutions, covering GPT, Mistral, Llama3, and any OpenAI API-compatible models. A full list of supported model providers can be found [here](https://docs.dify.ai/getting-started/readme/model-providers).

![providers-v5](https://github.com/langgenius/dify/assets/13230914/5a17bdbe-097a-4100-8363-40255b70f6e3)


**3. Prompt IDE**:
Intuitive interface for crafting prompts, comparing model performance, and adding additional features such as text-to-speech to a chat-based app.

**4. RAG Pipeline**:
Extensive RAG capabilities that cover everything from document ingestion to retrieval, with out-of-box support for text extraction from PDFs, PPTs, and other common document formats.

**5. Agent capabilities**:
You can define agents based on LLM Function Calling or ReAct, and add pre-built or custom tools for the agent. Dify provides 50+ built-in tools for AI agents, such as Google Search, DALL·E, Stable Diffusion and WolframAlpha.

**6. LLMOps**:
Monitor and analyze application logs and performance over time. You could continuously improve prompts, datasets, and models based on production data and annotations.

**7. Backend-as-a-Service**:
All of Dify's offerings come with corresponding APIs, so you could effortlessly integrate Dify into your own business logic.


## Using Dify

- **Cloud </br>**
  We host a [Dify Cloud](https://dify.ai) service for anyone to try with zero setup. It provides all the capabilities of the self-deployed version, and includes 200 free GPT-4 calls in the sandbox plan.

- **Self-hosting Dify Community Edition</br>**
  Quickly get Dify running in your environment with this [starter guide](#quick-start).
  Use our [documentation](https://docs.dify.ai) for further references and more in-depth instructions.

- **Dify for enterprise / organizations</br>**
  We provide additional enterprise-centric features. [Log your questions for us through this chatbot](https://udify.app/chat/22L1zSxg6yW1cWQg) or [send us an email](mailto:business@dify.ai?subject=[GitHub]Business%20License%20Inquiry) to discuss enterprise needs. </br>
  > For startups and small businesses using AWS, check out [Dify Premium on AWS Marketplace](https://aws.amazon.com/marketplace/pp/prodview-t22mebxzwjhu6) and deploy it to your own AWS VPC with one-click. It's an affordable AMI offering with the option to create apps with custom logo and branding.


## Staying ahead

Star Dify on GitHub and be instantly notified of new releases.

![star-us](https://github.com/langgenius/dify/assets/13230914/b823edc1-6388-4e25-ad45-2f6b187adbb4)


## Advanced Setup

If you need to customize the configuration, please refer to the comments in our [.env.example](docker/.env.example) file and update the corresponding values in your `.env` file. Additionally, you might need to make adjustments to the `docker-compose.yaml` file itself, such as changing image versions, port mappings, or volume mounts, based on your specific deployment environment and requirements. After making any changes, please re-run `docker-compose up -d`. You can find the full list of available environment variables [here](https://docs.dify.ai/getting-started/install-self-hosted/environments).

If you'd like to configure a highly-available setup, there are community-contributed [Helm Charts](https://helm.sh/) and YAML files which allow Dify to be deployed on Kubernetes.

- [Helm Chart by @LeoQuote](https://github.com/douban/charts/tree/master/charts/dify)
- [Helm Chart by @BorisPolonsky](https://github.com/BorisPolonsky/dify-helm)
- [YAML file by @Winson-030](https://github.com/Winson-030/dify-kubernetes)

#### Using Terraform for Deployment

Deploy Dify to Cloud Platform with a single click using [terraform](https://www.terraform.io/)

##### Azure Global
- [Azure Terraform by @nikawang](https://github.com/nikawang/dify-azure-terraform)

##### Google Cloud
- [Google Cloud Terraform by @sotazum](https://github.com/DeNA/dify-google-cloud-terraform)

#### Using AWS CDK for Deployment

Deploy Dify to AWS with [CDK](https://aws.amazon.com/cdk/)

##### AWS
- [AWS CDK by @KevinZhao](https://github.com/aws-samples/solution-for-deploying-dify-on-aws)

## Contributing

For those who'd like to contribute code, see our [Contribution Guide](https://github.com/langgenius/dify/blob/main/CONTRIBUTING.md).
At the same time, please consider supporting Dify by sharing it on social media and at events and conferences.


> We are looking for contributors to help with translating Dify to languages other than Mandarin or English. If you are interested in helping, please see the [i18n README](https://github.com/langgenius/dify/blob/main/web/i18n/README.md) for more information, and leave us a comment in the `global-users` channel of our [Discord Community Server](https://discord.gg/8Tpq4AcN9c).

## Community & contact

* [Github Discussion](https://github.com/langgenius/dify/discussions). Best for: sharing feedback and asking questions.
* [GitHub Issues](https://github.com/langgenius/dify/issues). Best for: bugs you encounter using Dify.AI, and feature proposals. See our [Contribution Guide](https://github.com/langgenius/dify/blob/main/CONTRIBUTING.md).
* [Discord](https://discord.gg/FngNHpbcY7). Best for: sharing your applications and hanging out with the community.
* [X(Twitter)](https://twitter.com/dify_ai). Best for: sharing your applications and hanging out with the community.

**Contributors**

<a href="https://github.com/langgenius/dify/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=langgenius/dify" />
</a>

## Star history

[![Star History Chart](https://api.star-history.com/svg?repos=langgenius/dify&type=Date)](https://star-history.com/#langgenius/dify&Date)


## Security disclosure

To protect your privacy, please avoid posting security issues on GitHub. Instead, send your questions to security@dify.ai and we will provide you with a more detailed answer.

## License

This repository is available under the [Dify Open Source License](LICENSE), which is essentially Apache 2.0 with a few additional restrictions.