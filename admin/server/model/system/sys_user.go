package system

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/model/common"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia"
	"github.com/gofrs/uuid/v5"
)

type Login interface {
	GetUsername() string
	GetNickname() string
	GetUUID() uuid.UUID
	GetUserId() uint
	GetAuthorityId() uint
	GetUserInfo() any
	GetUserEmail() string // Extend add mail
}

var _ Login = new(SysUser)

type SysUser struct {
	global.GVA_MODEL
	UUID          uuid.UUID      `json:"uuid" gorm:"index;comment:用户UUID"`                                                                   // 用户UUID
	Username      string         `json:"userName" gorm:"index;comment:用户登录名"`                                                                // 用户登录名
	Password      string         `json:"-"  gorm:"comment:用户登录密码"`                                                                           // 用户登录密码
	NickName      string         `json:"nickName" gorm:"default:系统用户;comment:用户昵称"`                                                          // 用户昵称
	HeaderImg     string         `json:"headerImg" gorm:"default:https://qmplusimg.henrongyi.top/1576554439myAvatar.png;comment:用户头像"`       // 用户头像
	AuthorityId   uint           `json:"authorityId" gorm:"default:888;comment:用户角色ID"`                                                      // 用户角色ID
	Authority     SysAuthority   `json:"authority" gorm:"foreignKey:AuthorityId;references:AuthorityId;comment:用户角色"`                        // 用户角色
	Authorities   []SysAuthority `json:"authorities" gorm:"many2many:sys_user_authority;"`                                                   // 多用户角色
	Phone         string         `json:"phone"  gorm:"comment:用户手机号"`                                                                        // 用户手机号
	Email         string         `json:"email"  gorm:"index;comment:用户邮箱"`                                                                   // 用户邮箱
	Enable        int            `json:"enable" gorm:"default:1;comment:用户是否被冻结 1正常 2冻结"`                                                    //用户是否被冻结 1正常 2冻结
	OriginSetting common.JSONMap `json:"originSetting" form:"originSetting" gorm:"type:text;default:null;column:origin_setting;comment:配置;"` //配置
}

func (SysUser) TableName() string {
	return "sys_users"
}

func (s *SysUser) GetUsername() string {
	return s.Username
}

func (s *SysUser) GetNickname() string {
	return s.NickName
}

func (s *SysUser) GetUUID() uuid.UUID {
	return s.UUID
}

func (s *SysUser) GetUserId() uint {
	return s.ID
}

func (s *SysUser) GetAuthorityId() uint {
	return s.AuthorityId
}

func (s *SysUser) GetUserInfo() any {
	return *s
}

// Extend: Start Get the corresponding GAIA platform user information

func (s *SysUser) GetUserEmail() string {
	return s.Email
}

const UserActive = 1           // 用户状态: 活跃
const UserDeactivate = 2       // 用户状态: 停用
const AdminGroupID = uint(888) // 管理员组ID
const DefaultGroupID = uint(1) // 普通用户组ID

// GetAccount
// @description: Get user information through the user provider relationship table
// @return account gaia.Account, err error
func (s SysUser) GetAccount() (account gaia.Account, err error) {
	// init
	// get account
	if err = global.GVA_DB.Where("email=?", s.Email).First(&account).Error; err != nil {
		return account, errors.New("cannot find a user related to the database")
	}
	// return
	return account, nil
}

// SyncGaiaStatus
// @description: Sync user status to GAIA platform
// @return enable int
func (s SysUser) SyncGaiaStatus(enable int) {
	key := fmt.Sprintf("login_error_rate_limit:%s", s.Email)
	if enable == UserActive {
		global.GVA_REDIS.Del(context.Background(), key)
	} else {
		global.GVA_REDIS.Set(context.Background(), key, global.GVA_CONFIG.Gaia.LoginMaxErrorLimit, time.Hour*24)
	}

}

// Extend: Stop Get the corresponding GAIA platform user information

// Extend: Start global code

type SysUserGlobalCode struct {
	global.GVA_MODEL
	UserID uint `json:"user_id" gorm:"index;comment:用户id"`
}

// Extend: Stop global code
