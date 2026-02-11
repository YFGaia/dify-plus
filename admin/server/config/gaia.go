package config

type Gaia struct {
	Url                 string `mapstructure:"url" json:"url" yaml:"url"`
	LoginMaxErrorLimit  int    `mapstructure:"login_max_error_limit" json:"login_max_error_limit" yaml:"login_max_error_limit"`
	SuperAdminAccountId string `mapstructure:"SUPER_ADMIN_ACCOUNT_ID" json:"SUPER_ADMIN_ACCOUNT_ID" yaml:"SUPER_ADMIN_ACCOUNT_ID"` // 超级管理员账号
	SuperAdminTenantId  string `mapstructure:"SUPER_ADMIN_TENANT_ID" json:"SUPER_ADMIN_TENANT_ID" yaml:"SUPER_ADMIN_TENANT_ID"`    // 系统默认工作区
	StoragePath         string `mapstructure:"storage-path" json:"storage-path" yaml:"storage-path"`                              // Dify storage 目录路径，用于读取私钥
}
