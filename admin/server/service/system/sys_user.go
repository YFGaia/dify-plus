package system

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/flipped-aurora/gin-vue-admin/server/model/common"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia"
	systemReq "github.com/flipped-aurora/gin-vue-admin/server/model/system/request"
	serviceGaia "github.com/flipped-aurora/gin-vue-admin/server/service/gaia"
	"go.uber.org/zap"

	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/model/system"
	"github.com/flipped-aurora/gin-vue-admin/server/utils"
	"github.com/gofrs/uuid/v5"
	"gorm.io/gorm"
)

//@author: [piexlmax](https://github.com/piexlmax)
//@function: Register
//@description: 用户注册
//@param: u model.SysUser
//@return: userInter system.SysUser, err error

type UserService struct{}

var UserServiceApp = new(UserService)

// Register
// @author: [piexlmax](https://github.com/piexlmax)
// @author: [SliverHorn](https://github.com/SliverHorn)
// @function: Register
// @description: 用户注册
// @param: u *model.SysUser
// @return: err error, userInter *model.SysUser
func (userService *UserService) Register(u system.SysUser, token string) (userInter system.SysUser, err error) {
	var user system.SysUser
	// 首先检查email是否已注册
	if !errors.Is(global.GVA_DB.Where("email = ?", u.Email).First(&user).Error, gorm.ErrRecordNotFound) {
		global.GVA_LOG.Info(fmt.Sprintf("用户email已存在: %s", u.Email))
		return userInter, errors.New("用户名已注册")
	}

	// 如果传入了UUID，检查UUID是否已存在
	if u.UUID != uuid.Nil {
		var existingUser system.SysUser
		if !errors.Is(global.GVA_DB.Where("uuid = ?", u.UUID).First(&existingUser).Error, gorm.ErrRecordNotFound) {
			global.GVA_LOG.Info(fmt.Sprintf("用户UUID已存在: %s, email: %s", u.UUID, u.Email))
			// UUID已存在，返回已存在的用户而不是报错（用于SyncUser场景）
			return existingUser, nil
		}
	}

	global.GVA_LOG.Debug("注册用户信息:", zap.Any("1", 1))

	// Extend Start: Gaia Register User
	if err = serviceGaia.RegisterUser(u, token); err != nil {
		return userInter, errors.New("gaia注册失败:" + err.Error())
	}
	// Extend Stop: Gaia Register User

	// 再次检查email是否已注册（防止并发创建）
	if !errors.Is(global.GVA_DB.Where("email = ?", u.Email).First(&user).Error, gorm.ErrRecordNotFound) {
		global.GVA_LOG.Info(fmt.Sprintf("并发检测：用户email已被创建: %s", u.Email))
		return user, nil
	}

	// 否则 附加uuid 密码hash加密 注册
	u.Password = utils.BcryptHash(u.Password)
	// 如果没有设置UUID，才生成新的UUID
	if u.UUID == uuid.Nil {
		u.UUID = uuid.Must(uuid.NewV4())
	}
	err = global.GVA_DB.Create(&u).Error
	return u, err
}

//@author: [piexlmax](https://github.com/piexlmax)
//@author: [SliverHorn](https://github.com/SliverHorn)
//@function: Login
//@description: 用户登录
//@param: u *model.SysUser
//@return: err error, userInter *model.SysUser

func (userService *UserService) Login(u *system.SysUser) (userInter *system.SysUser, err error) {
	if nil == global.GVA_DB {
		return nil, fmt.Errorf("db not init")
	}

	var user system.SysUser
	err = global.GVA_DB.Where("username = ? or email = ?", u.Username, u.Username).Preload("Authorities").Preload("Authority").First(&user).Error
	if err == nil {
		// Extend: Start 用户账号密码登录修改
		var ok bool
		var account gaia.Account
		var pwd = serviceGaia.PasswdEncode{}
		if account, err = user.GetAccount(); err != nil {
			return nil, errors.New("无法在Gaia中找到相关用户, 请联系管理员到用户列表执行刷新操作")
		}
		// 判断密码是否正确
		if ok, err = pwd.ComparePassword(u.Password, account.Password, account.PasswordSalt); err != nil || !ok {
			return nil, errors.New("密码错误")
		}
		// Extend: Stop 用户账号密码登录修改
		MenuServiceApp.UserAuthorityDefaultRouter(&user)
	}
	return &user, err
}

//@author: [piexlmax](https://github.com/piexlmax)
//@function: ChangePassword
//@description: 修改用户密码
//@param: u *model.SysUser, newPassword string
//@return: userInter *model.SysUser,err error

