# Dify-Plus 合并执行任务清单：升级至 upstream v1.13.2

> 文档状态：执行中  
> 创建日期：2026-03-26  
> 基线：fork `main`（含 1.12.1 tag）  
> 目标：upstream `1.13.2`（tag）  
> 合并分支：`merge/upstream-1.13.2`

---

## 任务总览

| 阶段 | 任务 | 状态 | 验收 |
|------|------|------|------|
| 准备 | T0: 创建合并分支与快照 | ✅ 完成 | - |
| Phase 1 | T1: Docker/部署层配置同步 | ⬜ 待做 | docker-compose 环境变量全量 |
| Phase 2A | T2: 引入 dify_graph 新包 | ⬜ 待做 | `import dify_graph` 无报错 |
| Phase 2B | T3: commands/ 目录重构 + extend_db 迁移 | ⬜ 待做 | `extend_db --help` 可用 |
| Phase 2C | T4: 引入上游7个新迁移文件 | ⬜ 待做 | 迁移链完整 |
| Phase 2D | T5: pyproject.toml 依赖升级 | ⬜ 待做 | `uv sync` 无报错 |
| Phase 3A | T6: service_api/wraps.py 合并（最高风险） | ⬜ 待做 | 额度拦截测试通过 |
| Phase 3B | T7: web/completion.py 合并 | ⬜ 待做 | 登录/余额校验通过 |
| Phase 3C | T8: web/workflow.py 合并 | ⬜ 待做 | 工作流余额校验通过 |
| Phase 3D | T9: services/app_service.py 合并 | ⬜ 待做 | 应用中心排序正常 |
| Phase 3E | T10: console/auth/oauth.py 合并 | ⬜ 待做 | OAuth2/Casdoor 兼容 |
| Phase 3F | T11: console/feature.py 合并（CVE保护） | ⬜ 待做 | bootstrap接口正常 |
| Phase 3G | T12: libs/token.py CSRF白名单 | ⬜ 待做 | admin接口可访问 |
| Phase 3H | T13: configs/app_config.py 合并 | ⬜ 待做 | ExtendConfig注入 |
| Phase 3I | T14: workspace/model_providers.py 合并 | ⬜ 待做 | 模型同步功能 |
| Phase 4A | T15: web context global-public-context.tsx | ✅ 完成 | 两阶段bootstrap |
| Phase 4B | T16: web service/client.ts | ✅ 完成 | JWT header注入 |
| Phase 4C | T17: web header/index.tsx | ✅ 完成 | 应用中心跳转+额度 |
| Phase 4D | T18: web signin/normal-form.tsx | ✅ 完成 | 钉钉/OAuth2入口 |
| Phase 4E | T19: web app-card 同步功能 | ✅ 完成 | 同步到模板 |
| Phase 4F | T20: 前端依赖安装与构建验证 | 🔄 进行中 | pnpm build 通过 |
| Phase 5 | T21: 后端集成测试 | ⬜ 待做 | pytest 全通过 |
| Phase 5 | T22: 完整回归检查 | ⬜ 待做 | 业务回归通过 |

状态说明：✅ 完成 | 🔄 进行中 | ⚠️ 有问题 | ⬜ 待做

---

## T0: 创建合并分支与快照

**目标**：建立安全的合并工作分支，防止误操作影响 main

```bash
# 基于 main 分支创建合并分支
git checkout main
git checkout -b merge/upstream-1.13.2

# 标记快照点
git tag fork-main-pre-merge-1.13.2 HEAD

# 确认 upstream 1.13.2 tag 可用
git show 1.13.2 --stat | head -5
```

**验收**：
- [ ] 分支 `merge/upstream-1.13.2` 存在
- [ ] tag `fork-main-pre-merge-1.13.2` 存在
- [ ] 可以 `git show 1.13.2` 无报错

---

## T1: Phase 1 — Docker/部署层配置同步

