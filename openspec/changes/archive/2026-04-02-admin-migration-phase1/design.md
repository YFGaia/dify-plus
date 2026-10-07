# Design: Admin Migration Phase 1 — System Integration

## Backend Architecture

### Permission Decorator

- File: `api/controllers/console/system_manage_extend.py`
- Checks: user logged in + (workspace owner OR email in ADMIN_EMAILS config)
- Pattern: follows existing decorators in `wraps.py`

### Service Layer

- File: `api/services/system_manage_extend.py`
- Class: `SystemIntegrationManageService`
- Methods: get_config, set_config, test_dingtalk, test_oauth2, test_email_api, forward token CRUD
- Reads/writes `system_integration_extend` table via SQLAlchemy
- AppSecret: Blowfish encryption (existing pattern from `SystemIntegrationExtend.decodeSecret`)

### Controller Routes

- File: `api/controllers/console/system_manage_extend.py`
- Base: `/console/api/system-manage-extend/`
- Resources: DingTalkConfig, OAuth2Config, EmailApiTest, ForwardToken

### Route Registration

- Add import in `api/controllers/console/__init__.py`

## Frontend Architecture

### Route Structure

```
web/app/(commonLayout)/system-manage-extend/
├── layout.tsx
├── page.tsx (redirect)
└── system-integration/
    └── page.tsx (tab container with all config components)
```

### Components

- dingtalk-config.tsx: DingTalk SSO form
- oauth2-config.tsx: OAuth2 form
- email-api-config.tsx: Email API form
- forward-token-list.tsx: Token CRUD table

### API Service

- File: `web/service/system-manage-extend.ts`
- Types: `web/models/system-manage-extend.ts`

### Navigation

- New component: `web/app/components/header/system-manage-nav-extend/index.tsx`
- Added to Header, visible only for owner/admin

### i18n

- `web/i18n/en-US/system-manage-extend.ts`
- `web/i18n/zh-Hans/system-manage-extend.ts`

## Data Model

- Reuse existing `system_integration_extend` table
- classify=1: DingTalk, classify=4: OAuth2
- config column: JSON with email_api, forward_config (DingTalk) or OAuth2 URLs
