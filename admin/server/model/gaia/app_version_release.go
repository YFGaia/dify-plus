package gaia

import "time"

// AppVersionRelease 应用版本发布（多版本列表中的一条）
type AppVersionRelease struct {
	Id           uint      `json:"id" gorm:"primarykey;column:id;comment:id"`
	Version      string    `json:"version" gorm:"column:version;size:64;not null;comment:版本号"`
	ReleaseNotes string    `json:"release_notes" gorm:"column:release_notes;type:text;comment:更新说明"`
	CreatedAt    time.Time `json:"created_at" gorm:"column:created_at;comment:创建时间"`
	UpdatedAt    time.Time `json:"updated_at" gorm:"column:updated_at;comment:更新时间"`
}

func (AppVersionRelease) TableName() string {
	return "app_version_releases"
}
