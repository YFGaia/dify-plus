package gaia

import (
	"errors"
	"fmt"
	"mime/multipart"
	"time"

	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia/request"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia/response"
	"github.com/flipped-aurora/gin-vue-admin/server/model/system"
	"github.com/flipped-aurora/gin-vue-admin/server/utils"
	"github.com/flipped-aurora/gin-vue-admin/server/utils/upload"
	"gorm.io/gorm"
)

const tokenMask = "********"

type AppVersionService struct{}

// getOrCreateConfig 获取或创建全局配置（单例）
func (s *AppVersionService) getOrCreateConfig() (*gaia.AppVersionConfig, error) {
	var cfg gaia.AppVersionConfig
	err := global.GVA_DB.First(&cfg).Error
	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			if err = global.GVA_DB.Create(&cfg).Error; err != nil {
				return nil, err
			}
			return &cfg, nil
		}
		return nil, err
	}
	return &cfg, nil
}

// downloadsToItems 将下载记录转为 API 返回的 DownloadItem 列表
func (s *AppVersionService) downloadsToItems(releaseID uint) ([]response.DownloadItem, error) {
	var downloads []gaia.AppVersionDownload
	if err := global.GVA_DB.Where("release_id = ?", releaseID).Find(&downloads).Error; err != nil {
		return nil, err
	}
	items := make([]response.DownloadItem, 0, len(downloads))
	for _, d := range downloads {
		items = append(items, response.DownloadItem{
			Id:          d.Id,
			Platform:    d.Platform,
			Arch:        d.Arch,
			DownloadUrl: d.DownloadUrl,
			FileName:    d.FileName,
			CreatedAt:   d.CreatedAt.Format(time.RFC3339),
		})
	}
	return items, nil
}

// GetLatest 客户端「最新版本」；token 从全局 config 校验。仅必填 platform；arch 可选：未传则取该平台第一个包，传了但无匹配则按 platform 取第一个。
func (s *AppVersionService) GetLatest(platform, arch, token string) (*response.LatestVersionResponse, int) {
	cfg, err := s.getOrCreateConfig()
	if err != nil {
		return nil, 500
	}
	if cfg.LinkToken != nil && *cfg.LinkToken != "" && (token == "" || token != *cfg.LinkToken) {
		return nil, 401
	}

	var release gaia.AppVersionRelease
	if err = global.GVA_DB.Order("id DESC").First(&release).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, 404
		}
		return nil, 500
	}

	var download gaia.AppVersionDownload
	if arch != "" {
		err = global.GVA_DB.Where("release_id = ? AND platform = ? AND arch = ?", release.Id, platform, arch).First(&download).Error
	}
	if arch == "" || errors.Is(err, gorm.ErrRecordNotFound) {
		err = global.GVA_DB.Where("release_id = ? AND platform = ?", release.Id, platform).First(&download).Error
	}
	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, 404
		}
		return nil, 500
	}

	return &response.LatestVersionResponse{
		Version:      release.Version,
		ReleaseNotes: release.ReleaseNotes,
		DownloadUrl:  download.DownloadUrl,
	}, 200
}

// GetTokenConfig 管理端获取全局 Token（脱敏）
func (s *AppVersionService) GetTokenConfig() (*response.AppVersionTokenConfig, error) {
	cfg, err := s.getOrCreateConfig()
	if err != nil {
		return nil, err
	}
	var linkToken *string
	if cfg.LinkToken != nil && *cfg.LinkToken != "" {
		mask := tokenMask
		linkToken = &mask
	}
	return &response.AppVersionTokenConfig{LinkToken: linkToken}, nil
}

// SetTokenConfig 管理端设置全局 Token
func (s *AppVersionService) SetTokenConfig(req request.AppVersionTokenConfig) error {
	cfg, err := s.getOrCreateConfig()
	if err != nil {
		return err
	}
	if req.LinkToken == nil || *req.LinkToken == tokenMask {
		return nil
	}
	if *req.LinkToken == "" {
		cfg.LinkToken = nil
	} else {
		cfg.LinkToken = req.LinkToken
	}
	return global.GVA_DB.Save(cfg).Error
}

// RevealToken 验证当前用户登录密码后返回明文 Token
func (s *AppVersionService) RevealToken(userID uint, password string) (string, error) {
	var user system.SysUser
	if err := global.GVA_DB.Select("password").First(&user, userID).Error; err != nil {
		return "", err
	}
	if !utils.BcryptCheck(password, user.Password) {
		return "", errors.New("密码错误")
	}
	cfg, err := s.getOrCreateConfig()
	if err != nil {
		return "", err
	}
	if cfg.LinkToken == nil || *cfg.LinkToken == "" {
		return "", nil
	}
	return *cfg.LinkToken, nil
}

