-- ============================================================
-- 1.11.4 升级到 1.12.2 需要执行的权限 SQL
-- 包含：应用版本、模型管理 的菜单、API、Casbin 权限、角色-菜单关联
-- 执行前请确认：1) 表结构已存在 2) 若 id 冲突可调整或改用 INSERT IGNORE / ON CONFLICT
-- 模型管理 API 的 id 从 259 起，若与现有数据冲突请先查 SELECT MAX(id) FROM sys_apis; 再调整
-- ============================================================

-- --------------- 1. 菜单 sys_base_menus (应用版本) ---------------
-- 若已存在 id=41 可改为其他未占用 id，或先查 max(id)+1
INSERT INTO sys_base_menus (
  id, created_at, updated_at, deleted_at,
  menu_level, parent_id, path, name, hidden, component, sort,
  active_name, keep_alive, default_menu, title, icon, close_tab
) VALUES (
  41,
  NOW(), NOW(), NULL,
  0, 0, 'AppVersion', 'AppVersion', false, 'view/gaia/appVersion/index.vue', 10,
  '', false, false, '版本管理', 'upload-filled', false
);
-- MySQL 若主键冲突可改为: INSERT IGNORE INTO ... 或 ON DUPLICATE KEY UPDATE id=id
-- PostgreSQL: INSERT ... ON CONFLICT (id) DO NOTHING;


-- --------------- 2. API sys_apis (应用版本相关 8 条) ---------------
-- id 使用自增则可不写 id 列；以下为显式 id，请按当前库最大 id 调整，避免冲突
-- 查询当前最大 id: SELECT MAX(id) FROM sys_apis;
-- 假设当前最大为 250，则从 251 开始；否则删除 id 列让库自增
INSERT INTO sys_apis (id, created_at, updated_at, deleted_at, path, description, api_group, method) VALUES
(251, NOW(), NOW(), NULL, '/gaia/app-version/token', '获取链接Token配置', '应用版本', 'GET'),
(252, NOW(), NOW(), NULL, '/gaia/app-version/token', '设置链接Token', '应用版本', 'PUT'),
(253, NOW(), NOW(), NULL, '/gaia/app-version/releases', '版本列表', '应用版本', 'GET'),
(254, NOW(), NOW(), NULL, '/gaia/app-version/releases', '新增版本', '应用版本', 'POST'),
(255, NOW(), NOW(), NULL, '/gaia/app-version/releases/:id', '版本详情', '应用版本', 'GET'),
(256, NOW(), NOW(), NULL, '/gaia/app-version/releases/:id', '更新版本信息', '应用版本', 'PUT'),
(257, NOW(), NOW(), NULL, '/gaia/app-version/releases/:id/upload', '上传安装包(自动识别平台架构)', '应用版本', 'POST'),
(258, NOW(), NOW(), NULL, '/gaia/app-version/releases/:id/download', '删除指定平台架构包', '应用版本', 'DELETE');
-- 若希望自增 id，可改为（去掉 id 列）:
-- INSERT INTO sys_apis (created_at, updated_at, deleted_at, path, description, api_group, method) VALUES
-- (NOW(), NOW(), NULL, '/gaia/app-version/token', '获取链接Token配置', '应用版本', 'GET'),
-- ... 共 8 条;


