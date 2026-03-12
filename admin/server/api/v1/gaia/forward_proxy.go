package gaia

import (
	"crypto/sha256"
	"fmt"
	"github.com/flipped-aurora/gin-vue-admin/server/global"
	gaiaModel "github.com/flipped-aurora/gin-vue-admin/server/model/gaia"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia/request"
	"github.com/gin-gonic/gin"
	"go.uber.org/zap"
	"net/http"
)

type ForwardProxyApi struct{}

// ForwardProxy 转发代理入口：免 JWT，通过 forwarding token + ding_id 鉴权并计费
// @Tags ForwardProxy
// @Summary GPT 转发代理（钉钉入口，无需 JWT）
// @Param X-Forward-Token header string false "转发 Token"
// @Param X-Ding-Id header string false "钉钉 ID"
// @Param forward_token query string false "转发 Token（Header 优先）"
// @Param ding_id query string false "钉钉 ID（Header 优先）"
// @Param path path string true "上游路径"
// @Router /gaia/forward/proxy/{path} [get,post,put,patch,delete]
func (f *ForwardProxyApi) ForwardProxy(c *gin.Context) {
	// 打印请求 Header，便于排查转发问题
	global.GVA_LOG.Info("ForwardProxy 请求头",
		zap.Any("headers", c.Request.Header),
		zap.String("method", c.Request.Method),
		zap.String("path", c.Request.URL.Path),
	)

	// 1. 读取转发配置
	integrate := systemIntegratedService.GetIntegratedConfig(gaiaModel.SystemIntegrationDingTalk)
	configMap, err := systemIntegratedService.ParseDingTalkConfig(integrate.Config)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": gin.H{"message": "配置解析失败"}})
		return
	}

	// 2. 获取并校验 forwarding token（存在有效 Token 即视为开启转发能力）
	dingId := c.GetHeader("X-Ding-Id")
	apiKey := c.GetHeader("X-Api-Key")
	bearer := c.GetHeader("Authorization")
	token := c.GetHeader("X-Forward-Token")

	if (len(bearer) > gaiaModel.BearerLength || len(apiKey) > gaiaModel.BearerLength) && len(dingId) == 0 {
		if len(bearer) > gaiaModel.BearerLength {
			if bearer[:gaiaModel.BearerLength] == "Bearer " {
				bearer = bearer[gaiaModel.BearerLength:]
			}
		} else if len(apiKey) > gaiaModel.BearerLength {
			if apiKey[:gaiaModel.BearerLength] == "Bearer " {
				bearer = apiKey[gaiaModel.BearerLength:]
			} else {
				bearer = apiKey
			}
		}

		if dingId, err = systemIntegratedService.ParseForwardToken(bearer, configMap.ForwardConfig.Tokens); err != nil {
			c.JSON(http.StatusUnauthorized, gin.H{"error": gin.H{"message": "Token 验证失败: " + err.Error()}})
			return
		}
	} else {
		if token == "" {
			token = c.Query("forward_token")
		}
		if token == "" {
			c.JSON(http.StatusUnauthorized, gin.H{"error": gin.H{"message": "缺少转发 Token"}})
			return
		}
		if !validateForwardToken(token, configMap.ForwardConfig.Tokens) {
			c.JSON(http.StatusUnauthorized, gin.H{"error": gin.H{"message": "无效的转发 Token"}})
			return
		}

		// 4. 获取 ding_id
		if dingId == "" {
			dingId = c.Query("ding_id")
		}
		if dingId == "" {
			c.JSON(http.StatusBadRequest, gin.H{"error": gin.H{"message": "缺少 ding_id"}})
			return
		}
	}

	// 5. 解析 account_id
	accountId, err := systemIntegratedService.ResolveAccountByDingId(dingId, configMap.EmailApi)
	if err != nil {
		global.GVA_LOG.Warn("ForwardProxy 用户解析失败", zap.String("ding_id", dingId), zap.Error(err))
		c.JSON(http.StatusBadRequest, gin.H{"error": gin.H{"message": "无法解析用户：" + err.Error()}})
		return
	}

	// 6. 复用与 Proxy 相同的转发逻辑（path/body/ProxyRequest）
	proxyWithAccountId(c, accountId)
}

// validateForwardToken 校验 token 是否在转发 Token 列表中（SHA256 比对）
func validateForwardToken(token string, tokens []request.ForwardToken) bool {
	hash := fmt.Sprintf("%x", sha256.Sum256([]byte(token)))
	for _, t := range tokens {
		if t.TokenHash == hash {
			return true
		}
	}
	return false
}
