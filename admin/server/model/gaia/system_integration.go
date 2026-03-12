package gaia

const EmailDomainEnv = "EMAIL_DOMAIN"
const SystemIntegrationDingTalk = uint(1) // 钉钉集成
const SystemIntegrationWeiXin = uint(2)   // 微信集成
const SystemIntegrationFeiShu = uint(3)   // 飞书集成
const SystemIntegrationOAuth2 = uint(4)   // OAuth2集成
const BearerLength = 7                    // OAuth2集成

// SystemIntegration 系统集成表
type SystemIntegration struct {
	Id        uint   `json:"id" form:"id" gorm:"primarykey;column:id;comment:id;"`
	Classify  uint   `json:"classify" gorm:"column:classify;default:1;comment:集成类型"`
	Status    bool   `json:"status" gorm:"column:status;default:f;comment:配置启用状态"`
	CorpID    string `json:"corp_id" gorm:"default:;comment:企业id"`
	AgentID   string `json:"agent_id" gorm:"default:;comment:代理Id"`
	AppID     string `json:"app_id" gorm:"default:;comment:应用ID"`
	AppKey    string `json:"app_key" gorm:"default:;comment:加密key"`
	AppSecret string `json:"app_secret" gorm:"default:;comment:加密密钥"`
	Test      bool   `json:"test" gorm:"default:0;comment:是否测试链接联通性"`
	Config    string `json:"config" gorm:"type:text;default:;comment:其他配置"`
}

// TableName system_integration_extend表 SystemIntegration自定义表名 system_integration_extend
func (SystemIntegration) TableName() string {
	return "system_integration_extend"
}
