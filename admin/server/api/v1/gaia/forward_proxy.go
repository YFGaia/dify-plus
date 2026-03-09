package gaia

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/flipped-aurora/gin-vue-admin/server/global"
	gaiaModel "github.com/flipped-aurora/gin-vue-admin/server/model/gaia"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia/request"
	"github.com/gin-gonic/gin"
	"go.uber.org/zap"
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
	// 1. 读取转发配置
	integrate := systemIntegratedService.GetIntegratedConfig(gaiaModel.SystemIntegrationDingTalk)
	var configMap request.DingTalkConfigRequest
	if integrate.Config != "" {
		if err := json.Unmarshal([]byte(integrate.Config), &configMap); err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": gin.H{"message": "配置解析失败"}})
			return
		}
	}

	// 2. 检查转发开关
	if !configMap.ForwardConfig.Enabled {
		c.JSON(http.StatusNotFound, gin.H{"error": gin.H{"message": "转发功能未开启"}})
		return
	}

	// 3. 获取并校验 forwarding token
	token := c.GetHeader("X-Forward-Token")
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
	dingId := c.GetHeader("X-Ding-Id")
	if dingId == "" {
		dingId = c.Query("ding_id")
	}
	if dingId == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": gin.H{"message": "缺少 ding_id"}})
		return
	}

	// 5. 解析 account_id
	accountId, err := systemIntegratedService.ResolveAccountByDingId(dingId, configMap.ForwardConfig.DingIdApi)
	if err != nil {
		global.GVA_LOG.Warn("ForwardProxy 用户解析失败", zap.String("ding_id", dingId), zap.Error(err))
		c.JSON(http.StatusBadRequest, gin.H{"error": gin.H{"message": "无法解析用户：" + err.Error()}})
		return
	}

	// 6. 转发请求
	path := c.Param("path")
	if path == "" || path == "/" {
		c.JSON(http.StatusBadRequest, gin.H{"error": gin.H{"message": "代理路径不能为空"}})
		return
	}

	// 将 query provider 转为请求头，供 service 解析
	reqHeader := c.Request.Header.Clone()
	if q := strings.TrimSpace(c.Query("provider")); q != "" {
		reqHeader.Set("X-Gaia-Provider", q)
	}

	body, err := io.ReadAll(c.Request.Body)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": gin.H{"message": "读取请求体失败"}})
		return
	}

	global.GVA_LOG.Info("ForwardProxy 请求",
		zap.String("ding_id", dingId),
		zap.String("account_id", accountId),
		zap.String("path", path),
		zap.String("method", c.Request.Method),
		zap.Int("body_len", len(body)),
	)

	if err = modelProviderService.ProxyRequest(
		accountId, path, c.Request.Method, reqHeader, body, c.Writer); err != nil {
		global.GVA_LOG.Error("ForwardProxy 转发失败",
			zap.String("account_id", accountId),
			zap.String("path", path),
			zap.Error(err),
		)
		if !c.Writer.Written() {
			c.JSON(http.StatusInternalServerError, gin.H{"error": gin.H{"message": err.Error()}})
		}
	}
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
