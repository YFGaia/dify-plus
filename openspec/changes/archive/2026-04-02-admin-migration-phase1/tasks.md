# Tasks: Admin Migration Phase 1 — System Integration

## Backend Tasks

- [x] T1: Create permission decorator `system_admin_required_extend` in `api/controllers/console/system_manage_extend.py`
- [x] T2: Implement `SystemIntegrationManageService` in `api/services/system_manage_extend.py`
- [x] T3: Implement Controller routes (Resources) in `api/controllers/console/system_manage_extend.py`
- [x] T4: Register route in `api/controllers/console/__init__.py`

## Frontend Tasks

- [x] T5: Create route structure `web/app/(commonLayout)/system-manage-extend/`
- [x] T6: Implement admin layout component with sidebar menu
- [x] T14: Create API service `web/service/system-manage-extend.ts` and types `web/models/system-manage-extend.ts`
- [x] T7: Implement DingTalk config page
- [x] T8: Implement OAuth2 config page
- [x] T9: Implement Email API config component
- [x] T10: Implement Forward Token management component
- [x] T11: Add Header navigation entry (system-manage-nav-extend)
- [x] T12: Add i18n files (en-US + zh-Hans)

## Integration

- [x] T13: End-to-end testing and validation