-- --------------- 3. Casbin 规则 casbin_rule (角色 888/8881/9528/1 的接口权限) ---------------
INSERT INTO casbin_rule (ptype, v0, v1, v2) VALUES
('p', '888', '/gaia/app-version/token', 'GET'),
('p', '888', '/gaia/app-version/token', 'PUT'),
('p', '888', '/gaia/app-version/releases', 'GET'),
('p', '888', '/gaia/app-version/releases', 'POST'),
('p', '888', '/gaia/app-version/releases/:id', 'GET'),
('p', '888', '/gaia/app-version/releases/:id', 'PUT'),
('p', '888', '/gaia/app-version/releases/:id/upload', 'POST'),
('p', '888', '/gaia/app-version/releases/:id/download', 'DELETE'),
('p', '8881', '/gaia/app-version/token', 'GET'),
('p', '8881', '/gaia/app-version/token', 'PUT'),
('p', '8881', '/gaia/app-version/releases', 'GET'),
('p', '8881', '/gaia/app-version/releases', 'POST'),
('p', '8881', '/gaia/app-version/releases/:id', 'GET'),
('p', '8881', '/gaia/app-version/releases/:id', 'PUT'),
('p', '8881', '/gaia/app-version/releases/:id/upload', 'POST'),
('p', '8881', '/gaia/app-version/releases/:id/download', 'DELETE'),
('p', '9528', '/gaia/app-version/token', 'GET'),
('p', '9528', '/gaia/app-version/token', 'PUT'),
('p', '9528', '/gaia/app-version/releases', 'GET'),
('p', '9528', '/gaia/app-version/releases', 'POST'),
('p', '9528', '/gaia/app-version/releases/:id', 'GET'),
('p', '9528', '/gaia/app-version/releases/:id', 'PUT'),
('p', '9528', '/gaia/app-version/releases/:id/upload', 'POST'),
('p', '9528', '/gaia/app-version/releases/:id/download', 'DELETE'),
('p', '1', '/gaia/app-version/token', 'GET'),
('p', '1', '/gaia/app-version/token', 'PUT'),
('p', '1', '/gaia/app-version/releases', 'GET'),
('p', '1', '/gaia/app-version/releases', 'POST'),
('p', '1', '/gaia/app-version/releases/:id', 'GET'),
('p', '1', '/gaia/app-version/releases/:id', 'PUT'),
('p', '1', '/gaia/app-version/releases/:id/upload', 'POST'),
('p', '1', '/gaia/app-version/releases/:id/download', 'DELETE');


-- --------------- 4. 角色-菜单关联 sys_authority_menus (让角色 888 拥有「应用版本」菜单) ---------------
-- 表结构：sys_authority_authority_id, sys_base_menu_id（以你库中实际列名为准，有的项目为 authority_id, menu_id）
INSERT INTO sys_authority_menus (sys_authority_authority_id, sys_base_menu_id) VALUES (888, 41);
-- 若需给 8881、9528、1 也加该菜单，可追加:
-- INSERT INTO sys_authority_menus (sys_authority_authority_id, sys_base_menu_id) VALUES (8881, 41);
-- INSERT INTO sys_authority_menus (sys_authority_authority_id, sys_base_menu_id) VALUES (9528, 41);
INSERT INTO sys_authority_menus (sys_authority_authority_id, sys_base_menu_id) VALUES (1, 41);


-- ============================================================
-- 模型管理：菜单、API、Casbin 权限、角色-菜单关联 插入语句
-- ============================================================

-- --------------- 5. 菜单 sys_base_menus (模型管理，挂在「系统集成」下) ---------------
INSERT INTO sys_base_menus (
  id, created_at, updated_at, deleted_at,
  menu_level, parent_id, path, name, hidden, component, sort,
  active_name, keep_alive, default_menu, title, icon, close_tab
) VALUES (
  42,
  NOW(), NOW(), NULL,
  0, 38, 'IntegratedModelManagement', 'IntegratedModelManagement', false, 'view/systemIntegrated/modelManagement/index.vue', 3,
  '', false, false, '模型管理', 'cpu', false
);
-- PostgreSQL: INSERT ... ON CONFLICT (id) DO NOTHING;


