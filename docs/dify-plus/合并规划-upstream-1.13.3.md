# Dify-Plus 合并规划：升级至 upstream v1.13.3

> 文档状态：制定中  
> 基线：fork `merge/upstream-1.13.2`  
> 目标：upstream `1.13.3`（tag）  
> 上游提交数：214 次  
> 编写日期：2026-04-01

---

## 1. 总体评估

### 1.1 规模概览

| 维度 | 数据 |
|------|------|
| 上游 commit 数量（1.13.2 → 1.13.3） | 214 次 |
| api/ 变更文件数 | 498 个 |
| web/ 变更文件数 | 1,461 个（大量为测试重构） |
| 新增上游数据库迁移 | **0 个**（无 DB schema 变更） |
| 预计冲突高风险文件 | 3 个 |
| 整体合并难度 | ★★★☆☆（中等） |

### 1.2 与 1.13.2 的主要变化类型

| 类型 | 内容 |
|------|------|
| Bug 修复 | StreamsBroadcastChannel 并发问题、OAuth ValueError 处理、loop/iteration metadata 清除 |
| 依赖更新 | boto3 1.42.73、google-api-python-client 2.193.0、litellm 1.82.6 |
| 重构 | SQLAlchemy `.query()` → `select()` 迁移，消除 SADeprecationWarning |
| 新功能 | inner_api DSL import/export 端点、Baidu Vector DB 自动索引参数 |
| 测试 | 大量 testcontainers 迁移（web/ test 重构占主体） |
| 版本标记 | pyproject.toml version 1.13.2 → 1.13.3 |

### 1.3 核心结论

1. **无 DB 迁移**：`api/migrations/versions/` 无新文件，fork 扩展迁移链无需调整
2. **主要冲突集中在 3 个文件**：详见下方风险矩阵
3. **fork 核心功能不受影响**：计费/额度/API限额/钉钉登录/OAuth2/应用中心 等核心二开功能变更风险低

---

## 2. 冲突风险矩阵

| 文件 | 难度 | 1.13.3 变更 | Fork 改动 | 处理策略 |
|------|------|------------|----------|---------|
| `api/libs/login.py` | ★★★☆☆ | 提取 `_resolve_current_user()` 函数，改进 `login_required` 逻辑 | 移除 ResponseReturnValue，简化函数签名 | 手动合并，保留两端改动 |
| `api/controllers/console/apikey.py` | ★★★★☆ | 简化 `_get_resource()`，添加 `ApiTokenType`，迁移至 `select()` | 添加 API 密钥额度字段（二开核心） | 手动合并，确保额度字段完整保留 |
| `api/controllers/console/auth/oauth.py` | ★★★☆☆ | 添加 `urllib.parse`，`sessionmaker`，ValueError 处理 | 添加 `OaOAuth`/Casdoor 支持 | 手动合并，两端改动在不同位置 |

### 2.1 低风险变更（自动合并可能成功）

以下文件 fork 未改动，1.13.3 的变更可直接接受：
- `api/controllers/console/app/app.py` - SQLAlchemy select 重构
- `api/controllers/console/datasets/*.py` - select 重构
- `api/controllers/service_api/**/*.py` - select 重构
- `api/core/**/*.py` - 内部重构
- `api/services/**/*.py` - 非 extend 服务
- `docker/docker-compose.yaml` - 版本号 + Baidu Vector DB 参数
- `api/pyproject.toml` - 版本号 + 依赖更新

---

## 3. 执行计划

### Phase 1：创建合并分支
```bash
git checkout merge/upstream-1.13.2
git checkout -b merge/upstream-1.13.3
```

### Phase 2：执行 merge
```bash
git merge 1.13.3 --no-commit
```
- 处理自动合并的文件
- 手动解决 3 个高风险文件的冲突

### Phase 3：逐一修复冲突

#### 3.1 `api/libs/login.py`
目标：
- 保留 1.13.3 的 `_resolve_current_user()` 函数提取重构
- 保留 fork 的简化函数签名（去掉 ResponseReturnValue）
- 保留 1.13.3 `login_required` 中对 `_resolve_current_user()` 的调用

#### 3.2 `api/controllers/console/apikey.py`
目标：
- 保留 1.13.3 的 `_get_resource()` 简化（去掉重复代码）
- 保留 1.13.3 的 `ApiTokenType` 类型标注和 `select(func.count())` 重构
- **完整保留** fork 的 API 密钥额度字段（`description`、`accumulated_quota`、`day_limit_quota` 等）
- **完整保留** fork 的额度查询联合逻辑

#### 3.3 `api/controllers/console/auth/oauth.py`
目标：
- 保留 1.13.3 的 `urllib.parse` 导入和 ValueError 重定向处理
- 保留 1.13.3 的 `sessionmaker` 更改
- **完整保留** fork 的 `OaOAuth`/`OaOAuth2` Casdoor 支持

### Phase 4：后端构建验证
```bash
cd /Users/liuxingwang/go/src/dify-plus
uv run --project api python -c "from app_factory import create_app; app = create_app(); print('OK')"
uv run --project api flask db upgrade  # 验证迁移链
```

### Phase 5：前端构建验证
```bash
cd /Users/liuxingwang/go/src/dify-plus/web
pnpm build 2>&1 | tail -30
```

### Phase 6：提交与文档更新

---

## 4. 核心功能验证检查清单

合并完成后，以下二开核心功能需要验证：

### 4.1 计费与额度
- [ ] `api/controllers/console/money_extend.py` - 账号额度接口正常
- [ ] `api/controllers/service_api/wraps.py` - 额度拦截逻辑完整
- [ ] `api/models/account_money_extend.py` - 模型完整
- [ ] `api/models/api_token_money_extend.py` - API key 额度模型完整

### 4.2 API 密钥额度控制
- [ ] `api/controllers/console/apikey.py` - 保留 `description`、`*_quota` 字段
- [ ] `api/models/api_token_money_extend.py` - 模型完整
- [ ] `web/app/components/develop/secret-key/` - 前端额度设置保留

### 4.3 登录与鉴权
- [ ] `api/libs/login.py` - `login_required` 完整
- [ ] `api/libs/login_extend.py` - 扩展登录逻辑完整
- [ ] `api/controllers/console/auth/oauth.py` - Casdoor/OaOAuth 保留

### 4.4 应用中心
- [ ] `web/app/(commonLayout)/explore/apps-center-extend/` - 页面完整
- [ ] `web/app/components/explore/app-list-center-extend/` - 组件完整

### 4.5 钉钉登录
- [ ] `api/controllers/console/app/ding_talk_extend.py` - 接口完整
- [ ] `api/services/ding_talk_extend.py` - 服务完整

---

## 5. 任务执行状态

| 任务 | 状态 | 说明 |
|------|------|------|
| T1: 分析差异 + 创建规划文档 | ✅ 完成 | 本文档 |
| T2: 创建 merge/upstream-1.13.3 分支 | ⏳ 待执行 | |
| T3: 执行 git merge 1.13.3 | ⏳ 待执行 | |
| T4: 修复 login.py 冲突 | ⏳ 待执行 | |
| T5: 修复 apikey.py 冲突 | ⏳ 待执行 | |
| T6: 修复 oauth.py 冲突 | ⏳ 待执行 | |
| T7: 更新 docker-compose.dify-plus.yaml | ⏳ 待执行 | |
| T8: 后端构建验证 | ⏳ 待执行 | |
| T9: 前端构建验证 | ⏳ 待执行 | |
| T10: 提交合并结果 | ⏳ 待执行 | |
