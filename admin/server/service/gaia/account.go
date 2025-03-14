package gaia

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia"
	"github.com/flipped-aurora/gin-vue-admin/server/model/system"
	"github.com/flipped-aurora/gin-vue-admin/server/utils"
	"go.uber.org/zap"
	"io"
	"net/http"
	"regexp"
)

// IsUserPasswordValid
// @author: [piexlmax](https://github.com/piexlmax)
// @author: [SliverHorn](https://github.com/SliverHorn)
// @function: IsUserPasswordValid
// @description: User Password Valid
// @param: passwd
// @return info request.GetUserInfoByUserName, err error
func IsUserPasswordValid(passwd string) (err error) {
	// Check if the string is at least 8 characters long
	if len(passwd) < 8 {
		return errors.New("请使用最少8位且最少有一个字母数字组合的密码")
	}

	// Check if the string contains at least one letter
	if containsLetter := regexp.MustCompile(`[a-zA-Z]`).MatchString(passwd); !containsLetter {
		return errors.New("请最少最少有一个字母组合的密码")
	}

	// Check if the string contains at least one digit
	if containsLetter := regexp.MustCompile(`\d`).MatchString(passwd); !containsLetter {
		return errors.New("请最少最少有一个数字组合的密码")
	}

	return nil
}

// GetSysUser
// @author: [piexlmax](https://github.com/piexlmax)
// @author: [SliverHorn](https://github.com/SliverHorn)
// @function: GetSysUser
// @description: Get user information by account
// @param: account gaia.Account
// @return system.SysUser, error
func GetSysUser(account gaia.Account) (system.SysUser, error) {
	// init
	var err error
	var user system.SysUser
	var integrate gaia.AccountIntegrate
	if err = global.GVA_DB.Where("account_id=? AND provider=?",
		account.ID, gaia.DefaultProviderType).First(&integrate).Error; err != nil {
		return user, errors.New("the relation between provider and user cannot be found")
	}
	// 查询用户信息
	if err = global.GVA_DB.Where("username=?", account.Name).First(&user).Error; err != nil {
		return user, errors.New("no relevant users found locally")
	}
	// 获取相关用户信息
	if err = global.GVA_DB.Where("username=?", integrate.OpenID).First(&user).Error; err != nil {
		return user, errors.New("unable to find any related user in the database")
	}
	// return
	return user, nil
}

// SyncUserStatus
// @author: [piexlmax](https://github.com/piexlmax)
// @author: [SliverHorn](https://github.com/SliverHorn)
// @function: SyncUserStatus
// @description: 同步用户状态
func SyncUserStatus() {
	// init
	var err error
	var account []gaia.Account
	var userDick = make(map[string]int)
	if err = global.GVA_DB.Find(&account).Error; err != nil {
		return
	}
	for _, v := range account {
		var userStatus = system.UserActive
		if v.Status != gaia.UserActive {
			userStatus = system.UserDeactivate
		}
		userDick[v.Email] = userStatus
	}
	// 获取gva用户列表
	var userList []system.SysUser
	if err = global.GVA_DB.Find(&userList).Error; err != nil {
		return
	}
	// 循环用户列表
	for _, v := range userList {
		if info, ok := userDick[v.Email]; ok {
			if v.Enable != info {
				global.GVA_DB.Model(&v).Updates(&map[string]interface{}{
					"enable": info,
				})
			}
		}
	}
}

// RegisterUser
// @author: [piexlmax](https://github.com/piexlmax)
// @author: [SliverHorn](https://github.com/SliverHorn)
// @function: RegisterUser
// @description: Gaia用户注册函数
// @param: u system.SysUser, token string
// @return: err error, userInter *model.SysUser
func RegisterUser(u system.SysUser, token string) (err error) {
	// 初始化密码
	var body []byte
	var s PasswdEncode
	var passwordHashed, salt string
	global.GVA_LOG.Debug("注册用户信息:", zap.Any("1", 1))
	if passwordHashed, salt, err = s.EncodePassword(u.Password); err != nil {
		return
	}
	global.GVA_LOG.Debug("注册用户信息:", zap.Any("1", 1))
	var acc gaia.Account
	if err = global.GVA_DB.Where("email=?", u.Email).First(&acc).Error; err == nil {
		// 用户已存在
		global.GVA_LOG.Info(fmt.Sprintf("account %s", acc.Name))
		return nil
	}
	// 默认以root执行
	var adminUser system.SysUser
	if err = global.GVA_DB.Where("authority_id=?", system.AdminAuthorityId).Order(
		"id asc").First(&adminUser).Error; err != nil {
		return err
	}

	global.GVA_LOG.Debug("注册用户信息:", zap.Any("1", 1))
	if token, _, err = utils.LoginToken(&adminUser); err != nil {
		return err
	}

	global.GVA_LOG.Debug("注册用户信息:", zap.Any("1", 1))
	// 合成用户新建
	if body, err = json.Marshal(&map[string]interface{}{
		"name":  u.Username,
		"nick":  u.NickName,
		"email": u.Email,
	}); err != nil {
		return err
	}

	global.GVA_LOG.Debug("注册用户信息:", zap.Any("1", 1))
	// 请求远程创建
	var res *http.Response
	req, _ := http.NewRequest("POST", fmt.Sprintf(
		"%s/console/api/admin_register_user", global.GVA_CONFIG.Gaia.Url), bytes.NewBuffer(body))
	req.Header.Add("content-type", "application/json")
	req.Header.Add("Authorization", "Bearer "+token)
	req.Header.Add("console_token", token)
	if res, err = http.DefaultClient.Do(req); err != nil {
		return err
	}
	var bodyByte []byte
	var bodyMap map[string]string
	if bodyByte, err = io.ReadAll(res.Body); err != nil {
		return err
	}

	global.GVA_LOG.Debug("注册用户信息:", zap.Any("1", 1))
	_ = res.Body.Close()
	if err = json.Unmarshal(bodyByte, &bodyMap); err != nil {
		return err
	}

	global.GVA_LOG.Debug("注册用户信息:", zap.Any("1", 1))
	// result
	if result, ok := bodyMap["result"]; !ok && result != "success" {
		return errors.New("failed to create user")
	}
	// 修改密码
	var account gaia.Account
	if account, err = u.GetAccount(); err != nil {
		return err
	}

	global.GVA_LOG.Debug("注册用户信息:", zap.Any("1", 1))
	// 修改密码
	global.GVA_DB.Model(&account).Updates(&map[string]interface{}{
		"password":      passwordHashed,
		"password_salt": salt,
	})
	// 完成
	return nil
}
