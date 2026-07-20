# 第一阶段任务规划 — 系统集成功能迁移

> **需求编号**: AM-001  
> **阶段**: Phase 1 - 系统集成  
> **关联需求**: [specs/requirements_admin_migration.md](../requirements_admin_migration.md)

---

## 任务总览

| ID  | 任务                         | 状态    | 依赖        |
| --- | ---------------------------- | ------- | ----------- |
| T1  | 创建后端权限装饰器           | ✅ 完成 | -           |
| T2  | 实现系统集成 Service 层      | ✅ 完成 | T1          |
| T3  | 实现系统集成 Controller 路由 | ✅ 完成 | T2          |
| T4  | 注册路由到 console blueprint | ✅ 完成 | T3          |
| T5  | 创建前端路由结构             | ✅ 完成 | -           |
| T6  | 实现管理中心布局组件         | ✅ 完成 | T5          |
| T7  | 实现钉钉配置页               | ✅ 完成 | T3, T6, T14 |
| T8  | 实现 OAuth2 配置页           | ✅ 完成 | T3, T6, T14 |
| T9  | 实现邮箱 API 配置组件        | ✅ 完成 | T3, T6, T14 |
| T10 | 实现转发 Token 管理组件      | ✅ 完成 | T3, T6, T14 |
| T11 | 新增 Header 导航入口         | ✅ 完成 | T5          |
| T12 | 新增 i18n 文件               | ✅ 完成 | T7-T10      |
| T13 | 联调测试                     | ✅ 完成 | ALL         |
| T14 | 创建前端 API 服务封装        | ✅ 完成 | T3          |

---

## T1: 创建后端权限装饰器

**文件**: `api/controllers/console/system_manage_extend.py`

**设计**:

```python
def system_admin_required_extend(f):
    """
    确保当前用户有系统管理权限:
    1. 用户已登录（复用 @login_required）
    2. 用户是当前 workspace 的 owner
    3. 或用户 email 在 dify_config.ADMIN_EMAILS_EXTEND 中
    """
```

**注意事项**:

- 复用 `flask_login.current_user` 获取当前用户
- 参考 `api/controllers/console/wraps.py` 中的装饰器模式
- 检查 `TenantAccountRole` 枚举中 owner 的判定方式

---

## T2: 实现系统集成 Service 层

**文件**: `api/services/system_manage_extend.py`

**核心方法**:

```python
class SystemIntegrationManageService:
    @staticmethod
    def get_config(classify: int) -> dict:
        """读取指定分类的集成配置"""

    @staticmethod
    def set_config(classify: int, data: dict) -> None:
        """保存指定分类的集成配置"""

    @staticmethod
    def get_dingtalk_test_auth_url(config: dict, redirect_uri: str) -> str:
        """生成钉钉测试授权URL"""

    @staticmethod
    def dingtalk_test_callback(code: str) -> dict:
        """处理钉钉测试回调，返回用户信息"""

    @staticmethod
    def validate_email_api(api_url: str, api_key: str) -> bool:
        """测试邮箱API连通性"""

    @staticmethod
    def get_forward_tokens(config: dict) -> list:
        """从钉钉配置的config JSON中提取forward_config.tokens"""

    @staticmethod
    def create_forward_token(config: dict, token_data: dict) -> dict:
        """向config JSON中添加新的forward token"""

    @staticmethod
    def delete_forward_token(config: dict, seq: int) -> dict:
        """从config JSON中删除指定seq的forward token"""
```

**关键参考**:

- Go 版本: `admin/server/service/gaia/system.go`
- 已有 Python 读取逻辑: `api/services/feature_service.py` (L283)
- 模型: `api/models/system_extend.py` (SystemIntegrationExtend)

**config JSON 结构参考**:

钉钉 (classify=1):

```json
{
  "email_api": {
    "url": "https://...",
    "key": "..."
  },
  "forward_config": {
    "tokens": [{ "seq": 1, "name": "token1", "token": "xxx", "created_at": "..." }]
  }
}
```

OAuth2 (classify=4):

```json
{
  "authorize_url": "https://...",
  "token_url": "https://...",
  "userinfo_url": "https://...",
  "scope": "openid email profile",
  "button_text": "SSO 登录",
  "logout_url": "https://..."
}
```

---

## T3: 实现系统集成 Controller 路由

