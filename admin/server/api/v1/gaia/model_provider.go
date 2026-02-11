package gaia

import (
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/model/common/response"
	"github.com/flipped-aurora/gin-vue-admin/server/service"
	"github.com/flipped-aurora/gin-vue-admin/server/utils"
	"github.com/gin-gonic/gin"
	"go.uber.org/zap"
)

type ModelProviderApi struct{}

var modelProviderService = service.ServiceGroupApp.GaiaServiceGroup.ModelProviderService

// GetProviderList 获取提供商配置列表
// @Tags ModelProvider
// @Summary 获取提供商配置列表
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Success 200 {object} response.Response{data=[]gaiaResponse.ProviderListItem,msg=string} "获取成功"
// @Router /gaia/model-provider/list [get]
func (m *ModelProviderApi) GetProviderList(c *gin.Context) {
	list, err := modelProviderService.GetProviderList()
	if err != nil {
		global.GVA_LOG.Error("获取提供商配置列表失败", zap.Error(err))
		response.FailWithMessage("获取失败: "+err.Error(), c)
		return
	}
	response.OkWithData(list, c)
}

// UpdateProviderConfig 更新提供商配置
// @Tags ModelProvider
// @Summary 更新提供商配置
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Param data body object true "提供商配置"
// @Success 200 {object} response.Response{msg=string} "更新成功"
// @Router /gaia/model-provider/update [post]
func (m *ModelProviderApi) UpdateProviderConfig(c *gin.Context) {
	var req struct {
		ProviderName string   `json:"provider_name" binding:"required"`
		Enabled      bool     `json:"enabled"`
		Models       []string `json:"models"`
	}

	if err := c.ShouldBindJSON(&req); err != nil {
		response.FailWithMessage("参数错误: "+err.Error(), c)
		return
	}

	if err := modelProviderService.UpdateProviderConfig(req.ProviderName, req.Enabled, req.Models); err != nil {
		global.GVA_LOG.Error("更新提供商配置失败", zap.String("provider", req.ProviderName), zap.Error(err))
		response.FailWithMessage("更新失败: "+err.Error(), c)
		return
	}

	response.OkWithMessage("更新成功", c)
}

// GetModels 获取开启的模型列表（OpenAI格式）
// @Tags ModelProvider
// @Summary 获取开启的模型列表
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Success 200 {object} gaiaResponse.OpenAIModelsResponse "获取成功"
// @Router /gaia/models [get]
func (m *ModelProviderApi) GetModels(c *gin.Context) {
	models, err := modelProviderService.GetEnabledModels()
	if err != nil {
		global.GVA_LOG.Error("获取模型列表失败", zap.Error(err))
		c.JSON(http.StatusInternalServerError, gin.H{
			"error": gin.H{
				"message": "获取模型列表失败: " + err.Error(),
			},
		})
		return
	}

	c.JSON(http.StatusOK, models)
}

// Proxy 通用中转 API：将 /gaia/proxy/* 的请求按路径转发到上游（如 /v1/chat/completions、/v1/messages、/v1/images/generations、/v1/embeddings 等）。
// 上游 base 优先使用 provider_credentials 的 openai_api_base（如 "https://yunwu.ai"），便于计费区分。
// @Tags ModelProvider
// @Summary 通用中转API（按路径转发）
// @Security ApiKeyAuth
// @Param path path string true "上游路径，如 v1/chat/completions、v1/messages"
// @Router /gaia/proxy/*path [get,post,put,patch,delete]
func (m *ModelProviderApi) Proxy(c *gin.Context) {
	// init
	var err error
	var body []byte
	path := c.Param("path")
	userID := utils.GetUserUuid(c).String()
	if path == "" || path == "/" {
		c.JSON(http.StatusBadRequest, gin.H{"error": gin.H{"message": "代理路径不能为空"}})
		return
	}
	// 将 query provider 转为请求头，供 service 解析
	reqHeader := c.Request.Header.Clone()
	if q := strings.TrimSpace(c.Query("provider")); q != "" {
		reqHeader.Set("X-Gaia-Provider", q)
	}

	if body, err = io.ReadAll(c.Request.Body); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": gin.H{"message": "读取请求体失败"}})
		return
	}

	if err = modelProviderService.ProxyRequest(
		userID, path, c.Request.Method, reqHeader, body, c.Writer); err != nil {
		global.GVA_LOG.Error("代理请求失败", zap.String("user_id", userID), zap.String(
			"path", path), zap.Error(err))
		if !c.Writer.Written() {
			c.JSON(http.StatusInternalServerError, gin.H{"error": gin.H{"message": err.Error()}})
		}
	}
}

