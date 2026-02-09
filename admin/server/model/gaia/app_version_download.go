package gaia

import "time"

// Platform: darwin | win32 | linux
// Arch: x64 | arm64

// AppVersionDownload 某版本的各平台/架构安装包
type AppVersionDownload struct {
	Id          uint      `json:"id" gorm:"primarykey;column:id;comment:id"`
	ReleaseId   uint      `json:"release_id" gorm:"column:release_id;not null;index;comment:关联的发布id"`
	Platform    string    `json:"platform" gorm:"column:platform;size:32;not null;comment:平台 darwin|win32|linux"`
	Arch        string    `json:"arch" gorm:"column:arch;size:16;not null;comment:架构 x64|arm64"`
	DownloadUrl string    `json:"download_url" gorm:"column:download_url;size:1024;not null;comment:下载地址（完整URL或相对路径）"`
	FileName    string    `json:"file_name" gorm:"column:file_name;size:256;comment:原始文件名"`
	CreatedAt   time.Time `json:"created_at" gorm:"column:created_at;comment:创建时间"`
}

func (AppVersionDownload) TableName() string {
	return "app_version_downloads"
}