**文件**: `api/controllers/console/system_manage_extend.py` (同 T1 文件)

**路由定义**:

```python
# 钉钉配置
api.add_resource(DingTalkConfigExtend, '/system-manage-extend/integration/dingtalk')
api.add_resource(DingTalkTestExtend, '/system-manage-extend/integration/dingtalk/test')
api.add_resource(DingTalkTestCallbackExtend, '/system-manage-extend/integration/dingtalk/test-callback')

# OAuth2 配置
api.add_resource(OAuth2ConfigExtend, '/system-manage-extend/integration/oauth2')
api.add_resource(OAuth2TestExtend, '/system-manage-extend/integration/oauth2/test')

# 邮箱 API
api.add_resource(EmailApiTestExtend, '/system-manage-extend/integration/email-api/test')

# 转发 Token
api.add_resource(ForwardTokenListExtend, '/system-manage-extend/forward-tokens')
api.add_resource(ForwardTokenDetailExtend, '/system-manage-extend/forward-tokens/<int:seq>')
```

**每个 Resource 的方法**:

| Resource                   | GET             | POST        | DELETE    |
| -------------------------- | --------------- | ----------- | --------- |
| DingTalkConfigExtend       | 获取配置        | 保存配置    | -         |
| DingTalkTestExtend         | 获取测试授权URL | -           | -         |
| DingTalkTestCallbackExtend | -               | 测试回调    | -         |
| OAuth2ConfigExtend         | 获取配置        | 保存配置    | -         |
| OAuth2TestExtend           | -               | 测试连接    | -         |
| EmailApiTestExtend         | -               | 测试邮箱API | -         |
| ForwardTokenListExtend     | 获取列表        | 创建Token   | -         |
| ForwardTokenDetailExtend   | -               | -           | 删除Token |

---

## T4: 注册路由到 console blueprint

**文件**: `api/controllers/console/__init__.py`

**改动**:

```python
# 在文件末尾的 extend import 区域添加一行:
from controllers.console import system_manage_extend  # noqa: F401
```

---

## T5: 创建前端路由结构

**新增目录**:

```
web/app/(commonLayout)/system-manage-extend/
├── layout.tsx          # 管理中心布局
├── page.tsx            # 默认页(redirect到system-integration)
└── system-integration/
    └── page.tsx        # 系统集成主页
```

---

## T6: 实现管理中心布局组件

**文件**: `web/app/(commonLayout)/system-manage-extend/layout.tsx`

**设计**:

- 左侧菜单栏，列出各管理模块
- 右侧为内容区 (children)
- 参考 Dify 现有的 account-setting 面板布局风格
- 权限检查：非 owner 重定向到首页

**左侧菜单数据结构**:

```typescript
const menuItems = [
  {
    group: '系统集成',
    items: [
      {
        key: 'system-integration',
        label: t('systemManage.integration'),
        href: '/system-manage-extend/system-integration',
      },
    ],
  },
  // 后续阶段扩展...
]
```

---

## T7: 实现钉钉配置页

**组件**: `dingtalk-config.tsx`

**表单字段**:

| 字段       | 类型           | 对应 DB 列/JSON |
| ---------- | -------------- | --------------- |
| 启用状态   | Switch         | `status`        |
| Corp ID    | Input          | `corp_id`       |
| Agent ID   | Input          | `agent_id`      |
| App Key    | Input          | `app_key`       |
| App Secret | Password Input | `app_secret`    |

**按钮**:

- 保存配置 → POST /integration/dingtalk
- 测试连接 → GET /integration/dingtalk/test → 打开弹窗显示授权URL

**参考**: `admin/web/src/view/systemIntegrated/dingTalk/index.vue` 的表单布局和交互流程

---

## T8: 实现 OAuth2 配置页

**组件**: `oauth2-config.tsx`

**表单字段**:

| 字段          | 类型           | 对应 DB 列/JSON        |
| ------------- | -------------- | ---------------------- |
| 启用状态      | Switch         | `status`               |
| Client ID     | Input          | `app_id`               |
| Client Secret | Password Input | `app_secret`           |
| Authorize URL | Input          | `config.authorize_url` |
| Token URL     | Input          | `config.token_url`     |
| UserInfo URL  | Input          | `config.userinfo_url`  |
| Scope         | Input          | `config.scope`         |
| 按钮文字      | Input          | `config.button_text`   |
| 登出 URL      | Input          | `config.logout_url`    |

