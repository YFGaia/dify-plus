package gaia

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/url"
	"strconv"
	"strings"

	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/model/common/response"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia/request"
	gaiaResp "github.com/flipped-aurora/gin-vue-admin/server/model/gaia/response"
	"github.com/flipped-aurora/gin-vue-admin/server/model/system"
	serviceGaia "github.com/flipped-aurora/gin-vue-admin/server/service/gaia"
	"github.com/flipped-aurora/gin-vue-admin/server/utils"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"time"
)

type SystemApi struct{}

// GetDingTalk 获取钉钉系统配置
// @Tags System
// @Summary 获取钉钉系统配置
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Param data query gaia.Tenants true "用id查询tenants表"
// @Success 200 {object} response.Response{data=gaia.Tenants,msg=string} "查询成功"
// @Router /gaia/system/dingtalk [get]
func (systemApi *SystemApi) GetDingTalk(c *gin.Context) {
	var host string
	var config = make(map[string]interface{})
	if host, _ = global.GVA_Dify_REDIS.Get(context.Background(), "api_host").Result(); len(host) == 0 {
		host = global.GVA_CONFIG.Gaia.Url
	}
	config["host"] = host
	config["config"] = systemIntegratedService.GetIntegratedConfig(gaia.SystemIntegrationDingTalk)
	response.OkWithData(config, c)
}

// SetDingTalk 设置钉钉系统配置
// @Tags System
// @Summary 设置钉钉系统配置
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Param data query gaia.Tenants true "用id查询tenants表"
// @Success 200 {object} response.Response{data=gaia.Tenants,msg=string} "查询成功"
// @Router /gaia/system/dingtalk [post]
func (systemApi *SystemApi) SetDingTalk(c *gin.Context) {
	var err error
	var req gaia.SystemIntegration
	if err = c.ShouldBindJSON(&req); err != nil {
		response.FailWithMessage(err.Error(), c)
		return
	}
	// update
	req.Classify = gaia.SystemIntegrationDingTalk
	if err = systemIntegratedService.SetIntegratedConfig(
		req, "", req.Test); err != nil {
		response.FailWithMessage(err.Error(), c)
		return
	}
	response.OkWithData("ok", c)
}

// TestEmailApiConfig 测试第三方邮箱 API 配置
// @Tags System
// @Summary 测试第三方邮箱 API 配置
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Param data body request.TestEmailApiConfigRequest true "测试配置请求"
// @Success 200 {object} response.Response{data=gaiaResp.TestEmailApiConfigResponse,msg=string} "测试结果"
// @Router /gaia/system/dingtalk/test-email-config [post]
func (systemApi *SystemApi) TestEmailApiConfig(c *gin.Context) {
	var req request.TestEmailApiConfigRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		response.FailWithMessage(err.Error(), c)
		return
	}

	result, err := systemIntegratedService.TestEmailApiConfig(req.Config, req.TestDingID)
	if err != nil {
		response.FailWithMessage("测试失败："+err.Error(), c)
		return
	}

	response.OkWithData(result, c)
}

// GetDingTalkTestAuthURL 获取「测试连接」用的钉钉授权 URL，打开后扫码完成即视为连接成功
// @Router /gaia/system/dingtalk/test-auth-url [get]
func (systemApi *SystemApi) GetDingTalkTestAuthURL(c *gin.Context) {
	origin := c.GetHeader("Referer")
	if origin == "" {
		origin = c.GetHeader("Origin")
	}
	if origin != "" {
		if u, err := url.Parse(origin); err == nil {
			origin = u.Scheme + "://" + u.Host + strings.TrimSuffix(u.Path, "/")
		}
	}
	if origin == "" {
		response.FailWithMessage("无法获取前端地址，请从配置页点击「测试连接」", c)
		return
	}
	authURL, err := systemIntegratedService.GetDingTalkTestAuthURL(origin)
	if err != nil {
		response.FailWithMessage(err.Error(), c)
		return
	}
	response.OkWithData(gin.H{"auth_url": authURL}, c)
}

// DingTalkTestCallback 测试连接回调：仅用 code 换 token 验证，不登录
// @Router /gaia/system/dingtalk/test-callback [post]
func (systemApi *SystemApi) DingTalkTestCallback(c *gin.Context) {
	var req struct {
		Code string `json:"code"`
	}
	if err := c.ShouldBindJSON(&req); err != nil || strings.TrimSpace(req.Code) == "" {
		response.FailWithMessage("缺少授权码 code", c)
		return
	}
	if err := systemIntegratedService.DingTalkTestCallback(req.Code); err != nil {
		response.FailWithMessage("验证失败: "+err.Error(), c)
		return
	}
	response.OkWithMessage("验证成功", c)
}

// GetForwardTokens 获取转发 Token 列表
// @Tags System
// @Summary 获取转发 Token 列表
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Success 200 {object} response.Response{data=gaiaResp.ForwardTokensResponse,msg=string} "查询成功"
// @Router /gaia/system/forward-tokens [get]
func (systemApi *SystemApi) GetForwardTokens(c *gin.Context) {
	integrate := systemIntegratedService.GetIntegratedConfig(gaia.SystemIntegrationDingTalk)

	var configMap request.DingTalkConfigRequest
	if integrate.Config != "" {
		if err := json.Unmarshal([]byte(integrate.Config), &configMap); err != nil {
			response.FailWithMessage("解析配置失败："+err.Error(), c)
			return
		}
	}

	tokens := make([]gaiaResp.ForwardTokenInfo, 0, len(configMap.ForwardConfig.Tokens))
	for i, token := range configMap.ForwardConfig.Tokens {
		tokens = append(tokens, gaiaResp.ForwardTokenInfo{
			ID:        utils.AddAsteriskToString(token.TokenSecret),
			CreatedAt: token.CreatedAt,
			Seq:       i + 1,
		})
	}

	response.OkWithData(gaiaResp.ForwardTokensResponse{Tokens: tokens, Count: len(tokens), Max: 20}, c)
}