func (userService *UserService) ChangePassword(u *system.SysUser, newPassword string) (userInter *system.SysUser, err error) {
	var user system.SysUser
	if err = global.GVA_DB.Where("id = ?", u.ID).First(&user).Error; err != nil {
		return nil, err
	}
	if ok := utils.BcryptCheck(u.Password, user.Password); !ok {
		return nil, errors.New("原密码错误")
	}
	user.Password = utils.BcryptHash(newPassword)
	err = global.GVA_DB.Save(&user).Error
	return &user, err

}

//@author: [piexlmax](https://github.com/piexlmax)
//@function: GetUserInfoList
//@description: 分页获取数据
//@param: info request.PageInfo
//@return: err error, list interface{}, total int64

func (userService *UserService) GetUserInfoList(info systemReq.GetUserList) (
	list []map[string]interface{}, total int64, err error) {
	limit := info.PageSize
	offset := info.PageSize * (info.Page - 1)
	db := global.GVA_DB.Model(&system.SysUser{})
	var userList []system.SysUser

	if info.NickName != "" {
		db = db.Where("nick_name LIKE ?", "%"+info.NickName+"%")
	}
	if info.Phone != "" {
		db = db.Where("phone LIKE ?", "%"+info.Phone+"%")
	}
	if info.Username != "" {
		db = db.Where("username LIKE ?", "%"+info.Username+"%")
	}
	if info.Email != "" {
		db = db.Where("email LIKE ?", "%"+info.Email+"%")
	}

	err = db.Count(&total).Error
	if err != nil {
		return
	}
	err = db.Limit(limit).Offset(offset).Preload("Authorities").Preload(
		"Authority").Order("id desc").Find(&userList).Error
	if err != nil {
		return
	}

	// Extend Start: global code

	// 获取用户关联的控制名称
	var idList []uint
	var globalCode []system.SysUserGlobalCode
	if err = global.GVA_DB.Find(&globalCode).Error; err == nil {
		for _, v := range globalCode {
			idList = append(idList, v.UserID)
		}
	}

	// Extend Stop: global code

	// Extend Start: Loop through the user list to see if it is disabled
	for i, v := range userList {
		if len(v.Email) == 0 {
			continue
		}
		// Check if the user is disabled
		if count, iErr := global.GVA_REDIS.Get(context.Background(), fmt.Sprintf(
			"login_error_rate_limit:%s", v.Email)).Int(); iErr == nil && count >= global.GVA_CONFIG.Gaia.LoginMaxErrorLimit {
			userList[i].Enable = system.UserDeactivate
		}
		// encode
		var userByte []byte
		var userDick map[string]interface{}
		if userByte, err = json.Marshal(&userList[i]); err == nil {
			err = json.Unmarshal(userByte, &userDick)
		}
		list = append(list, userDick)
		if utils.InUintArray(v.ID, idList) {
			list[i]["global_code"] = true
		}
	}
	// Extend Stop: Loop through the user list to see if it is disabled
	return list, total, err
}

//@author: [piexlmax](https://github.com/piexlmax)
//@function: SetUserAuthority
//@description: 设置一个用户的权限
//@param: uuid uuid.UUID, authorityId string
//@return: err error

func (userService *UserService) SetUserAuthority(id uint, authorityId uint) (err error) {

	assignErr := global.GVA_DB.Where("sys_user_id = ? AND sys_authority_authority_id = ?", id, authorityId).First(&system.SysUserAuthority{}).Error
	if errors.Is(assignErr, gorm.ErrRecordNotFound) {
		return errors.New("该用户无此角色")
	}

	var authority system.SysAuthority
	err = global.GVA_DB.Where("authority_id = ?", authorityId).First(&authority).Error
	if err != nil {
		return err
	}
	var authorityMenu []system.SysAuthorityMenu
	var authorityMenuIDs []string
	err = global.GVA_DB.Where("sys_authority_authority_id = ?", authorityId).Find(&authorityMenu).Error
	if err != nil {
		return err
	}

	for i := range authorityMenu {
		authorityMenuIDs = append(authorityMenuIDs, authorityMenu[i].MenuId)
	}

	var authorityMenus []system.SysBaseMenu
	err = global.GVA_DB.Preload("Parameters").Where("id in (?)", authorityMenuIDs).Find(&authorityMenus).Error
	if err != nil {
		return err
	}
	hasMenu := false
	for i := range authorityMenus {
		if authorityMenus[i].Name == authority.DefaultRouter {
			hasMenu = true
			break
		}
	}
	if !hasMenu {
		return errors.New("找不到默认路由,无法切换本角色")
	}

	err = global.GVA_DB.Model(&system.SysUser{}).Where("id = ?", id).Update("authority_id", authorityId).Error
	return err
}