**目标**：将 upstream 1.13.2 新增的环境变量同步到 `docker/docker-compose.dify-plus.yaml`

### 1.1 同步 nginx 配置

```bash
git checkout 1.13.2 -- docker/nginx/
```

### 1.2 同步 docker-compose 核心文件

对比 upstream 和 fork 的 docker-compose 差异，手动添加新增环境变量到 `docker-compose.dify-plus.yaml`

新增环境变量列表：
- `UV_CACHE_DIR`
- `REDIS_MAX_CONNECTIONS`
- `CELERY_TASK_ANNOTATIONS`
- `WORKFLOW_LOG_CLEANUP_SPECIFIC_WORKFLOW_IDS`
- `SANDBOX_EXPIRED_RECORDS_CLEAN_BATCH_MAX_INTERVAL`
- Hologres 相关配置（全套）

### 1.3 同步 .env.example 文件

```bash
git checkout 1.13.2 -- docker/.env.example
```

**验收**：
- [ ] `docker-compose.dify-plus.yaml` 包含所有上游新增环境变量
- [ ] fork 专用变量（`COOKIE_DOMAIN`、`ADMIN_*`、`GAIA_*`）保持不变
- [ ] Nginx 配置已更新

---

## T2: Phase 2A — 引入 dify_graph 新包

**目标**：引入 upstream 1.13.2 新增的 `api/dify_graph/` 包（273个文件，全量新增）

```bash
git checkout 1.13.2 -- api/dify_graph/
```

**验收**：
- [ ] `api/dify_graph/` 目录存在
- [ ] `uv run --project api python -c "import dify_graph; print('OK')"` 无报错
- [ ] import 路径映射确认（core.model_runtime vs dify_graph.model_runtime）

**注意**：需要检查 core/ 中是否有路径冲突，确认 dify_graph 是全新包还是替代包

---

## T3: Phase 2B — commands/ 目录重构

**目标**：upstream 将 `commands.py` 拆分为 `commands/` 目录；fork 需要保留 `extend_db` 命令

### 3.1 引入 upstream commands/ 目录

```bash
git checkout 1.13.2 -- api/commands/
```

### 3.2 创建 api/commands/extend.py

将 fork 的 `extend_db` 命令组和 `_run_alembic_command_extend` 从旧的 `commands.py` 迁移到新文件

### 3.3 更新 ext_commands.py

- 在 import 中添加 `extend_db`
- 在 `cmds_to_register` 末尾追加 `extend_db`
- 同时保留 upstream 新增的 `export_app_messages`

### 3.4 删除旧 commands.py

```bash
git rm api/commands.py
```

**验收**：
- [ ] `api/commands/` 目录存在且包含 extend.py
- [ ] `uv run --project api python -c "from commands import extend_db; print('OK')"`
- [ ] `api/extensions/ext_commands.py` 正确注册了 extend_db 和 export_app_messages

---

## T4: Phase 2C — 引入7个新迁移文件

**目标**：将 upstream 1.13.2 中 fork 尚未包含的7个迁移文件引入

```bash
for f in \
  "2026_01_29_1415-e8c3b3c46151_add_human_input_related_db_models.py" \
  "2026_02_09_0950-c3df22613c99_drop_server_default_for_app_trail_.py" \
  "2026_02_10_1507-f55813ffe2c8_fix_tenant_default_model_unique.py" \
  "2026_02_11_1549-fce013ca180e_fix_index_to_optimize_message_clean_job_.py" \
  "2026_02_26_1336-e288952f2994_add_partial_indexes_on_conversations_.py" \
  "2026_03_02_1805-0ec65df55790_add_indexes_for_human_input_forms.py" \
  "2026_03_04_1600-6b5f9f8b1a2c_add_user_id_to_workflow_draft_variables.py"; do
  git checkout 1.13.2 -- "api/migrations/versions/$f"
done
```

