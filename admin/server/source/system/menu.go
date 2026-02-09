package system

import (
	"context"

	"github.com/flipped-aurora/gin-vue-admin/server/global"
	. "github.com/flipped-aurora/gin-vue-admin/server/model/system"
	"github.com/flipped-aurora/gin-vue-admin/server/service/system"
	"github.com/pkg/errors"
	"gorm.io/gorm"
)

const initOrderMenu = initOrderAuthority + 1

type initMenu struct{}

// auto run
func init() {
	system.RegisterInit(initOrderMenu, &initMenu{})
}

func (i initMenu) InitializerName() string {
	return SysBaseMenu{}.TableName()
}

func (i *initMenu) MigrateTable(ctx context.Context) (context.Context, error) {
	db, ok := ctx.Value("db").(*gorm.DB)
	if !ok {
		return ctx, system.ErrMissingDBContext
	}
	return ctx, db.AutoMigrate(
		&SysBaseMenu{},
		&SysBaseMenuParameter{},
		&SysBaseMenuBtn{},
	)
}

func (i *initMenu) TableCreated(ctx context.Context) bool {
	db, ok := ctx.Value("db").(*gorm.DB)
	if !ok {
		return false
	}
	m := db.Migrator()
	return m.HasTable(&SysBaseMenu{}) &&
		m.HasTable(&SysBaseMenuParameter{}) &&
		m.HasTable(&SysBaseMenuBtn{})
}

