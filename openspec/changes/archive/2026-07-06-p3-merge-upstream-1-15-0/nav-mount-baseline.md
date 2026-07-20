# main-nav 迁移前挂载点基线（任务 1.1 留档）

> 记录时间：2026-07-06，基点 commit `ffb0c5303f`（P2 完成点，1.14.2 基线）。
> 本地无运行环境（localhost:3000 无响应、无 dify 容器），无法截图；以代码级清单替代截图作为迁移后对比基准。

## fork 相对 1.14.2 在旧 `web/app/components/header/` 的全部差异（`git diff 1.14.2 ffb0c5303f --stat`）

| 文件                                                      | 性质                                         | 迁移去向（1.15.0 后）                                                    |
| --------------------------------------------------------- | -------------------------------------------- | ------------------------------------------------------------------------ |
| `account-money-extend/index.tsx`（85 行）                 | 新增：额度徽章组件                           | 重做到 `main-nav/`（任务 3.1，顺势删 6.97 硬编码汇率）                   |
| `system-manage-nav-extend/index.tsx`（44 行）             | 新增：系统管理入口                           | 重做到 `main-nav/`（任务 3.2）                                           |
| `draw-nav-extend/index.tsx`（43 行）                      | 新增：draw 导航（**当前被注释未挂载**）      | 重做到 `main-nav/`（任务 3.3，保持默认关闭）                             |
| `nav-extend/amazon-marketing.tsx` + `index.tsx`（42 行）  | 新增：nav 挂载点（**当前被注释未挂载**）     | 重做到 `main-nav/`（任务 3.3，保持默认关闭）                             |
| `index.tsx`（+19/-x）                                     | 宿主挂载行（下详）                           | 宿主文件被上游删除，挂载行迁 `main-nav/index.tsx`                        |
| `explore-nav/index.tsx`（2 行）+ 其测试                   | Explore 链接改 `/explore/apps-center-extend` | explore-nav 随上游删除；等价改动需落到 main-nav 路由/routes.ts           |
| `account-setting/index.tsx`（17±）                        | 账户设置二开项                               | `account-setting/` 在 1.15.0 仍存在，走正常 merge 冲突解决（非本节范围） |
| `account-setting/.../invite-modal/`（26± + css）          | 邀请弹窗二开                                 | 同上；另涉任务 4（headlessui 已在 P2 清理，需复核）                      |
| `account-setting/.../parameter-item-extend.tsx`（242 行） | 模型参数二开组件                             | 同上，独立 extend 文件随 merge 保留                                      |
| `account-dropdown/**`                                     | **无 fork 差异**                             | 任务 3.4 无实际重挂项，验证即可                                          |

## 旧 header 宿主挂载行为（迁移后需等价）

`web/app/components/header/index.tsx`（fork 版，1.14.2 基线）：

1. **Logo 链接**：`renderLogo()` 指向 `/explore/apps-center-extend`（上游为 `/apps`），带 `extend: 跳转修改` 注释。
2. **移动端布局**：nav 区依序渲染 `SystemManageNavExtend`（`navClassName`）、`AccountMoneyExtend`；`DrawNav` / `AmazonMarketingNav` 挂载行**被注释**（预留位，默认关闭）。
3. **桌面布局**：左区 `PlanBadge/LicenseNav` 之后渲染 `AccountMoneyExtend`；右区 `EnvNav` 之前渲染 `SystemManageNavExtend`。
4. `ExploreNav` 高亮 segment 与链接指向 apps-center-extend。

## 行为要点（双角色验收依据，对应任务 9.3）

- `AccountMoneyExtend`：`fetchUserMoney`（`service/common-extend`）返回 `used_quota/total_quota`；`total_quota===0` 不渲染；汇率 6.97（**迁移时改读后端 `RMB_TO_USD_RATE`**）；余额 <10 元红色、已用 >50 元黄色。
- `SystemManageNavExtend`：`isCurrentWorkspaceOwner` 才渲染（owner-only，与后端 admin_or_owner 的口径差异已在 P2 登记、留待 p6 统一）；链接 `/system-manage-extend/system-integration`；segment `system-manage-extend` 时高亮。
- `nav-extend` / `draw-nav-extend`：迁移前即为注释态（未启用），迁移后保持"预留挂载位、默认关闭"。
- `account-dropdown`：fork 无差异，迁移后直接取上游。
