package request

// SystemOAuth2Error OAuth2 错误返回
type SystemOAuth2Error struct {
	Code int    `json:"code" gorm:"comment:分类"`   // 错误代码
	Info string `json:"info" gorm:"comment:错误详情"` // 错误详情
}

// SystemOAuth2Request OAuth2 集成配置
type SystemOAuth2Request struct {
	Classify        uint   `json:"classify" gorm:"comment:分类"`                // 分类
	Status          bool   `json:"status" gorm:"comment:状态"`                  // 状态
	ServerURL       string `json:"server_url" gorm:"comment:服务器地址"`           // OAuth2 服务器地址
	AuthorizeURL    string `json:"authorize_url" gorm:"comment:申请认证的URL"`     // 申请认证的URL
	TokenURL        string `json:"token_url" gorm:"comment:获取Token的URL"`      // 获取Token的URL
	UserinfoURL     string `json:"userinfo_url" gorm:"comment:获取用户信息URL"`     // 获取用户信息的URL
	LogoutURL       string `json:"logout_url" gorm:"comment:退出登录回调URL"`       // 退出登录回调URL
	DiscoveryURL    string `json:"discovery_url" gorm:"comment:OIDC发现配置URL"`  // OIDC 发现配置URL
	AppID           string `json:"app_id" gorm:"comment:Client ID"`           // Client ID
	AppSecret       string `json:"app_secret" gorm:"comment:Client Secret"`   // Client Secret
	UserNameField   string `json:"user_name_field" gorm:"comment:用户名字段"`      // 用户名字段
	UserEmailField  string `json:"user_email_field" gorm:"comment:邮箱字段"`      // 邮箱字段
	UserIDField     string `json:"user_id_field" gorm:"comment:用户唯一标识字段"`     // 用户唯一标识字段
	Scope           string `json:"scope" gorm:"comment:授权范围scope"`            // 授权范围
	TokenAuthMethod string `json:"token_auth_method" gorm:"comment:令牌端点认证方式"` // client_secret_post|client_secret_basic
	RedirectUri     string `json:"redirect_uri" gorm:"comment:测试用回调地址"`       // 测试用回调地址
	Test            bool   `json:"test" gorm:"default:0;comment:是否测试链接联通性"`   // 是否测试链接联通性
	Code            string `json:"code" gorm:"default:0;comment:code代码"`      // code代码
}

// AuthorizationConfig 认证配置
type AuthorizationConfig struct {
	Type     string `json:"type"`     // none | bearer | basic
	Token    string `json:"token"`    // Bearer Token
	Username string `json:"username"` // Basic Auth用户名
	Password string `json:"password"` // Basic Auth密码
}

// BodyData Body数据配置
type BodyData struct {
	FormData   []map[string]string `json:"form_data"`   // form-data格式数据
	Urlencoded []map[string]string `json:"urlencoded"`  // x-www-form-urlencoded格式数据
	Raw        string              `json:"raw"`         // raw JSON字符串
}

// EmailApiConfig 第三方邮箱API配置
type EmailApiConfig struct {
	Enabled              bool                `json:"enabled"`                 // 是否启用
	URL                  string              `json:"url"`                     // API地址
	Method               string              `json:"method"`                  // HTTP方法
	RequestParamField    string              `json:"request_param_field"`     // 请求参数字段名
	BodyType             string              `json:"body_type"`               // Body类型: form-data | x-www-form-urlencoded | raw
	Headers              map[string]string   `json:"headers"`                 // 请求头
	Authorization        AuthorizationConfig  `json:"authorization"`           // 认证配置
	BodyData             BodyData            `json:"body_data"`               // Body数据
	ResponseEmailField   string              `json:"response_email_field"`    // 响应邮箱字段路径
}

// DingTalkConfigRequest 钉钉集成配置
type DingTalkConfigRequest struct {
	EmailApi EmailApiConfig `json:"email_api"` // 第三方邮箱API配置
}

