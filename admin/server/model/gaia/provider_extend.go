package gaia

import "time"

type ProviderCredential struct {
	ID              string    `json:"id" gorm:"index;comment:凭证ID"`
	TenantID        string    `json:"tenant_id" gorm:"comment:租户ID"`
	ProviderName    string    `json:"provider_name" gorm:"comment:提供者名称"`
	CredentialName  string    `json:"credential_name" gorm:"comment:凭证名称"`
	EncryptedConfig string    `json:"encrypted_config" gorm:"comment:加密配置"`
	CreatedAt       time.Time `json:"created_at" gorm:"not null;default:CURRENT_TIMESTAMP;comment:创建时间"`
	UpdatedAt       time.Time `json:"updated_at" gorm:"not null;default:CURRENT_TIMESTAMP;comment:更新时间"`
}
