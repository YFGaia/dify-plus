package system

import (
	"fmt"
	"strings"
	"sync"

	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia"
	"github.com/flipped-aurora/gin-vue-admin/server/model/system"
	"github.com/flipped-aurora/gin-vue-admin/server/utils"
	"github.com/gofrs/uuid/v5"
)

// 全局互斥锁，防止SyncUser并发执行
var syncUserMutex sync.Mutex

//@author: [piexlmax](https://github.com/piexlmax)
//@function: Register
//@description: 用户注册
//@param: u model.SysUser
//@return: userInter system.SysUser, err error

type UserExtendService struct{}

//@author: [piexlmax](https://github.com/piexlmax)
//@author: [SliverHorn](https://github.com/SliverHorn)
//@function: OaLogin
//@description: 用户登录(不检查密码)
//@param: u *model.SysUser
//@return: err error, userInter *model.SysUser

func (userService *UserExtendService) OaLogin(u *system.SysUser) (userInter *system.SysUser, err error) {
	if nil == global.GVA_DB {
		return nil, fmt.Errorf("db not init")
	}

	var user system.SysUser
	err = global.GVA_DB.Where("username = ?", u.Username).Preload("Authorities").Preload("Authority").First(&user).Error
	if err == nil {
		MenuServiceApp.UserAuthorityDefaultRouter(&user)
	}
	return &user, err
}

// SyncUser
// @author: [piexlmax](https://github.com/piexlmax)
// @author: [SliverHorn](https://github.com/SliverHorn)
// @function: SyncUser
// @description: 用户同步
// @param: u *model.SysUser
// @return: err error, userInter *model.SysUser
func (userService *UserExtendService) SyncUser() {
	// 使用互斥锁防止并发执行
	syncUserMutex.Lock()
	defer syncUserMutex.Unlock()
	
	// init
	var err error
	var isInit = true
	var user []system.SysUser
	var accountList []gaia.Account
	var mailDick = make(map[string]string)
	if global.GVA_DB == nil {
		global.GVA_LOG.Info("数据库未初始化，同步用户失败")
		return
	}
	// 遍历后台用户表
	if err = global.GVA_DB.Select("email", "username").Find(&user).Error; err != nil {
		global.GVA_LOG.Error("SyncUser gaia表查询失败,原因: " + err.Error())
		return
	}
	// 循环用户列表
	if len(user) > 0 {
		isInit = false
	}
	var emailList []string
	for _, v := range user {
		emailList = append(emailList, v.Email)
		mailDick[v.Email] = v.Username
	}
	// 查询gaia用户表
	db := global.GVA_DB
	if len(emailList) > 0 {
		db = db.Where("email NOT IN (?)", emailList)
	}
	if err = db.Where("status = ?", gaia.UserActive).Order("created_at ASC").Find(&accountList).Error; err != nil {
		global.GVA_LOG.Error("SyncUser gaia表查询失败,原因: " + err.Error())
		return
	}
	var adminAuthorities, userAuthorities []system.SysAuthority
	userAuthorities = append(userAuthorities, system.SysAuthority{
		AuthorityId: system.DefaultGroupID,
	})
	adminAuthorities = append(adminAuthorities, system.SysAuthority{
		AuthorityId: system.AdminGroupID,
	})
	// 循环结果
	for i, v := range accountList {
		// 创建相关用户
		var integrate gaia.AccountIntegrate
		if username, ok := mailDick[v.Email]; ok {
			if integrate, err = v.GetAccount(username); err != nil {
				global.GVA_LOG.Debug("SyncUser GetAccount: " + err.Error())
			}
		}
		// 是否配置了多余信息获取接口
		var name = v.Name
		var phone, ding string
		if mailList := strings.Split(v.Email, "@"); len(integrate.OpenID) == 0 && len(mailList) == 2 {
			integrate.OpenID = mailList[0]
		}

		// 创建用户
		s := UserService{}
		uuidStr, _ := uuid.NewV1()
		if len(ding) > 0 {
			if err = global.GVA_DB.Create(gaia.AccountDingTalkExtend{
				ID:       v.ID,
				DingTalk: ding,
			}).Error; err != nil {
				global.GVA_LOG.Error("SyncUser Create AccountDingTalkExtend: " + err.Error())
			}
		}
		// 注册
		AuthorityId := system.DefaultGroupID
		authorities := userAuthorities
		if isInit && i == 0 {
			// admin
			AuthorityId = system.AdminGroupID
			authorities = adminAuthorities

			// 并设置管理员配置
			if global.GVA_CONFIG.Gaia.SuperAdminTenantId == "" || global.GVA_CONFIG.Gaia.SuperAdminAccountId == "" {
				global.GVA_CONFIG.Gaia.SuperAdminAccountId = v.ID.String()
				// TODO 需要查询默认空间的id
				var Tenant gaia.Tenants
				global.GVA_CONFIG.Gaia.SuperAdminTenantId = Tenant.GetSuperAdminTenantId()
				cs := utils.StructToMap(global.GVA_CONFIG)
				for k, v := range cs {
					global.GVA_VP.Set(k, v)
				}
				err = global.GVA_VP.WriteConfig()
				if err != nil {
					return
				}
			}
		}
		// Register
		if _, err = s.Register(system.SysUser{
			HeaderImg:   "",
			UUID:        v.ID,
			NickName:    name,
			Phone:       phone,
			Email:       v.Email,
			AuthorityId: AuthorityId,
			Authorities: authorities,
			Username:    integrate.OpenID,
			Password:    uuidStr.String(),
			Enable:      system.UserActive,
		}, ""); err != nil {
			global.GVA_LOG.Error("SyncUser Register system.SysUser: " + err.Error())
		}
	}
}