//@author: [piexlmax](https://github.com/piexlmax)
//@function: SetUserAuthorities
//@description: 设置一个用户的权限
//@param: id uint, authorityIds []string
//@return: err error

func (userService *UserService) SetUserAuthorities(adminAuthorityID, id uint, authorityIds []uint) (err error) {
	return global.GVA_DB.Transaction(func(tx *gorm.DB) error {
		var user system.SysUser
		TxErr := tx.Where("id = ?", id).First(&user).Error
		if TxErr != nil {
			global.GVA_LOG.Debug(TxErr.Error())
			return errors.New("查询用户数据失败")
		}
		TxErr = tx.Delete(&[]system.SysUserAuthority{}, "sys_user_id = ?", id).Error
		if TxErr != nil {
			return TxErr
		}
		var useAuthority []system.SysUserAuthority
		for _, v := range authorityIds {
			e := AuthorityServiceApp.CheckAuthorityIDAuth(adminAuthorityID, v)
			if e != nil {
				return e
			}
			useAuthority = append(useAuthority, system.SysUserAuthority{
				SysUserId: id, SysAuthorityAuthorityId: v,
			})
		}
		TxErr = tx.Create(&useAuthority).Error
		if TxErr != nil {
			return TxErr
		}
		TxErr = tx.Model(&user).Update("authority_id", authorityIds[0]).Error
		if TxErr != nil {
			return TxErr
		}
		// 返回 nil 提交事务
		return nil
	})
}

//@author: [piexlmax](https://github.com/piexlmax)
//@function: DeleteUser
//@description: 删除用户
//@param: id float64
//@return: err error

func (userService *UserService) DeleteUser(id int) (err error) {
	return global.GVA_DB.Transaction(func(tx *gorm.DB) error {
		// 1. 获取用户信息
		var user system.SysUser
		if err := tx.Where("id = ?", id).First(&user).Error; err != nil {
			return err
		}

		// 2. 关闭关联账户（accounts.status = closed）
		var account gaia.Account
		if err := tx.Where("email = ?", user.Email).First(&account).Error; err != nil {
			return err
		}
		if err := tx.Model(&gaia.Account{}).Where("id = ?", account.ID).Updates(map[string]interface{}{
			"status":     gaia.UserClosed,
			"updated_at": time.Now(),
		}).Error; err != nil {
			return err
		}

		// 3. 删除用户以及用户角色关联
		if err := tx.Where("id = ?", id).Delete(&system.SysUser{}).Error; err != nil {
			return err
		}
		if err := tx.Delete(&[]system.SysUserAuthority{}, "sys_user_id = ?", id).Error; err != nil {
			return err
		}
		return nil
	})
}

//@author: [piexlmax](https://github.com/piexlmax)
//@function: SetUserInfo
//@description: 设置用户信息
//@param: req system.SysUser, globalCodeType bool
//@return: err error, user model.SysUser

func (userService *UserService) SetUserInfo(req system.SysUser, globalCodeType bool) error {

	// Extend Start: synchronize gaia user information

	var err error
	var user system.SysUser
	if err = global.GVA_DB.Where("id = ?", req.ID).First(&user).Error; err != nil {
		return errors.New("SetUserInfo : No relevant users found" + err.Error())
	}
	var account gaia.Account
	if account, err = user.GetAccount(); err != nil {
		return errors.New("SetUserInfo : No corresponding user can be found in Gaia" + err.Error())
	}
	// determine the switching user status
	status := gaia.UserBanned
	if req.Enable == system.UserActive {
		status = gaia.UserActive
	}
	// switch user status
	user.SyncGaiaStatus(req.Enable)
	// modify Gaia user information
	if err = global.GVA_DB.Model(&gaia.Account{}).Where("id=?", account.ID).Updates(map[string]interface{}{
		"updated_at": time.Now(),
		"name":       req.NickName,
		"avatar":     req.HeaderImg,
		"email":      req.Email,
		"status":     status,
	}).Error; err != nil {
		return errors.New("SetUserInfo : gaia.Account update error: " + err.Error())
	}

	// Extend Stop: synchronize gaia user information

	// Extend Start global code
	globalCode := system.SysUserGlobalCode{UserID: user.ID}

	// 这里假设 gorn 提供了类似 gorm 的 FirstOrCreate 方法
	if globalCodeType {
		global.GVA_DB.FirstOrCreate(&globalCode, global.GVA_DB.Where("user_id = ?", user.ID))
	}

	// 如果需要根据 req.GlobalCode 决定是否创建记录
	if !globalCodeType {
		// 如果不需要 GlobalCode，可以在这里删除记录
		global.GVA_DB.Where("user_id = ?", user.ID).Delete(&globalCode)
	}
	// 修改
	go serviceGaia.SyncExecuteCode()
	// Extend Start global code

	return global.GVA_DB.Model(&system.SysUser{}).
		Select("updated_at", "nick_name", "header_img", "phone", "email", "enable").
		Where("id=?", req.ID).
		Updates(map[string]interface{}{
			"updated_at": time.Now(),
			"nick_name":  req.NickName,
			"header_img": req.HeaderImg,
			"phone":      req.Phone,
			"email":      req.Email,
			"enable":     req.Enable,
		}).Error
}