**验收**：
- [ ] 7个文件存在于 `api/migrations/versions/`
- [ ] 迁移链 revision 连续（down_revision 对应正确）
- [ ] `api/migrations_extend/` 的 base revision 不冲突

---

## T5: Phase 2D — pyproject.toml 依赖升级

**目标**：采用 upstream 1.13.2 的 pyproject.toml，更新依赖版本

```bash
git checkout 1.13.2 -- api/pyproject.toml
git checkout 1.13.2 -- api/uv.lock
```

**关键依赖变更确认**：
- celery: ~5.5.2 → ~5.6.2
- flask-migrate: ~4.0.7 → ~4.1.0
- authlib: 1.6.7 → 1.6.9

**验收**：
- [ ] `uv sync --project api` 无报错
- [ ] 所有依赖可正常安装

---

## T6: Phase 3A — service_api/wraps.py 合并（最高风险★★★★★）

**目标**：保留 fork 的额度/计费逻辑，同时采纳 upstream 的新架构

**Fork 必须保留的功能**：
1. 账号余额检查（AccountMoneyExtend.used_quota >= total_quota → AccountNoMoneyErrorExtend）
2. API Key 日额度检查（day_used_quota >= day_limit_quota → ApiTokenDayNoMoneyErrorExtend）
3. API Key 月额度检查（month_used_quota >= month_limit_quota → ApiTokenMonthNoMoneyErrorExtend）
4. `create_or_update_end_user_account_join_extend()` 函数

**合并策略**：
1. `git checkout 1.13.2 -- api/controllers/service_api/wraps.py`
2. 在 import 区域追加 fork 所需的 extend imports
3. 在 `validate_app_token` 的 `decorated_view` 函数中，在 `view_func(*args, **kwargs)` 调用前插入额度校验
4. 将 `create_or_update_end_user_account_join_extend` 函数追加到文件末尾

**测试用例**：
- 发送消息（API Key）→ 账号余额被扣减 ✓
- 设置低日限额 → 超限返回 ApiTokenDayNoMoneyErrorExtend ✓
- 设置低月限额 → 超限返回 ApiTokenMonthNoMoneyErrorExtend ✓
- 账号余额 <= 0 → 返回 AccountNoMoneyErrorExtend ✓

---

## T7: Phase 3B — web/completion.py 合并

**目标**：保留 WebApp 登录校验和余额扣减逻辑

**Fork 必须保留**：
- `is_end_login(end_user)` 函数
- `is_money_limit(end_user)` 函数
- post() 中的双重门控（登录 + 余额）
- AppGenerateServiceExtend.calculate_cumulative_usage()

**合并策略**：
1. `git checkout 1.13.2 -- api/controllers/web/completion.py`
2. 恢复 is_end_login、is_money_limit 函数定义
3. 在 post() 方法中恢复门控逻辑

---

## T8: Phase 3C — web/workflow.py 合并

**目标**：保留工作流登录/余额校验

**Fork 必须保留**：
- is_end_login、is_money_limit 调用（或直接引用 completion.py 中的函数）
- 余额超限拦截

**合并策略**：
1. `git checkout 1.13.2 -- api/controllers/web/workflow.py`
2. 追加 fork 的 extend imports
3. 在工作流运行 post() 中恢复余额校验

---

## T9: Phase 3D — services/app_service.py 合并

**目标**：保留应用中心统计注入和推荐应用列表

**Fork 必须保留**：
- AppStatisticsExtend 初始化（get_app_list 中）
- RecommendedApp 列表查询

**合并策略**：
1. `git checkout 1.13.2 -- api/services/app_service.py`

---

## 执行日志