// ListReleases 版本列表（按 id 倒序）
func (s *AppVersionService) ListReleases() ([]response.ReleaseListItem, error) {
	var releases []gaia.AppVersionRelease
	if err := global.GVA_DB.Order("id DESC").Find(&releases).Error; err != nil {
		return nil, err
	}
	result := make([]response.ReleaseListItem, 0, len(releases))
	for _, r := range releases {
		items, err := s.downloadsToItems(r.Id)
		if err != nil {
			return nil, err
		}
		result = append(result, response.ReleaseListItem{
			Id:           r.Id,
			Version:      r.Version,
			ReleaseNotes: r.ReleaseNotes,
			CreatedAt:    r.CreatedAt.Format(time.RFC3339),
			Downloads:    items,
		})
	}
	return result, nil
}

// CreateRelease 新增版本
func (s *AppVersionService) CreateRelease(req request.AppVersionReleaseCreate) (*gaia.AppVersionRelease, error) {
	release := gaia.AppVersionRelease{Version: req.Version, ReleaseNotes: req.ReleaseNotes}
	if err := global.GVA_DB.Create(&release).Error; err != nil {
		return nil, err
	}
	return &release, nil
}

// GetReleaseByID 获取单个版本详情
func (s *AppVersionService) GetReleaseByID(id uint) (*response.ReleaseDetail, error) {
	var release gaia.AppVersionRelease
	if err := global.GVA_DB.First(&release, id).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, errors.New("版本不存在")
		}
		return nil, err
	}
	items, err := s.downloadsToItems(release.Id)
	if err != nil {
		return nil, err
	}
	return &response.ReleaseDetail{
		Id:           release.Id,
		Version:      release.Version,
		ReleaseNotes: release.ReleaseNotes,
		CreatedAt:    release.CreatedAt.Format(time.RFC3339),
		UpdatedAt:    release.UpdatedAt.Format(time.RFC3339),
		Downloads:    items,
	}, nil
}

// UpdateRelease 更新版本信息
func (s *AppVersionService) UpdateRelease(id uint, req request.AppVersionReleaseUpdate) error {
	var release gaia.AppVersionRelease
	if err := global.GVA_DB.First(&release, id).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return errors.New("版本不存在")
		}
		return err
	}
	release.Version = req.Version
	release.ReleaseNotes = req.ReleaseNotes
	return global.GVA_DB.Save(&release).Error
}

// UploadPackageToRelease 上传安装包到指定版本，根据文件名自动推断 platform/arch
func (s *AppVersionService) UploadPackageToRelease(releaseID uint, file *multipart.FileHeader, buildDownloadUrl func(path string) string) error {
	platform, arch := utils.InferPlatformArch(file.Filename)
	if platform == "" || arch == "" {
		return fmt.Errorf("无法从文件名推断平台/架构，请使用 .dmg/.exe/.deb/.AppImage 等常见安装包")
	}
	var release gaia.AppVersionRelease
	if err := global.GVA_DB.First(&release, releaseID).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return errors.New("版本不存在")
		}
		return err
	}
	oss := upload.NewOss()
	filePath, _, err := oss.UploadFile(file)
	if err != nil {
		return err
	}
	fullURL := buildDownloadUrl(filePath)
	var download gaia.AppVersionDownload
	err = global.GVA_DB.Where("release_id = ? AND platform = ? AND arch = ?", releaseID, platform, arch).First(&download).Error
	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			download = gaia.AppVersionDownload{
				ReleaseId:   releaseID,
				Platform:    platform,
				Arch:        arch,
				DownloadUrl: fullURL,
				FileName:    file.Filename,
			}
			return global.GVA_DB.Create(&download).Error
		}
		return err
	}
	download.DownloadUrl = fullURL
	download.FileName = file.Filename
	return global.GVA_DB.Save(&download).Error
}

// DeleteDownload 删除指定版本下某 platform/arch 的安装包记录
func (s *AppVersionService) DeleteDownload(releaseID uint, platform, arch string) error {
	return global.GVA_DB.Where("release_id = ? AND platform = ? AND arch = ?", releaseID, platform, arch).
		Delete(&gaia.AppVersionDownload{}).Error
}
