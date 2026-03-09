package request

import "time"

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
	AuthorizeURL    string `json:"authorize_url" gorm:"comment:申请认证的 URL"`     // 申请认证的 URL
	TokenURL        string `json:"token_url" gorm:"comment:获取 Token 的 URL"`      // 获取 Token 的 URL
	UserinfoURL     string `json:"userinfo_url" gorm:"comment:获取用户信息 URL"`     // 获取用户信息的 URL
	LogoutURL       string `json:"logout_url" gorm:"comment:退出登录回调 URL"`       // 退出登录回调 URL
	DiscoveryURL    string `json:"discovery_url" gorm:"comment:OIDC 发现配置 URL"`  // OIDC 发现配置 URL
	AppID           string `json:"app_id" gorm:"comment:Client ID"`           // Client ID
	AppSecret       string `json:"app_secret" gorm:"comment:Client Secret"`   // Client Secret
	UserNameField   string `json:"user_name_field" gorm:"comment:用户名字段"`      // 用户名字段
	UserEmailField  string `json:"user_email_field" gorm:"comment:邮箱字段"`      // 邮箱字段
	UserIDField     string `json:"user_id_field" gorm:"comment:用户唯一标识字段"`     // 用户唯一标识字段
	Scope           string `json:"scope" gorm:"comment:授权范围 scope"`            // 授权范围
	TokenAuthMethod string `json:"token_auth_method" gorm:"comment:令牌端点认证方式"` // client_secret_post|client_secret_basic
	RedirectUri     string `json:"redirect_uri" gorm:"comment:测试用回调地址"`       // 测试用回调地址
	Test            bool   `json:"test" gorm:"default:0;comment:是否测试链接联通性"`   // 是否测试链接联通性
	Code            string `json:"code" gorm:"default:0;comment:code 代码"`      // code 代码
}

// AuthorizationConfig 认证配置
type AuthorizationConfig struct {
	Type     string `json:"type"`     // none | bearer | basic
	Token    string `json:"token"`    // Bearer Token
	Username string `json:"username"` // Basic Auth 用户名
	Password string `json:"password"` // Basic Auth 密码
}

// BodyData Body 数据配置
type BodyData struct {
	FormData   []map[string]string `json:"form_data"`   // form-data 格式数据
	Urlencoded []map[string]string `json:"urlencoded"`  // x-www-form-urlencoded 格式数据
	Raw        string              `json:"raw"`         // raw JSON 字符串
}

// EmailApiConfig 第三方邮箱 API 配置
type EmailApiConfig struct {
	Enabled            bool                `json:"enabled"`                 // 是否启用
	URL                string              `json:"url"`                     // API 地址
	Method             string              `json:"method"`                  // HTTP 方法
	RequestParamField  string              `json:"request_param_field"`     // 请求参数字段名
	BodyType           string              `json:"body_type"`               // Body 类型：form-data | x-www-form-urlencoded | raw
	Headers            map[string]string   `json:"headers"`                 // 请求头
	Authorization      AuthorizationConfig `json:"authorization"`           // 认证配置
	BodyData           BodyData            `json:"body_data"`               // Body 数据
	ResponseEmailField string              `json:"response_email_field"`    // 响应邮箱字段路径
}

// DingIdApiConfig 第三方钉钉 ID 匹配用户 API 配置
type DingIdApiConfig struct {
	Enabled              bool                `json:"enabled"`                 // 是否启用
	URL                  string              `json:"url"`                     // API 地址
	Method               string              `json:"method"`                  // HTTP 方法
	RequestParamField    string              `json:"request_param_field"`     // 请求参数字段名（如 ding_id）
	BodyType             string              `json:"body_type"`               // Body 类型：form-data | x-www-form-urlencoded | raw
	Headers              map[string]string   `json:"headers"`                 // 请求头
	Authorization        AuthorizationConfig `json:"authorization"`           // 认证配置
	BodyData             BodyData            `json:"body_data"`               // Body 数据
	ResponseUserNamePath string              `json:"response_user_name_path"` // 响应用户名 JSON 路径（如 data.username）
}

// ForwardToken 转发 Token 配置
type ForwardToken struct {
	ID        string    `json:"id"`          // 前端生成的唯一 ID（用于删除）
	TokenHash string    `json:"token_hash"`  // SHA256(token)
	CreatedAt time.Time `json:"created_at"`  // 创建时间
}

// ForwardConfig 转发集成配置
type ForwardConfig struct {
	Enabled   bool            `json:"enabled"`     // 是否启用转发
	Tokens    []ForwardToken  `json:"tokens"`      // Token 列表，最多 20 个
	DingIdApi DingIdApiConfig `json:"ding_id_api"` // 第三方钉钉 ID 匹配用户 API
}

// DingTalkConfigRequest 钉钉集成配置
type DingTalkConfigRequest struct {
	EmailApi      EmailApiConfig  `json:"email_api"`       // 第三方邮箱 API 配置
	ForwardConfig ForwardConfig   `json:"forward_config"`  // 转发集成配置
}