- 2026-03-27 Phase 4A-4D 完成：确认并保留 `global-public-context.tsx` 两阶段 bootstrap、`service/client.ts` 的 `X-Login-Config-Token` 注入、header 的应用中心落点/余额展示，以及登录链路默认落点到 `/explore/apps-center-extend`；定向 ESLint 通过，仓库级 `type-check:tsgo` 因既有前端类型错误阻塞。
- 2026-03-27 Phase 4E 完成：合并 `web/app/components/explore/app-card/index.tsx` 与 `web/app/components/apps/app-card.tsx` 的上游 1.13.2 接口形态，保留二开“同步到模板/取消同步”能力；定向验证通过：`vitest run app/components/explore/app-card/index.spec.tsx` 3/3 通过，`vitest run app/components/apps/app-card.spec.tsx` 81/81 通过。
- 2026-03-27 Phase 4F 进行中：`pnpm install --frozen-lockfile` 已完成；当前 pre-commit 会执行仓库级 `type-check:tsgo`，被既有类型错误阻塞，需在完成本轮合并后统一清理或在阶段提交时使用 `--no-verify` 保留增量提交节奏。
- 2026-03-27 Phase 4F 补充：为 `web/service/explore.ts` 增加 `fetchInstalledAppList()` 返回类型后，复跑仓库级 `type-check:tsgo`，已确认本轮涉及的 `app-card` 与 `service/explore.ts` 不再出现在错误列表中；剩余错误均为仓库中其他既有问题。
2. 恢复 AppStatisticsExtend/RecommendedApp 的 import
3. 在 get_app_list() 中恢复统计初始化代码块

---

## T10: Phase 3E — console/auth/oauth.py 合并

**目标**：保留 OAuth2/Casdoor 兼容代码

**Fork 必须保留**：
- OaOAuth 类的 import
- oauth2 provider 初始化和 OAUTH_PROVIDERS 注入
- Casdoor token_from_query fallback（带 warning 日志）
- 空 token 校验

**合并策略**：
1. `git checkout 1.13.2 -- api/controllers/console/auth/oauth.py`
2. 追加 fork 的 OAuth2 相关代码
3. 安全注意：token_from_query 必须带充分日志

---

## T11: Phase 3F — console/feature.py 合并（CVE-2025-63387保护）

**目标**：保留 CVE-2025-63387 双阶段 bootstrap 保护

**Fork 必须保留**：
- `_issue_login_config_jwt()` 函数
- `_verify_login_config_token()` 函数
- `LoginConfigBootstrapApi`（/login_config_bootstrap 路由）
- `LoginConfigApi` 的 JWT 校验门控

**合并策略**：
1. `git checkout 1.13.2 -- api/controllers/console/feature.py`
2. 恢复 CVE 保护相关函数和路由

---

## T12: Phase 3G — libs/token.py CSRF白名单

**目标**：保留 `/console/api/admin_register_user` 的 CSRF 豁免

```python
# 在 CSRF_WHITE_LIST 中追加
re.compile(r"/console/api/admin_register_user"),
```

**合并策略**：
1. `git checkout 1.13.2 -- api/libs/token.py`
2. 追加上述 CSRF 白名单规则

---

## T13: Phase 3H — configs/app_config.py 合并

**目标**：保留 ExtendConfig 注入

**合并策略**：
1. `git checkout 1.13.2 -- api/configs/app_config.py`
2. 恢复 `from .extend import ExtendConfig` import
3. 恢复 DifyConfig 基类中的 ExtendConfig

---

## T14: Phase 3I — workspace/model_providers.py 合并

**目标**：保留模型同步逻辑，更新 dify_graph import 路径

**合并策略**：
1. `git checkout 1.13.2 -- api/controllers/console/workspace/model_providers.py`
2. 恢复 ModelProviderExtendService 的 import 和调用点

---

## T15: Phase 4A — web/context/global-public-context.tsx 合并

**目标**：保留 CVE-2025-63387 两阶段 bootstrap

**Fork 必须保留**：
- bootstrap → login_config 两阶段调用
- setLoginConfigToken() 调用

