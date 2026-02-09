package gaia

import (
	"strconv"

	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/model/common/response"
	gaiaReq "github.com/flipped-aurora/gin-vue-admin/server/model/gaia/request"
	"github.com/flipped-aurora/gin-vue-admin/server/service"
	"github.com/flipped-aurora/gin-vue-admin/server/utils"
	"github.com/gin-gonic/gin"
	"go.uber.org/zap"
)

type AppVersionApi struct{}

var appVersionService = service.ServiceGroupApp.GaiaServiceGroup.AppVersionService

// GetLatest 客户端获取最新版本（公开接口）
// @Tags AppVersion
// @Summary 客户端获取最新版本
// @accept application/json
// @Produce application/json
// @Param platform query string true "平台(如 win32/darwin/linux)"
// @Param arch query string false "架构(可选，未传或未匹配时取该 platform 第一个包)"
// @Param token query string false "Token(若后台配置了则必填)"
// @Success 200 {object} response.Response "获取成功"
// @Router /latest [get]
func (appVersionApi *AppVersionApi) GetLatest(c *gin.Context) {
	platform := c.Query("platform")
	arch := c.Query("arch")
	token := c.Query("token")
	if platform == "" {
		response.FailWithMessage("platform is required", c)
		return
	}
	resp, code := appVersionService.GetLatest(platform, arch, token)
	if code == 401 {
		response.NoAuth("token required or invalid", c)
		return
	}
	if code == 404 {
		response.FailWithMessage("no package for this platform/arch", c)
		return
	}
	if code != 200 {
		global.GVA_LOG.Error("GetLatest failed", zap.Int("code", code))
		response.FailWithMessage("internal error", c)
		return
	}
	response.OkWithData(resp, c)
}

func buildDownloadUrl(c *gin.Context) func(string) string {
	prefix := global.GVA_CONFIG.System.RouterPrefix
	return func(path string) string {
		scheme := "https"
		if c.GetHeader("X-Forwarded-Proto") != "" {
			scheme = c.GetHeader("X-Forwarded-Proto")
		} else if c.Request.TLS == nil {
			scheme = "http"
		}
		host := c.Request.Host
		if path == "" {
			return ""
		}
		if path[0] != '/' {
			path = "/" + path
		}
		if prefix != "" {
			return scheme + "://" + host + prefix + path
		}
		return scheme + "://" + host + path
	}
}

// GetTokenConfig 管理端获取全局 Token 配置
// @Tags AppVersion
// @Summary 管理端获取全局 Token 配置
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Success 200 {object} response.Response "获取成功"
// @Router /gaia/app-version/token [get]
func (appVersionApi *AppVersionApi) GetTokenConfig(c *gin.Context) {
	cfg, err := appVersionService.GetTokenConfig()
	if err != nil {
		global.GVA_LOG.Error("获取Token配置失败!", zap.Error(err))
		response.FailWithMessage("获取失败:"+err.Error(), c)
		return
	}
	response.OkWithData(cfg, c)
}

// SetTokenConfig 管理端设置全局 Token
// @Tags AppVersion
// @Summary 管理端设置全局 Token
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Param data body gaiaReq.AppVersionTokenConfig true "Token 配置"
// @Success 200 {object} response.Response "设置成功"
// @Router /gaia/app-version/token [put]
func (appVersionApi *AppVersionApi) SetTokenConfig(c *gin.Context) {
	var req gaiaReq.AppVersionTokenConfig
	if err := c.ShouldBindJSON(&req); err != nil {
		response.FailWithMessage(err.Error(), c)
		return
	}
	if err := appVersionService.SetTokenConfig(req); err != nil {
		global.GVA_LOG.Error("设置Token配置失败!", zap.Error(err))
		response.FailWithMessage(err.Error(), c)
		return
	}
	response.Ok(c)
}

// RevealToken 输入登录密码验证后返回明文 Token
// @Tags AppVersion
// @Summary 输入登录密码验证后返回明文 Token
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Param data body gaiaReq.AppVersionTokenReveal true "密码"
// @Success 200 {object} response.Response "获取成功"
// @Router /gaia/app-version/token/reveal [post]
func (appVersionApi *AppVersionApi) RevealToken(c *gin.Context) {
	var req gaiaReq.AppVersionTokenReveal
	if err := c.ShouldBindJSON(&req); err != nil {
		response.FailWithMessage(err.Error(), c)
		return
	}
	userID := utils.GetUserID(c)
	token, err := appVersionService.RevealToken(userID, req.Password)
	if err != nil {
		global.GVA_LOG.Error("RevealToken失败!", zap.Error(err))
		response.FailWithMessage(err.Error(), c)
		return
	}
	response.OkWithData(gin.H{"token": token}, c)
}

// ListReleases 版本列表
// @Tags AppVersion
// @Summary 版本列表
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Success 200 {object} response.Response "获取成功"
// @Router /gaia/app-version/releases [get]
func (appVersionApi *AppVersionApi) ListReleases(c *gin.Context) {
	list, err := appVersionService.ListReleases()
	if err != nil {
		global.GVA_LOG.Error("获取版本列表失败!", zap.Error(err))
		response.FailWithMessage("获取失败:"+err.Error(), c)
		return
	}
	response.OkWithData(list, c)
}

