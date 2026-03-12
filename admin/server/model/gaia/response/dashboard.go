package response

// AppQuotaRankingRow 应用配额排名查询单行（仅用于 service 层 GORM 查询扫描）
type AppQuotaRankingRow struct {
	AppID        string  `gorm:"column:app_id"`
	TotalCost    float64 `gorm:"column:total_cost"`
	MessageCost  float64 `gorm:"column:message_cost"`
	WorkflowCost float64 `gorm:"column:workflow_cost"`
	RecordNum    float64 `gorm:"column:record_num"`
}

// AppQuotaRankingCache 应用配额排名缓存结构（List + Total）
type AppQuotaRankingCache struct {
	List  []GetAppQuotaRankingDataRes
	Total int64
}

// AiImageQuotaRankingRow AI 图片使用量排名查询单行（仅用于 service 层 GORM 查询扫描）
type AiImageQuotaRankingRow struct {
	Address   string  `gorm:"column:address"`
	Path      string  `gorm:"column:path"`
	TotalCost float64 `gorm:"column:total_cost"`
	RecordNum int     `gorm:"column:record_num"`
	Model     string  `gorm:"column:model"`
}

// GetAccountQuotaRankingDataRes 获取账户配额排名数据的响应结构
type GetAccountQuotaRankingDataRes struct {
	Ranking    int     `json:"ranking"`     // 排名
	Name       string  `json:"name"`        // 姓名
	UsedQuota  float64 `json:"used_quota"`  // 已使用配额
	TotalQuota float64 `json:"total_quota"` // 总配额
}

// GetAppQuotaRankingDataRes 获取应用配额排名数据的响应结构
type GetAppQuotaRankingDataRes struct {
	Ranking      int     `json:"ranking"`      // 排名
	Name         string  `json:"name"`         // 应用名称
	Mode         string  `json:"mode"`         // 应用类型
	AccountName  string  `json:"account_name"` // 账号名称
	UsedQuota    float64 `json:"used_quota"`   // 已使用配额
	AppID        string  `json:"app_id"`
	TotalCost    float64 `json:"total_cost"`
	MessageCost  float64 `json:"message_cost"`
	WorkflowCost float64 `json:"workflow_cost"`
	RecordNum    float64 `json:"record_num"`
	UseNum       int     `json:"use_num"`
}

// GetAppTokenQuotaRankingDataRes 获取应用密钥配额排名数据的响应结构
type GetAppTokenQuotaRankingDataRes struct {
	Ranking          int     `json:"ranking"`           // 排名
	Name             string  `json:"name"`              // 对应应用名称
	AppToken         string  `json:"app_token"`         // 密钥（需要加密显示）
	AccumulatedQuota float64 `json:"accumulated_quota"` // 累计使用
	DayUsedQuota     float64 `json:"day_used_quota"`    // 日使用
	MonthUsedQuota   float64 `json:"month_used_quota"`  // 月使用
	DayLimitQuota    float64 `json:"day_limit_quota"`   // 日限额
	MonthLimitQuota  float64 `json:"month_limit_quota"` // 月限额
}

type GetAppTokenDailyQuotaDataRes struct {
	StatDate  string  `json:"stat_date"`
	TotalUsed float64 `json:"total_used"`
}

// GetAiImageQuotaRankingRes 获取AI图片配额排名数据的响应结构
type GetAiImageQuotaRankingRes struct {
	Ranking   int     `json:"ranking"`    // 排名
	Address   string  `json:"address"`    // 域名
	Path      string  `json:"path"`       // 路径
	Model     string  `json:"model"`      // 模型
	TotalCost float64 `json:"total_cost"` // 总花费
	RecordNum int     `json:"record_num"` // 调用次数
}
