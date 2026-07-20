## ADDED Requirements

### Requirement: 4 个二开挂载点在 main-nav 体系恢复

fork 原挂载于旧 `web/app/components/header/`（上游 1.15.0 已删除）的 4 个挂载点 SHALL 在 `web/app/components/main-nav/` 新导航体系中重新实现：`account-money-extend`（额度徽章）、`system-manage-nav-extend`（系统管理入口）、`nav-extend`、`draw-nav-extend`。实现 MUST 采用独立 extend 组件文件 + 宿主文件最小挂载行的模式，并保留「二开部分 Begin/End」注释标记。

#### Scenario: 额度徽章正常展示

- **WHEN** 已登录用户加载 Console 任意页面
- **THEN** 新导航中展示该用户的额度徽章，数值与 `account_money_extend` 数据一致

#### Scenario: nav-extend 与 draw-nav-extend 入口可达

- **WHEN** 用户查看新导航
- **THEN** fork 原有的 nav-extend / draw-nav-extend 入口在新体系中存在且点击行为与迁移前一致

#### Scenario: 旧 header 挂载实现已清除

- **WHEN** 检查合并后代码
- **THEN** fork 在旧 `header/` 目录下的挂载实现已删除，无残留 import 引用

### Requirement: 系统管理入口按角色可见

`system-manage-nav-extend` 系统管理入口 SHALL 沿用迁移前的可见性口径：具备管理权限的用户（owner/admin，口径与迁移前一致）可见并可进入 `/system-manage-extend/`；普通成员不可见。

#### Scenario: owner 可见

- **WHEN** workspace owner 查看新导航
- **THEN** 系统管理入口可见，点击进入 `/system-manage-extend/` 页面正常

#### Scenario: 普通成员不可见

- **WHEN** 普通成员查看新导航
- **THEN** 导航中不出现系统管理入口

### Requirement: account-dropdown 二开项重挂

上游大幅重写的 account-dropdown 中，fork 原有的二开菜单项/信息展示 SHALL 在新实现中重挂，功能与迁移前一致。

#### Scenario: dropdown 二开项可用

- **WHEN** 用户打开账号下拉菜单
- **THEN** fork 二开项全部出现且行为与迁移前一致

### Requirement: 额度徽章汇率改读后端配置

额度徽章的人民币→美元换算 SHALL 使用后端配置下发的汇率值（`RMB_TO_USD_RATE`，当前 7.26），前端 MUST NOT 保留硬编码汇率 `6.97`。

#### Scenario: 汇率来自后端

- **WHEN** 后端配置 `RMB_TO_USD_RATE=7.26` 且用户查看额度徽章
- **THEN** 换算展示按 7.26 计算，且前端代码中不存在 `6.97` 硬编码