// CreateForwardToken 新增转发 Token
// @Tags System
// @Summary 新增转发 Token
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Param token body string true "Token 明文"
// @Success 200 {object} response.Response{data=request.ForwardToken,msg=string} "创建成功"
// @Router /gaia/system/forward-tokens [post]
func (systemApi *SystemApi) CreateForwardToken(c *gin.Context) {
	var req struct {
		Token string `json:"token"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		response.FailWithMessage(err.Error(), c)
		return
	}

	if req.Token == "" {
		response.FailWithMessage("Token 不能为空", c)
		return
	}

	integrate := systemIntegratedService.GetIntegratedConfig(gaia.SystemIntegrationDingTalk)
	var configMap request.DingTalkConfigRequest
	if integrate.Config != "" {
		if err := json.Unmarshal([]byte(integrate.Config), &configMap); err != nil {
			response.FailWithMessage("解析配置失败："+err.Error(), c)
			return
		}
	}

	// 检查数量限制
	if len(configMap.ForwardConfig.Tokens) >= 20 {
		response.FailWithMessage("转发 Token 最多 20 个", c)
		return
	}

	// 生成唯一 ID 和哈希
	tokenID := "tok_" + uuid.New().String()
	tokenHash := fmt.Sprintf("%x", sha256.Sum256([]byte(req.Token)))
	// 生成 HMAC 签名密钥（仅创建时回传一次）
	secretBytes := make([]byte, 32)
	if _, err := rand.Read(secretBytes); err != nil {
		response.FailWithMessage("生成 TokenSecret 失败："+err.Error(), c)
		return
	}
	tokenSecret := base64.RawURLEncoding.EncodeToString(secretBytes)

	newToken := request.ForwardToken{
		ID:          tokenID,
		TokenHash:   tokenHash,
		CreatedAt:   time.Now(),
		TokenSecret: tokenSecret,
	}

	// 添加到配置
	configMap.ForwardConfig.Tokens = append(configMap.ForwardConfig.Tokens, newToken)
	seq := len(configMap.ForwardConfig.Tokens) // 1..N
	configJSON, _ := json.Marshal(configMap)
	integrate.Config = string(configJSON)

	// 保存配置
	if err := systemIntegratedService.SetIntegratedConfig(integrate, "", false); err != nil {
		response.FailWithMessage("保存失败："+err.Error(), c)
		return
	}

	// 返回明文 token（仅此次展示）
	response.OkWithData(gin.H{
		"seq":          seq,
		"token":        req.Token,
		"token_secret": tokenSecret,
		"created_at":   newToken.CreatedAt,
	}, c)
}

// DeleteForwardToken 删除转发 Token
// @Tags System
// @Summary 删除转发 Token
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Param seq path int true "Token 序列号（从列表获取，1..N）"
// @Param password body string true "当前用户密码"
// @Success 200 {object} response.Response{msg=string} "删除成功"
// @Router /gaia/system/forward-tokens/:seq [delete]
func (systemApi *SystemApi) DeleteForwardToken(c *gin.Context) {
	seqStr := c.Param("seq")
	if seqStr == "" {
		response.FailWithMessage("Token 序列号不能为空", c)
		return
	}
	seq, err := strconv.Atoi(seqStr)
	if err != nil || seq <= 0 {
		response.FailWithMessage("Token 序列号非法", c)
		return
	}

	var req struct {
		Password string `json:"password"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		response.FailWithMessage(err.Error(), c)
		return
	}

	// 验证当前用户密码（使用 Dify account 密码体系）
	userID := utils.GetUserUuid(c).String()
	var user system.SysUser
	if err := global.GVA_DB.Select("email").Where(
		"uuid = ?", userID).First(&user).Error; err != nil {
		response.FailWithMessage("查询用户失败："+err.Error(), c)
		return
	}
	account, err := user.GetAccount()
	if err != nil {
		response.FailWithMessage("查询账号失败："+err.Error(), c)
		return
	}
	var pwd serviceGaia.PasswdEncode
	if ok, pwdErr := pwd.ComparePassword(
		req.Password, account.Password, account.PasswordSalt); pwdErr != nil || !ok {
		response.FailWithMessage("密码错误", c)
		return
	}

	// 获取配置
	integrate := systemIntegratedService.GetIntegratedConfig(gaia.SystemIntegrationDingTalk)
	var configMap request.DingTalkConfigRequest
	if integrate.Config != "" {
		if err = json.Unmarshal([]byte(integrate.Config), &configMap); err != nil {
			response.FailWithMessage("解析配置失败："+err.Error(), c)
			return
		}
	}

	// 查找并删除 token
	if seq > len(configMap.ForwardConfig.Tokens) {
		response.FailWithMessage("Token 不存在", c)
		return
	}
	idx := seq - 1
	newTokens := make([]request.ForwardToken, 0, len(configMap.ForwardConfig.Tokens)-1)
	newTokens = append(newTokens, configMap.ForwardConfig.Tokens[:idx]...)
	newTokens = append(newTokens, configMap.ForwardConfig.Tokens[idx+1:]...)

	// 更新配置
	configMap.ForwardConfig.Tokens = newTokens
	configJSON, _ := json.Marshal(configMap)
	integrate.Config = string(configJSON)

	if err = systemIntegratedService.SetIntegratedConfig(integrate, "", false); err != nil {
		response.FailWithMessage("保存失败："+err.Error(), c)
		return
	}

	response.OkWithMessage("删除成功", c)
}