**合并策略**：
1. `git checkout 1.13.2 -- web/context/global-public-context.tsx`
2. 恢复两阶段 fetchSystemFeatures 逻辑

---

## T16: Phase 4B — web/service/client.ts 合并

**目标**：保留 JWT header 注入

**Fork 必须保留**：
- `loginConfigToken` 变量和 `setLoginConfigToken()`
- `getConsoleHeaders()` 函数
- consoleClient 的 headers 回调

**合并策略**：
1. `git checkout 1.13.2 -- web/service/client.ts`
2. 恢复 CVE 保护相关代码

---

## T17: Phase 4C — web/app/components/header/index.tsx 合并

**目标**：保留应用中心跳转和额度显示

**Fork 必须保留**：
- Logo href="/explore/apps-center-extend"
- `<AccountMoneyExtend />` 组件

---

## T18: Phase 4D — web/app/signin/normal-form.tsx 合并

**目标**：保留钉钉/OAuth2 登录入口

**Fork 必须保留**：
- 钉钉登录按钮（`<DingTalkAuth>`）
- OAuth2 登录按钮（`<OAuth2Auth>`）
- 登录成功后跳转 `/explore/apps-center-extend`

---

## T19: Phase 4E — web app-card 同步功能

**目标**：保留应用卡片的"同步到模板"功能

**涉及文件**：
- `web/app/components/explore/app-card/index.tsx`
- `web/app/components/apps/app-card.tsx`

---

## T20: Phase 4F — 前端依赖安装与构建验证

```bash
cd web
pnpm install
pnpm type-check
pnpm build
```

**验收**：
- [ ] pnpm build 无构建错误
- [ ] pnpm type-check 无类型错误

---

## T21: Phase 5 — 后端集成测试

```bash
# import 完整性检查
uv run --project api python -c "import dify_graph; print('dify_graph OK')"
uv run --project api python -c "from commands import extend_db; print('extend_db OK')"
uv run --project api python -c "from controllers.service_api.wraps import validate_app_token; print('wraps OK')"
uv run --project api python -c "from controllers.console.feature import LoginConfigBootstrapApi; print('feature OK')"
uv run --project api python -c "from controllers.web.completion import is_end_login; print('completion OK')"

# 运行测试
uv run --project api python -m pytest api/tests/ -x -q --tb=short
```

---

## T22: 完整回归检查

（需启动服务后验证）

### 登录认证
- [ ] Console 邮箱密码登录 → 跳转 `/explore/apps-center-extend`
- [ ] 钉钉登录（如有环境）
- [ ] OAuth2 登录（如有环境）
- [ ] `/login_config_bootstrap` → 200 + cookie

### 计费额度
- [ ] API Key 调用 → 余额扣减
- [ ] API Key 超日限额 → ApiTokenDayNoMoneyErrorExtend
- [ ] 账号余额 <= 0 → AccountNoMoneyErrorExtend
- [ ] WebApp 未登录 → WebAuthRequiredErrorExtend

### 应用中心
- [ ] 顶部 Logo → `/explore/apps-center-extend`
- [ ] 顶部额度显示正常
- [ ] 已安装应用列表正常

### 管理后台
- [ ] 额度排行榜展示
- [ ] 修改用户额度

---

## 执行日志

| 时间 | 任务 | 结果 | 备注 |
|------|------|------|------|
| 2026-03-26 | T0: 创建分支 | ✅ | 分支 merge/upstream-1.13.2 |
| 2026-03-27 | T15-T18: Phase 4A-4D 前端关键链路合并 | ✅ | 保留 bootstrap JWT header、应用中心落点、钉钉/OAuth2 登录入口 |
| 2026-03-27 | T20: 前端依赖安装与文件级校验 | 🔄 | `pnpm install --frozen-lockfile` 完成；目标文件 ESLint 仅剩 header 既有 `<img>` warning；全量 `pnpm type-check:tsgo` 仍被仓库既有错误阻塞 |
