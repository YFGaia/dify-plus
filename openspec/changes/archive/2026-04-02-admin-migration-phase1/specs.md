# Specs: Admin Migration Phase 1

## API Endpoints

### DingTalk Configuration

- `GET /console/api/system-manage-extend/integration/dingtalk` — returns config with masked secrets
- `POST /console/api/system-manage-extend/integration/dingtalk` — save config, validate AppKey/AppSecret
- `GET /console/api/system-manage-extend/integration/dingtalk/test` — test connection, return auth URL
- `POST /console/api/system-manage-extend/integration/dingtalk/test-callback` — test callback with auth code

### OAuth2 Configuration

- `GET /console/api/system-manage-extend/integration/oauth2` — returns config with masked secrets
- `POST /console/api/system-manage-extend/integration/oauth2` — save config
- `POST /console/api/system-manage-extend/integration/oauth2/test` — test OAuth2 connection

### Email API

- `POST /console/api/system-manage-extend/integration/email-api/test` — test email API connectivity

### Forward Tokens

- `GET /console/api/system-manage-extend/forward-tokens` — list tokens
- `POST /console/api/system-manage-extend/forward-tokens` — create token
- `DELETE /console/api/system-manage-extend/forward-tokens/<seq>` — delete token

## Permission Model

- All endpoints require `@setup_required` + `@login_required` + `@system_admin_required_extend`
- system_admin_required_extend: workspace owner OR email in admin list

## Data Compatibility

- JSON config structure must match existing Admin Go format
- AppSecret encryption uses existing Blowfish scheme
- Masked fields use asterisk pattern from Go service
