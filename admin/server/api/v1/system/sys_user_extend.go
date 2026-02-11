package system

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"

	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/model/common/response"
	"github.com/flipped-aurora/gin-vue-admin/server/model/system"
	gaiaReq "github.com/flipped-aurora/gin-vue-admin/server/model/gaia/request"
	systemReq "github.com/flipped-aurora/gin-vue-admin/server/model/system/request"
	systemRes "github.com/flipped-aurora/gin-vue-admin/server/model/system/response"
	"github.com/flipped-aurora/gin-vue-admin/server/service"
	sysSvc "github.com/flipped-aurora/gin-vue-admin/server/service/system"
	"github.com/flipped-aurora/gin-vue-admin/server/utils"
	"github.com/gin-gonic/gin"
	"github.com/go-resty/resty/v2"
	"go.uber.org/zap"
	"gorm.io/gorm"
)

var gaiaSystemIntegratedService = service.ServiceGroupApp.GaiaServiceGroup.SystemIntegratedService

// Extend Start: sync user

// SyncUser
// @Tags     Base
// @Summary  用户同步
// @Produce   application/json
// @Param    data  body      systemReq.Login                                             true  "用户名, 密码, 验证码"
// @Success  200   {object}  response.Response{data=systemRes.LoginResponse,msg=string}  "返回包括用户信息,token,过期时间"
// @Router   /user/sync [post]
func (b *BaseApi) SyncUser(c *gin.Context) {
	userExtendService.SyncUser()
	response.OkWithMessage("同步中", c)
}

// Extend Stop: sync user

