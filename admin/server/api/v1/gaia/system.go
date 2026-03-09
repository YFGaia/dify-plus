package gaia

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/model/common/response"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia/request"
	"github.com/flipped-aurora/gin-vue-admin/server/model/system"
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

// GetForwardTokens 获取转发 Token 列表
// @Tags System
// @Summary 获取转发 Token 列表
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Success 200 {object} response.Response{data=[]request.ForwardToken,msg=string} "查询成功"
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

	// 返回不包含 token_hash 的列表
	type TokenInfo struct {
		ID        string    `json:"id"`
		CreatedAt time.Time `json:"created_at"`
	}

	tokens := make([]TokenInfo, 0, len(configMap.ForwardConfig.Tokens))
	for _, token := range configMap.ForwardConfig.Tokens {
		tokens = append(tokens, TokenInfo{
			ID:        token.ID,
			CreatedAt: token.CreatedAt,
		})
	}

	response.OkWithData(gin.H{"tokens": tokens, "count": len(tokens), "max": 20}, c)
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

	newToken := request.ForwardToken{
		ID:        tokenID,
		TokenHash: tokenHash,
		CreatedAt: time.Now(),
	}

	// 添加到配置
	configMap.ForwardConfig.Tokens = append(configMap.ForwardConfig.Tokens, newToken)
	configJSON, _ := json.Marshal(configMap)
	integrate.Config = string(configJSON)

	// 保存配置
	if err := systemIntegratedService.SetIntegratedConfig(integrate, "", false); err != nil {
		response.FailWithMessage("保存失败："+err.Error(), c)
		return
	}

	// 返回明文 token（仅此次展示）
	response.OkWithData(gin.H{
		"id":         tokenID,
		"token":      req.Token,
		"created_at": newToken.CreatedAt,
	}, c)
}

// DeleteForwardToken 删除转发 Token
// @Tags System
// @Summary 删除转发 Token
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Param id path string true "Token ID"
// @Param password body string true "当前用户密码"
// @Success 200 {object} response.Response{msg=string} "删除成功"
// @Router /gaia/system/forward-tokens/:id [delete]
func (systemApi *SystemApi) DeleteForwardToken(c *gin.Context) {
	tokenID := c.Param("id")
	if tokenID == "" {
		response.FailWithMessage("Token ID 不能为空", c)
		return
	}

	var req struct {
		Password string `json:"password"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		response.FailWithMessage(err.Error(), c)
		return
	}

	// 验证当前用户密码
	userID := utils.GetUserUuid(c).String()
	var user system.SysUser
	if err := global.GVA_DB.Select("password").First(&user, userID).Error; err != nil {
		response.FailWithMessage("查询用户失败："+err.Error(), c)
		return
	}
	if !utils.BcryptCheck(req.Password, user.Password) {
		response.FailWithMessage("密码错误", c)
		return
	}

	// 获取配置
	integrate := systemIntegratedService.GetIntegratedConfig(gaia.SystemIntegrationDingTalk)
	var configMap request.DingTalkConfigRequest
	if integrate.Config != "" {
		if err := json.Unmarshal([]byte(integrate.Config), &configMap); err != nil {
			response.FailWithMessage("解析配置失败："+err.Error(), c)
			return
		}
	}

	// 查找并删除 token
	found := false
	newTokens := make([]request.ForwardToken, 0, len(configMap.ForwardConfig.Tokens))
	for _, token := range configMap.ForwardConfig.Tokens {
		if token.ID == tokenID {
			found = true
			continue
		}
		newTokens = append(newTokens, token)
	}

	if !found {
		response.FailWithMessage("Token 不存在", c)
		return
	}

	// 更新配置
	configMap.ForwardConfig.Tokens = newTokens
	configJSON, _ := json.Marshal(configMap)
	integrate.Config = string(configJSON)

	if err := systemIntegratedService.SetIntegratedConfig(integrate, "", false); err != nil {
		response.FailWithMessage("保存失败："+err.Error(), c)
		return
	}

	response.OkWithMessage("删除成功", c)
}
