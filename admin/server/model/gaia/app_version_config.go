package gaia

import "time"

// AppVersionConfig 应用版本全局配置（仅链接 Token，与具体版本解耦）
type AppVersionConfig struct {
	Id        uint      `json:"id" gorm:"primarykey;column:id;comment:id"`
	LinkToken *string   `json:"link_token,omitempty" gorm:"column:link_token;size:255;comment:链接token，配置后GET /latest需传此token"`
	UpdatedAt time.Time `json:"updated_at" gorm:"column:updated_at;comment:更新时间"`
}

func (AppVersionConfig) TableName() string {
	return "app_version_config"
}