// CreateRelease 新增版本
// @Tags AppVersion
// @Summary 新增版本
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Param data body gaiaReq.AppVersionReleaseCreate true "版本信息"
// @Success 200 {object} response.Response "创建成功"
// @Router /gaia/app-version/releases [post]
func (appVersionApi *AppVersionApi) CreateRelease(c *gin.Context) {
	var req gaiaReq.AppVersionReleaseCreate
	if err := c.ShouldBindJSON(&req); err != nil {
		response.FailWithMessage(err.Error(), c)
		return
	}
	r, err := appVersionService.CreateRelease(req)
	if err != nil {
		global.GVA_LOG.Error("创建版本失败!", zap.Error(err))
		response.FailWithMessage(err.Error(), c)
		return
	}
	response.OkWithData(r, c)
}

// GetRelease 获取单个版本详情
// @Tags AppVersion
// @Summary 获取单个版本详情
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Param id path int true "版本 ID"
// @Success 200 {object} response.Response "获取成功"
// @Router /gaia/app-version/releases/:id [get]
func (appVersionApi *AppVersionApi) GetRelease(c *gin.Context) {
	idStr := c.Param("id")
	id, err := strconv.ParseUint(idStr, 10, 32)
	if err != nil {
		response.FailWithMessage("无效的版本 id", c)
		return
	}
	detail, err := appVersionService.GetReleaseByID(uint(id))
	if err != nil {
		global.GVA_LOG.Error("获取版本详情失败!", zap.Error(err))
		response.FailWithMessage(err.Error(), c)
		return
	}
	response.OkWithData(detail, c)
}

// UpdateRelease 更新版本信息
// @Tags AppVersion
// @Summary 更新版本信息
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Param id path int true "版本 ID"
// @Param data body gaiaReq.AppVersionReleaseUpdate true "版本信息"
// @Success 200 {object} response.Response "更新成功"
// @Router /gaia/app-version/releases/:id [put]
func (appVersionApi *AppVersionApi) UpdateRelease(c *gin.Context) {
	idStr := c.Param("id")
	id, err := strconv.ParseUint(idStr, 10, 32)
	if err != nil {
		response.FailWithMessage("无效的版本 id", c)
		return
	}
	var req gaiaReq.AppVersionReleaseUpdate
	if err = c.ShouldBindJSON(&req); err != nil {
		response.FailWithMessage(err.Error(), c)
		return
	}
	if err = appVersionService.UpdateRelease(uint(id), req); err != nil {
		global.GVA_LOG.Error("更新版本失败!", zap.Error(err))
		response.FailWithMessage(err.Error(), c)
		return
	}
	response.Ok(c)
}

// UploadToRelease 上传安装包到指定版本（根据文件名自动识别平台/架构）
// @Tags AppVersion
// @Summary 上传安装包到指定版本
// @Security ApiKeyAuth
// @accept multipart/form-data
// @Produce application/json
// @Param id path int true "版本 ID"
// @Param file formData file true "安装包文件"
// @Success 200 {object} response.Response "上传成功"
// @Router /gaia/app-version/releases/:id/upload [post]
func (appVersionApi *AppVersionApi) UploadToRelease(c *gin.Context) {
	idStr := c.Param("id")
	id, err := strconv.ParseUint(idStr, 10, 32)
	if err != nil {
		response.FailWithMessage("无效的版本 id", c)
		return
	}
	file, err := c.FormFile("file")
	if err != nil {
		response.FailWithMessage("请选择文件: "+err.Error(), c)
		return
	}
	if err = appVersionService.UploadPackageToRelease(uint(id), file, buildDownloadUrl(c)); err != nil {
		global.GVA_LOG.Error("上传安装包失败!", zap.Error(err))
		response.FailWithMessage(err.Error(), c)
		return
	}
	response.Ok(c)
}

// DeleteDownload 删除指定版本下某 platform/arch 的包
// @Tags AppVersion
// @Summary 删除指定版本下某 platform/arch 的包
// @Security ApiKeyAuth
// @accept application/json
// @Produce application/json
// @Param id path int true "版本 ID"
// @Param platform query string true "平台"
// @Param arch query string true "架构"
// @Success 200 {object} response.Response "删除成功"
// @Router /gaia/app-version/releases/:id/download [delete]
func (appVersionApi *AppVersionApi) DeleteDownload(c *gin.Context) {
	idStr := c.Param("id")
	id, err := strconv.ParseUint(idStr, 10, 32)
	if err != nil {
		response.FailWithMessage("无效的版本 id", c)
		return
	}
	var q gaiaReq.AppVersionDeleteQuery
	if err = c.ShouldBindQuery(&q); err != nil {
		response.FailWithMessage(err.Error(), c)
		return
	}
	if err = appVersionService.DeleteDownload(uint(id), q.Platform, q.Arch); err != nil {
		global.GVA_LOG.Error("删除安装包失败!", zap.Error(err))
		response.FailWithMessage(err.Error(), c)
		return
	}
	response.Ok(c)
}
