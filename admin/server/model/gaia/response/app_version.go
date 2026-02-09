package response

// LatestVersionResponse 客户端 GET /latest 返回（Electron 更新检测）
type LatestVersionResponse struct {
	Version      string `json:"version"`
	ReleaseNotes string `json:"releaseNotes"`
	DownloadUrl  string `json:"downloadUrl"`
}

// AppVersionTokenConfig 管理端获取的全局 Token 配置（脱敏）
type AppVersionTokenConfig struct {
	LinkToken *string `json:"link_token,omitempty"`
}

// ReleaseListItem 版本列表项
type ReleaseListItem struct {
	Id           uint           `json:"id"`
	Version      string         `json:"version"`
	ReleaseNotes string         `json:"release_notes"`
	CreatedAt    string         `json:"created_at"`
	Downloads    []DownloadItem `json:"downloads"`
}

// ReleaseDetail 单个版本详情（含包列表）
type ReleaseDetail struct {
	Id           uint           `json:"id"`
	Version      string         `json:"version"`
	ReleaseNotes string         `json:"release_notes"`
	CreatedAt    string         `json:"created_at"`
	UpdatedAt    string         `json:"updated_at"`
	Downloads    []DownloadItem `json:"downloads"`
}

// DownloadItem 单个平台/架构的安装包信息
type DownloadItem struct {
	Id          uint   `json:"id"`
	Platform    string `json:"platform"`
	Arch        string `json:"arch"`
	DownloadUrl string `json:"download_url"`
	FileName    string `json:"file_name"`
	CreatedAt   string `json:"created_at,omitempty"`
}