// OaLogin
// @Tags     Base
// @Summary  用户登录
// @Produce   application/json
// @Param    data  body      systemReq.Login                                             true  "用户名, 密码, 验证码"
// @Success  200   {object}  response.Response{data=systemRes.LoginResponse,msg=string}  "返回包括用户信息,token,过期时间"
// @Router   /base/oaLogin [post]
func (b *BaseApi) OaLogin(c *gin.Context) {
	var l systemReq.OaLoginReq
	err := c.ShouldBindJSON(&l)

	if err != nil {
		response.FailWithMessage(err.Error(), c)
		return
	}
	err = utils.Verify(l, utils.OaLoginVerify)
	if err != nil {
		response.FailWithMessage(err.Error(), c)
		return
	}
	/**
	请求获取accessToken
	*/
	clientGetToken := resty.New().R()
	var accessTokenResponse *resty.Response
	getTokenUrl := fmt.Sprintf("%s%s", global.GVA_CONFIG.OaLogin.Url, global.GVA_CONFIG.OaLogin.GetTokenByCodeApiPath)
	var postParams = map[string]string{
		"client_id":     global.GVA_CONFIG.OaLogin.Oauth2ClientId,
		"client_secret": global.GVA_CONFIG.OaLogin.Oauth2ClientSecret,
		"code":          l.AuthorizeCode,
		"grant_type":    "authorization_code",
		"redirect_uri":  "",
	}
	accessTokenResponse, err = clientGetToken.
		SetFormData(postParams).
		Post(getTokenUrl)
	if err != nil {
		global.GVA_LOG.Error("请求OA用户信息失败,响应数据为：", zap.Error(errors.New(accessTokenResponse.String())))
		response.FailWithMessage("请求OA用户信息失败："+err.Error(), c)
		return
	}
	tokenRes := accessTokenResponse.String()
	var oaAccessToken systemRes.OaAccessTokenRes
	err = json.Unmarshal([]byte(tokenRes), &oaAccessToken)
	if err != nil {
		global.GVA_LOG.Error("解析OA AccessToken接口返回数据失败,响应数据为：", zap.Error(errors.New(accessTokenResponse.String())))
		response.FailWithMessage("解析OA AccessToken接口返回数据失败："+err.Error(), c)
		return
	}

	/**
	请求OA，返回用户信息
	*/
	getUserInfoUrl := fmt.Sprintf("%s%s", global.GVA_CONFIG.OaLogin.Url, global.GVA_CONFIG.OaLogin.GetUserApiPath)
	clientGetUser := resty.New().R()
	var userInfoResponse *resty.Response
	userInfoResponse, err = clientGetUser.SetHeader("Authorization", oaAccessToken.AccessToken).Post(getUserInfoUrl)
	if err != nil {
		global.GVA_LOG.Error("请求OA用户信息失败,响应数据为：", zap.Error(errors.New(userInfoResponse.String())))
		response.FailWithMessage("请求OA用户信息失败："+err.Error(), c)
		return
	}
	userInfoRes := userInfoResponse.String()
	var oaUserInfo systemRes.OaUserInfoRes
	err = json.Unmarshal([]byte(userInfoRes), &oaUserInfo)
	if err != nil {
		global.GVA_LOG.Error("解析OA用户信息接口返回数据失败,响应数据为：", zap.Error(errors.New(userInfoRes)))
		global.GVA_LOG.Error("解析OA用户信息接口返回数据失败,请求Token为：", zap.String("", oaAccessToken.AccessToken))
		global.GVA_LOG.Error("解析OA用户信息接口返回数据失败,请求Token为：", zap.String("", accessTokenResponse.String()))
		response.FailWithMessage("解析OA用户信息接口返回数据失败："+err.Error(), c)
		return
	}

	// 查询数据库数据
	sysUser := &system.SysUser{}
	err = global.GVA_DB.Where("email", oaUserInfo.Data.Email).First(&sysUser).Error
	if err != nil && err != gorm.ErrRecordNotFound {
		response.FailWithMessage("查询数据库信息失败："+err.Error(), c)
		return
	}
	// 判断是否需要注册
	if sysUser.ID == 0 {
		// TODO 从ldap中获取用户详细数据

		// 注册用户
		sysUser = &system.SysUser{
			Username:    oaUserInfo.Data.Username,
			NickName:    oaUserInfo.Data.Username,
			HeaderImg:   "https://hn1.oss-cn-shenzhen.aliyuncs.com/w.jpg",
			AuthorityId: system.NormalAuthorityId,
			Authorities: []system.SysAuthority{{AuthorityId: system.NormalAuthorityId}},
			Enable:      1,
			//Phone:       r.Phone, // TODO 手机需要从ldap中获取用户详细数据
			Email:    oaUserInfo.Data.Email,
			Password: utils.RandomString(16),
		}
		var userReturn system.SysUser
		userReturn, err = userService.Register(*sysUser, "")
		if err != nil {
			global.GVA_LOG.Error("注册失败!", zap.Error(err))
			response.FailWithDetailed(systemRes.SysUserResponse{User: userReturn}, "注册失败", c)
			return
		} else {
			global.GVA_LOG.Info("注册成功！", zap.Any("username", userReturn.Username))
		}
		sysUser = &userReturn
	}

	var user *system.SysUser
	user, err = userExtendService.OaLogin(sysUser) // 注意这个方法不检查密码
	if err != nil {
		global.GVA_LOG.Error("登陆失败! 用户名不存在!", zap.Error(err))
		response.FailWithMessage("用户名不存在", c)
		return
	}
	if sysUser.Enable != 1 {
		global.GVA_LOG.Error("登陆失败! 用户被禁止登录!")
		response.FailWithMessage("用户被禁止登录", c)
		return
	}
	b.TokenNext(c, *user)
	return

}

// Extend Start: oAuth2 callback verification

// OAuth2Callback
// @Tags     Base
// @Summary  oAuth2回调校验
// @Produce   application/json
// @Param    code  query    string  true  "授权码"
// @Success  200   {string} string  "返回HTML内容，包含授权码"
// @Router   /base/auth2/callback [get]
func (b *BaseApi) OAuth2Callback(c *gin.Context) {
	// 获取授权码
	code := c.Request.URL.Query().Get("code")
	if code == "" {
		global.GVA_LOG.Error("OAuth2回调未获取到授权码")
		c.String(http.StatusBadRequest, "授权码不能为空")
		return
	}

	// 返回HTML内容，通过BroadcastChannel将授权码传递给前端
	htmlContent := fmt.Sprintf(`<html><body><script>const channel = new BroadcastChannel('oAuth2');channel.postMessage({code: '%s', timestamp: Date.now() });channel.close();window.close();</script></body></html>`, code)
	c.Header("Content-Type", "text/html; charset=utf-8")
	c.String(http.StatusOK, htmlContent)
}