**按钮**:

- 保存配置 → POST /integration/oauth2
- 测试连接 → POST /integration/oauth2/test

**参考**: `admin/web/src/view/systemIntegrated/oauth2/index.vue`

---

## T9: 实现邮箱 API 配置组件

**组件**: `email-api-config.tsx`

**表单字段**:

| 字段     | 类型           |
| -------- | -------------- |
| API 地址 | Input          |
| API 密钥 | Password Input |

**按钮**:

- 测试连接 → POST /integration/email-api/test
- (保存随钉钉配置一起在 config JSON 中)

---

## T10: 实现转发 Token 管理组件

**组件**: `forward-token-list.tsx`

**功能**:

- Token 列表 (表格: 名称、Token值、创建时间、操作)
- 新增 Token (弹窗表单: 名称)
- 删除 Token (确认弹窗)

---

## T11: 新增 Header 导航入口

**新增文件**: `web/app/components/header/system-manage-nav-extend/index.tsx`

**修改文件**: Header 组件中引入并渲染该导航项

**设计**:

- 图标: 齿轮/设置图标
- 仅 owner/admin 角色可见
- 点击跳转 `/system-manage-extend/system-integration`

---

## T12: 新增 i18n 文件

**新增文件**:

- `web/i18n/en-US/system-manage-extend.json`
- `web/i18n/zh-Hans/system-manage-extend.json`

**Key 结构**:

```json
{
  "title": "系统管理",
  "integration": {
    "title": "系统集成",
    "dingtalk": { "title": "钉钉单点登录", ... },
    "oauth2": { "title": "OAuth2.0 集成", ... },
    "emailApi": { "title": "邮箱 API", ... },
    "forwardToken": { "title": "转发 Token", ... }
  },
  "common": {
    "save": "保存",
    "test": "测试连接",
    "enable": "启用",
    "disable": "禁用"
  }
}
```

---

## T13: 联调测试

**测试清单**:

- [ ] 后端 API 单元测试（Service 层 CRUD + 测试连接 mock）
- [ ] 前端页面访问正常
- [ ] 权限控制有效（非 owner 不可访问）
- [ ] 钉钉配置 CRUD 正常
- [ ] OAuth2 配置 CRUD 正常
- [ ] 配置保存后登录页效果正确
- [ ] 转发 Token 增删有效
- [ ] i18n 中英文切换正常

---

## T14: 创建前端 API 服务封装

**文件**: `web/service/system-manage-extend.ts`

```typescript
// 钉钉相关
export const getDingTalkConfig = () => get('/system-manage-extend/integration/dingtalk')
export const setDingTalkConfig = (data) =>
  post('/system-manage-extend/integration/dingtalk', { body: data })
export const getDingTalkTestAuthUrl = () => get('/system-manage-extend/integration/dingtalk/test')
export const dingtalkTestCallback = (data) =>
  post('/system-manage-extend/integration/dingtalk/test-callback', { body: data })

// OAuth2 相关
export const getOAuth2Config = () => get('/system-manage-extend/integration/oauth2')
export const setOAuth2Config = (data) =>
  post('/system-manage-extend/integration/oauth2', { body: data })
export const testOAuth2Connection = (data) =>
  post('/system-manage-extend/integration/oauth2/test', { body: data })

// 邮箱 API
export const testEmailApi = (data) =>
  post('/system-manage-extend/integration/email-api/test', { body: data })

// 转发 Token
export const getForwardTokens = () => get('/system-manage-extend/forward-tokens')
export const createForwardToken = (data) =>
  post('/system-manage-extend/forward-tokens', { body: data })
export const deleteForwardToken = (seq) => del(`/system-manage-extend/forward-tokens/${seq}`)
```

**类型定义文件**: `web/models/system-manage-extend.ts`

```typescript
export interface DingTalkConfig {
  status: boolean
  corp_id: string
  agent_id: string
  app_key: string
  app_secret: string
  email_api?: { url: string; key: string }
  forward_config?: { tokens: ForwardToken[] }
}

export interface OAuth2Config {
  status: boolean
  app_id: string
  app_secret: string
  authorize_url: string
  token_url: string
  userinfo_url: string
  scope: string
  button_text: string
  logout_url: string
}

export interface ForwardToken {
  seq: number
  name: string
  token: string
  created_at: string
}
```