//@author: [piexlmax](https://github.com/piexlmax)
//@function: SetSelfInfo
//@description: 设置用户信息
//@param: reqUser model.SysUser
//@return: err error, user model.SysUser

func (userService *UserService) SetSelfInfo(req system.SysUser) error {
	return global.GVA_DB.Model(&system.SysUser{}).
		Where("id=?", req.ID).
		Updates(req).Error
}

//@author: [piexlmax](https://github.com/piexlmax)
//@function: SetSelfSetting
//@description: 设置用户配置
//@param: req datatypes.JSON, uid uint
//@return: err error

func (userService *UserService) SetSelfSetting(req common.JSONMap, uid uint) error {
	return global.GVA_DB.Model(&system.SysUser{}).Where("id = ?", uid).Update("origin_setting", req).Error
}

//@author: [piexlmax](https://github.com/piexlmax)
//@author: [SliverHorn](https://github.com/SliverHorn)
//@function: GetUserInfo
//@description: 获取用户信息
//@param: uuid uuid.UUID
//@return: err error, user system.SysUser

func (userService *UserService) GetUserInfo(uuid uuid.UUID) (user system.SysUser, err error) {
	var reqUser system.SysUser
	err = global.GVA_DB.Preload("Authorities").Preload("Authority").First(&reqUser, "uuid = ?", uuid).Error
	if err != nil {
		return reqUser, err
	}
	MenuServiceApp.UserAuthorityDefaultRouter(&reqUser)
	return reqUser, err
}

//@author: [SliverHorn](https://github.com/SliverHorn)
//@function: FindUserById
//@description: 通过id获取用户信息
//@param: id int
//@return: err error, user *model.SysUser

func (userService *UserService) FindUserById(id int) (user *system.SysUser, err error) {
	var u system.SysUser
	err = global.GVA_DB.Where("id = ?", id).First(&u).Error
	return &u, err
}

//@author: [SliverHorn](https://github.com/SliverHorn)
//@function: FindUserByUuid
//@description: 通过uuid获取用户信息
//@param: uuid string
//@return: err error, user *model.SysUser

func (userService *UserService) FindUserByUuid(uuid string) (user *system.SysUser, err error) {
	var u system.SysUser
	if err = global.GVA_DB.Where("uuid = ?", uuid).First(&u).Error; err != nil {
		return &u, errors.New("用户不存在")
	}
	return &u, nil
}

// Extend Start: update password

// ResetPassword
// @author: [piexlmax](https://github.com/piexlmax)
// @function: ResetPassword
// @description: 修改用户密码
// @param: ID uint
// @return: err error
func (userService *UserService) ResetPassword(id uint, passwd string) (err error) {
	var s serviceGaia.PasswdEncode
	var user system.SysUser
	if err = global.GVA_DB.Where("id = ?", id).First(&user).Error; err == nil {
		var passwordHashed, salt string
		global.GVA_DB.Model(&system.SysUser{}).Where("id = ?", id).Updates(&map[string]string{
			"password": utils.BcryptHash(passwd),
		})
		if passwordHashed, salt, err = s.EncodePassword(passwd); err != nil {
			return
		}
		var account gaia.Account
		if account, err = user.GetAccount(); err == nil {
			account.PasswordSalt = salt
			account.Password = passwordHashed
			err = global.GVA_DB.Save(&account).Error
		}
	}
	return err
}

// Extend Stop: update password