// GetAvailableModels 获取提供商的可用模型
// @Tags ModelProvider
// @Summary 获取提供商的可用模型
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Param provider_name query string true "提供商名称"
// @Success 200 {object} response.Response{data=[]gaiaResponse.ModelInfo,msg=string} "获取成功"
// @Router /gaia/model-provider/available-models [get]
func (m *ModelProviderApi) GetAvailableModels(c *gin.Context) {
	providerName := c.Query("provider_name")
	if providerName == "" {
		response.FailWithMessage("参数错误: provider_name不能为空", c)
		return
	}

	models, err := modelProviderService.GetAvailableModelsFromDify(providerName)
	if err != nil {
		global.GVA_LOG.Error("获取可用模型失败", zap.String("provider", providerName), zap.Error(err))
		response.FailWithMessage("获取失败: "+err.Error(), c)
		return
	}

	response.OkWithData(models, c)
}

// TestProviderCredentials 测试提供商凭证
// @Tags ModelProvider
// @Summary 测试提供商凭证
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Param provider_name query string true "提供商名称"
// @Success 200 {object} response.Response{msg=string} "测试成功"
// @Router /gaia/model-provider/test-credentials [get]
func (m *ModelProviderApi) TestProviderCredentials(c *gin.Context) {
	providerName := c.Query("provider_name")
	if providerName == "" {
		response.FailWithMessage("参数错误: provider_name不能为空", c)
		return
	}

	creds, err := modelProviderService.GetDifyProviderCredentials(providerName)
	if err != nil {
		global.GVA_LOG.Error("获取提供商凭证失败", zap.String("provider", providerName), zap.Error(err))
		response.FailWithMessage("获取凭证失败: "+err.Error(), c)
		return
	}

	// 隐藏API Key的大部分内容
	maskedKey := ""
	if len(creds.APIKey) > 8 {
		maskedKey = creds.APIKey[:4] + "****" + creds.APIKey[len(creds.APIKey)-4:]
	} else {
		maskedKey = "****"
	}

	result := map[string]interface{}{
		"provider":    providerName,
		"has_api_key": creds.APIKey != "",
		"api_key":     maskedKey,
	}

	response.OkWithData(result, c)
}

// GetProxyLogs 获取代理日志
// @Tags ModelProvider
// @Summary 获取代理日志
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Param page query int false "页码"
// @Param page_size query int false "每页数量"
// @Success 200 {object} response.Response{data=map[string]interface{},msg=string} "获取成功"
// @Router /gaia/model-provider/logs [get]
func (m *ModelProviderApi) GetProxyLogs(c *gin.Context) {
	page := c.DefaultQuery("page", "1")
	pageSize := c.DefaultQuery("page_size", "20")

	var pageInt, pageSizeInt int
	if _, err := fmt.Sscanf(page, "%d", &pageInt); err != nil {
		pageInt = 1
	}
	if _, err := fmt.Sscanf(pageSize, "%d", &pageSizeInt); err != nil {
		pageSizeInt = 20
	}

	if pageInt < 1 {
		pageInt = 1
	}
	if pageSizeInt < 1 || pageSizeInt > 100 {
		pageSizeInt = 20
	}

	var logs []map[string]interface{}
	var total int64

	db := global.GVA_DB.Table("model_proxy_log")

	// 获取总数
	if err := db.Count(&total).Error; err != nil {
		global.GVA_LOG.Error("获取日志总数失败", zap.Error(err))
		response.FailWithMessage("获取失败: "+err.Error(), c)
		return
	}

	// 分页查询
	offset := (pageInt - 1) * pageSizeInt
	if err := db.Order("created_at DESC").Limit(pageSizeInt).Offset(
		offset).Find(&logs).Error; err != nil {
		global.GVA_LOG.Error("获取日志列表失败", zap.Error(err))
		response.FailWithMessage("获取失败: "+err.Error(), c)
		return
	}

	result := map[string]interface{}{
		"list":      logs,
		"total":     total,
		"page":      pageInt,
		"page_size": pageSizeInt,
	}

	response.OkWithData(result, c)
}