func (i *initMenu) InitializeData(ctx context.Context) (next context.Context, err error) {
	db, ok := ctx.Value("db").(*gorm.DB)
	if !ok {
		return ctx, system.ErrMissingDBContext
	}
	entities := []SysBaseMenu{
		{GVA_MODEL: global.GVA_MODEL{ID: 1}, MenuLevel: 0, Hidden: false, ParentId: 0, Path: "dashboard", Name: "dashboard", Component: "view/dashboard/index.vue", Sort: 1, Meta: Meta{Title: "仪表盘", Icon: "odometer"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 2}, MenuLevel: 0, Hidden: false, ParentId: 0, Path: "about", Name: "about", Component: "view/about/index.vue", Sort: 9, Meta: Meta{Title: "关于我们", Icon: "info-filled"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 3}, MenuLevel: 0, Hidden: false, ParentId: 0, Path: "admin", Name: "superAdmin", Component: "view/superAdmin/index.vue", Sort: 3, Meta: Meta{Title: "超级管理员", Icon: "user"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 4}, MenuLevel: 0, Hidden: false, ParentId: 3, Path: "authority", Name: "authority", Component: "view/superAdmin/authority/authority.vue", Sort: 1, Meta: Meta{Title: "角色管理", Icon: "avatar"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 5}, MenuLevel: 0, Hidden: false, ParentId: 3, Path: "menu", Name: "menu", Component: "view/superAdmin/menu/menu.vue", Sort: 2, Meta: Meta{Title: "菜单管理", Icon: "tickets", KeepAlive: true}},
		{GVA_MODEL: global.GVA_MODEL{ID: 6}, MenuLevel: 0, Hidden: false, ParentId: 3, Path: "api", Name: "api", Component: "view/superAdmin/api/api.vue", Sort: 3, Meta: Meta{Title: "api管理", Icon: "platform", KeepAlive: true}},
		{GVA_MODEL: global.GVA_MODEL{ID: 7}, MenuLevel: 0, Hidden: false, ParentId: 3, Path: "user", Name: "user", Component: "view/superAdmin/user/user.vue", Sort: 4, Meta: Meta{Title: "用户管理", Icon: "coordinate"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 8}, MenuLevel: 0, Hidden: false, ParentId: 3, Path: "dictionary", Name: "dictionary", Component: "view/superAdmin/dictionary/sysDictionary.vue", Sort: 5, Meta: Meta{Title: "字典管理", Icon: "notebook"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 9}, MenuLevel: 0, Hidden: false, ParentId: 3, Path: "operation", Name: "operation", Component: "view/superAdmin/operation/sysOperationRecord.vue", Sort: 6, Meta: Meta{Title: "操作历史", Icon: "pie-chart"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 10}, MenuLevel: 0, Hidden: true, ParentId: 0, Path: "person", Name: "person", Component: "view/person/person.vue", Sort: 4, Meta: Meta{Title: "个人信息", Icon: "message"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 11}, MenuLevel: 0, Hidden: false, ParentId: 0, Path: "example", Name: "example", Component: "view/example/index.vue", Sort: 7, Meta: Meta{Title: "示例文件", Icon: "management"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 12}, MenuLevel: 0, Hidden: false, ParentId: 11, Path: "upload", Name: "upload", Component: "view/example/upload/upload.vue", Sort: 5, Meta: Meta{Title: "媒体库（上传下载）", Icon: "upload"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 13}, MenuLevel: 0, Hidden: false, ParentId: 11, Path: "breakpoint", Name: "breakpoint", Component: "view/example/breakpoint/breakpoint.vue", Sort: 6, Meta: Meta{Title: "断点续传", Icon: "upload-filled"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 14}, MenuLevel: 0, Hidden: false, ParentId: 11, Path: "customer", Name: "customer", Component: "view/example/customer/customer.vue", Sort: 7, Meta: Meta{Title: "客户列表（资源示例）", Icon: "avatar"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 15}, MenuLevel: 0, Hidden: false, ParentId: 0, Path: "systemTools", Name: "systemTools", Component: "view/systemTools/index.vue", Sort: 5, Meta: Meta{Title: "系统工具", Icon: "tools"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 16}, MenuLevel: 0, Hidden: false, ParentId: 15, Path: "autoCode", Name: "autoCode", Component: "view/systemTools/autoCode/index.vue", Sort: 1, Meta: Meta{Title: "代码生成器", Icon: "cpu", KeepAlive: true}},
		{GVA_MODEL: global.GVA_MODEL{ID: 17}, MenuLevel: 0, Hidden: false, ParentId: 15, Path: "formCreate", Name: "formCreate", Component: "view/systemTools/formCreate/index.vue", Sort: 3, Meta: Meta{Title: "表单生成器", Icon: "magic-stick", KeepAlive: true}},
		{GVA_MODEL: global.GVA_MODEL{ID: 18}, MenuLevel: 0, Hidden: false, ParentId: 15, Path: "system", Name: "system", Component: "view/systemTools/system/system.vue", Sort: 4, Meta: Meta{Title: "系统配置", Icon: "operation"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 19}, MenuLevel: 0, Hidden: false, ParentId: 15, Path: "autoCodeAdmin", Name: "autoCodeAdmin", Component: "view/systemTools/autoCodeAdmin/index.vue", Sort: 2, Meta: Meta{Title: "自动化代码管理", Icon: "magic-stick"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 20}, MenuLevel: 0, Hidden: true, ParentId: 15, Path: "autoCodeEdit/:id", Name: "autoCodeEdit", Component: "view/systemTools/autoCode/index.vue", Sort: 0, Meta: Meta{Title: "自动化代码-${id}", Icon: "magic-stick"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 21}, MenuLevel: 0, Hidden: false, ParentId: 15, Path: "autoPkg", Name: "autoPkg", Component: "view/systemTools/autoPkg/autoPkg.vue", Sort: 0, Meta: Meta{Title: "模板配置", Icon: "folder"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 22}, MenuLevel: 0, Hidden: false, ParentId: 0, Path: "https://www.gin-vue-admin.com", Name: "https://www.gin-vue-admin.com", Component: "/", Sort: 0, Meta: Meta{Title: "官方网站", Icon: "customer-gva"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 23}, MenuLevel: 0, Hidden: false, ParentId: 0, Path: "state", Name: "state", Component: "view/system/state.vue", Sort: 8, Meta: Meta{Title: "服务器状态", Icon: "cloudy"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 24}, MenuLevel: 0, Hidden: false, ParentId: 0, Path: "plugin", Name: "plugin", Component: "view/routerHolder.vue", Sort: 6, Meta: Meta{Title: "插件系统", Icon: "cherry"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 25}, MenuLevel: 0, Hidden: false, ParentId: 24, Path: "https://plugin.gin-vue-admin.com/", Name: "https://plugin.gin-vue-admin.com/", Component: "https://plugin.gin-vue-admin.com/", Sort: 0, Meta: Meta{Title: "插件市场", Icon: "shop"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 26}, MenuLevel: 0, Hidden: false, ParentId: 24, Path: "installPlugin", Name: "installPlugin", Component: "view/systemTools/installPlugin/index.vue", Sort: 1, Meta: Meta{Title: "插件安装", Icon: "box"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 27}, MenuLevel: 0, Hidden: false, ParentId: 24, Path: "pubPlug", Name: "pubPlug", Component: "view/systemTools/pubPlug/pubPlug.vue", Sort: 3, Meta: Meta{Title: "打包插件", Icon: "files"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 28}, MenuLevel: 0, Hidden: false, ParentId: 24, Path: "plugin-email", Name: "plugin-email", Component: "plugin/email/view/index.vue", Sort: 4, Meta: Meta{Title: "邮件插件", Icon: "message"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 29}, MenuLevel: 0, Hidden: false, ParentId: 15, Path: "exportTemplate", Name: "exportTemplate", Component: "view/systemTools/exportTemplate/exportTemplate.vue", Sort: 5, Meta: Meta{Title: "导出模板", Icon: "reading"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 30}, MenuLevel: 0, Hidden: false, ParentId: 24, Path: "anInfo", Name: "anInfo", Component: "plugin/announcement/view/info.vue", Sort: 5, Meta: Meta{Title: "公告管理[示例]", Icon: "scaleToOriginal"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 31}, MenuLevel: 0, Hidden: false, ParentId: 3, Path: "sysParams", Name: "sysParams", Component: "view/superAdmin/params/sysParams.vue", Sort: 7, Meta: Meta{Title: "参数管理", Icon: "compass"}},
		// 二开部分
		{GVA_MODEL: global.GVA_MODEL{ID: 32}, MenuLevel: 0, Hidden: false, ParentId: 0, Path: "gaiaDashboard", Name: "gaiaDashboard", Component: "view/gaia/dashboard/index.vue", Sort: 0, Meta: Meta{Title: "费用报表", Icon: "odometer"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 33}, MenuLevel: 0, Hidden: false, ParentId: 0, Path: "UserList", Name: "UserList", Component: "view/user/index.vue", Sort: 1, Meta: Meta{Title: "用户列表", Icon: "user"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 34}, MenuLevel: 0, Hidden: false, ParentId: 0, Path: "QuotaList", Name: "QuotaList", Component: "view/quota/index.vue", Sort: 2, Meta: Meta{Title: "额度管理", Icon: "wallet"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 35}, MenuLevel: 0, Hidden: false, ParentId: 0, Path: "SuperTest", Name: "SuperTest", Component: "view/test/index.vue", Sort: 2, Meta: Meta{Title: "测试管理", Icon: "management"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 36}, MenuLevel: 0, Hidden: false, ParentId: 35, Path: "AppRequestTestBatch", Name: "AppRequestTestBatch", Component: "view/test/appRequest/index.vue", Sort: 1, Meta: Meta{Title: "测试批次", Icon: "list"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 37}, MenuLevel: 0, Hidden: true, ParentId: 35, Path: "AppRequestTestList", Name: "AppRequestTestList", Component: "view/test/appRequest/list.vue", Sort: 1, Meta: Meta{Title: "测试列表", Icon: "list"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 38}, MenuLevel: 0, Hidden: false, ParentId: 0, Path: "SystemIntegrated", Name: "SystemIntegrated", Component: "view/systemIntegrated/index.vue", Sort: 1, Meta: Meta{Title: "系统集成", Icon: "box"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 39}, MenuLevel: 0, Hidden: false, ParentId: 38, Path: "IntegratedDingTalk", Name: "IntegratedDingTalk", Component: "view/systemIntegrated/dingTalk/index.vue", Sort: 1, Meta: Meta{Title: "钉钉", Icon: "turn-off"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 40}, MenuLevel: 0, Hidden: false, ParentId: 38, Path: "IntegratedOAuth2", Name: "IntegratedOAuth2", Component: "view/systemIntegrated/oauth2/index.vue", Sort: 2, Meta: Meta{Title: "OAuth2", Icon: "share"}},
		{GVA_MODEL: global.GVA_MODEL{ID: 41}, MenuLevel: 0, Hidden: false, ParentId: 0, Path: "AppVersion", Name: "AppVersion", Component: "view/gaia/appVersion/index.vue", Sort: 10, Meta: Meta{Title: "版本管理", Icon: "upload-filled"}},
		// 二开部分
	}
	if err = db.Create(&entities).Error; err != nil {
		return ctx, errors.Wrap(err, SysBaseMenu{}.TableName()+"表数据初始化失败!")
	}
	next = context.WithValue(ctx, i.InitializerName(), entities)
	return next, nil
}

func (i *initMenu) DataInserted(ctx context.Context) bool {
	db, ok := ctx.Value("db").(*gorm.DB)
	if !ok {
		return false
	}
	if errors.Is(db.Where("path = ?", "autoPkg").First(&SysBaseMenu{}).Error, gorm.ErrRecordNotFound) { // 判断是否存在数据
		return false
	}
	return true
}