-- --------------- 6. API sys_apis (模型管理相关 11 条，proxy 使用通配符 /gaia/proxy/*) ---------------
-- 请按当前库最大 id 调整起始 id，避免冲突。例如 MAX(id)=258 则从 259 起
INSERT INTO sys_apis (id, created_at, updated_at, deleted_at, path, description, api_group, method) VALUES
(259, NOW(), NOW(), NULL, '/gaia/model-provider/list', '获取提供商配置列表', '模型管理', 'GET'),
(260, NOW(), NOW(), NULL, '/gaia/model-provider/update', '更新提供商配置', '模型管理', 'POST'),
(261, NOW(), NOW(), NULL, '/gaia/model-provider/available-models', '获取可用模型', '模型管理', 'GET'),
(262, NOW(), NOW(), NULL, '/gaia/model-provider/test-credentials', '测试提供商凭证', '模型管理', 'GET'),
(263, NOW(), NOW(), NULL, '/gaia/model-provider/logs', '获取代理日志', '模型管理', 'GET'),
(264, NOW(), NOW(), NULL, '/gaia/models', '获取开启的模型列表(第三方)', '模型管理', 'GET'),
(265, NOW(), NOW(), NULL, '/gaia/proxy/*', '中转API(第三方)-GET', '模型管理', 'GET'),
(266, NOW(), NOW(), NULL, '/gaia/proxy/*', '中转API(第三方)-POST', '模型管理', 'POST'),
(267, NOW(), NOW(), NULL, '/gaia/proxy/*', '中转API(第三方)-PUT', '模型管理', 'PUT'),
(268, NOW(), NOW(), NULL, '/gaia/proxy/*', '中转API(第三方)-PATCH', '模型管理', 'PATCH'),
(269, NOW(), NOW(), NULL, '/gaia/proxy/*', '中转API(第三方)-DELETE', '模型管理', 'DELETE');


-- --------------- 7. Casbin 规则 casbin_rule (模型管理 888/8881) ---------------
INSERT INTO casbin_rule (ptype, v0, v1, v2) VALUES
('p', '888', '/gaia/model-provider/list', 'GET'),
('p', '888', '/gaia/model-provider/update', 'POST'),
('p', '888', '/gaia/model-provider/available-models', 'GET'),
('p', '888', '/gaia/model-provider/test-credentials', 'GET'),
('p', '888', '/gaia/model-provider/logs', 'GET'),
('p', '888', '/gaia/models', 'GET'),
('p', '888', '/gaia/proxy/*', 'GET'),
('p', '888', '/gaia/proxy/*', 'POST'),
('p', '888', '/gaia/proxy/*', 'PUT'),
('p', '888', '/gaia/proxy/*', 'PATCH'),
('p', '888', '/gaia/proxy/*', 'DELETE'),
('p', '8881', '/gaia/model-provider/list', 'GET'),
('p', '8881', '/gaia/model-provider/update', 'POST'),
('p', '8881', '/gaia/model-provider/available-models', 'GET'),
('p', '8881', '/gaia/model-provider/test-credentials', 'GET'),
('p', '8881', '/gaia/model-provider/logs', 'GET'),
('p', '8881', '/gaia/models', 'GET'),
('p', '8881', '/gaia/proxy/*', 'GET'),
('p', '8881', '/gaia/proxy/*', 'POST'),
('p', '8881', '/gaia/proxy/*', 'PUT'),
('p', '8881', '/gaia/proxy/*', 'PATCH'),
('p', '8881', '/gaia/proxy/*', 'DELETE');


-- --------------- 8. 角色-菜单关联 sys_authority_menus (让角色 888 拥有「模型管理」菜单) ---------------
INSERT INTO sys_authority_menus (sys_authority_authority_id, sys_base_menu_id) VALUES (888, 42);


-- ============================================================
-- 已有库修复：若曾用具体路径 /gaia/proxy/v1/chat/completions，改为通配符 /gaia/proxy/*
-- ============================================================
-- sys_apis：将具体路径统一改为通配符（与 source/system/api.go、Casbin 一致）
UPDATE sys_apis SET path = '/gaia/proxy/*', description = '中转API(第三方)-POST', updated_at = NOW()
WHERE path = '/gaia/proxy/v1/chat/completions' AND method = 'POST';
-- 若有其他具体子路径也一并改为通配符（可选）
UPDATE sys_apis SET path = '/gaia/proxy/*', updated_at = NOW()
WHERE path LIKE '/gaia/proxy/%' AND path != '/gaia/proxy/*';
-- casbin_rule：若存在具体路径策略可删除，保留通配符策略即可（通常初始化已是 /gaia/proxy/*，可不执行）
-- DELETE FROM casbin_rule WHERE ptype = 'p' AND v1 LIKE '/gaia/proxy/%' AND v1 != '/gaia/proxy/*';