// Extend Stop: oAuth2 callback verification

// GetGaiaLoginOptions 获取 Gaia 登录方式（钉钉/OAuth2 是否启用及授权地址），供登录页展示，无需鉴权
// @Tags     Base
// @Summary  获取登录方式选项
// @Produce  application/json
// @Param    origin  query    string  false  "前端 origin，用于拼回调地址"
// @Router   /base/gaiaLoginOptions [get]
func (b *BaseApi) GetGaiaLoginOptions(c *gin.Context) {
	origin := c.Query("origin")
	if origin == "" {
		origin = c.GetHeader("Origin")
	}
	if origin == "" {
		origin = strings.TrimSuffix(global.GVA_CONFIG.Gaia.Url, "/")
	}
	opts := gaiaSystemIntegratedService.GetLoginOptions(origin)
	response.OkWithData(opts, c)
}

// GaiaOAuth2Login 使用系统集成 OAuth2 的 code 或 access_token（Extend: 兼容 casdoor）登录，返回 JWT；若带 redirect_uri/state 则一并返回供前端回调第三方
// @Tags     Base
// @Summary  Gaia OAuth2 登录
// @Produce  application/json
// @Param    data  body  gaiaReq.GaiaOAuth2LoginReq  true  "code 或 access_token 二选一、redirect_uri、state"
// @Router   /base/gaiaOAuth2Login [post]
func (b *BaseApi) GaiaOAuth2Login(c *gin.Context) {
	var req gaiaReq.GaiaOAuth2LoginReq
	if err := c.ShouldBindJSON(&req); err != nil {
		response.FailWithMessage(err.Error(), c)
		return
	}
	result, err := gaiaSystemIntegratedService.OAuth2CodeLogin(req)
	if err != nil {
		global.GVA_LOG.Error("Gaia OAuth2 登录失败", zap.Error(err))
		response.FailWithMessage(err.Error(), c)
		return
	}
	sysSvc.MenuServiceApp.UserAuthorityDefaultRouter(&result.User)
	data := map[string]interface{}{
		"user":   result.User,
		"token":  result.Token,
		"expiresAt": 0,
	}
	if result.RedirectURI != "" {
		data["redirect_uri"] = result.RedirectURI
	}
	if result.State != "" {
		data["state"] = result.State
	}
	response.OkWithDetailed(data, "登录成功", c)
}

// GaiaDingTalkLogin 钉钉 code 登录，返回 JWT
// @Tags     Base
// @Summary  钉钉登录
// @Produce  application/json
// @Param    data  body  gaiaReq.GaiaDingTalkLoginReq  true  "auth_code、redirect_uri、state"
// @Router   /base/dingtalkLogin [post]
func (b *BaseApi) GaiaDingTalkLogin(c *gin.Context) {
	var req gaiaReq.GaiaDingTalkLoginReq
	if err := c.ShouldBindJSON(&req); err != nil {
		response.FailWithMessage(err.Error(), c)
		return
	}
	result, err := gaiaSystemIntegratedService.DingTalkCodeLogin(req)
	if err != nil {
		global.GVA_LOG.Error("钉钉登录失败", zap.Error(err))
		response.FailWithMessage(err.Error(), c)
		return
	}
	sysSvc.MenuServiceApp.UserAuthorityDefaultRouter(&result.User)
	data := map[string]interface{}{
		"user":   result.User,
		"token":  result.Token,
		"expiresAt": 0,
	}
	if result.RedirectURI != "" {
		data["redirect_uri"] = result.RedirectURI
	}
	if result.State != "" {
		data["state"] = result.State
	}
	response.OkWithDetailed(data, "登录成功", c)
}

// Extend Stop: gaia login
