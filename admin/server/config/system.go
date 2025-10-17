package config

type System struct {
	DbType        string `mapstructure:"db-type" json:"db-type" yaml:"db-type"`    // 数据库类型:mysql(默认)|sqlite|sqlserver|postgresql
	OssType       string `mapstructure:"oss-type" json:"oss-type" yaml:"oss-type"` // Oss类型
	RouterPrefix  string `mapstructure:"router-prefix" json:"router-prefix" yaml:"router-prefix"`
	Addr          int    `mapstructure:"addr" json:"addr" yaml:"addr"` // 端口值
	LimitCountIP  int    `mapstructure:"iplimit-count" json:"iplimit-count" yaml:"iplimit-count"`
	LimitTimeIP   int    `mapstructure:"iplimit-time" json:"iplimit-time" yaml:"iplimit-time"`
	UseMultipoint bool   `mapstructure:"use-multipoint" json:"use-multipoint" yaml:"use-multipoint"`    // 多点登录拦截
	UseRedis      bool   `mapstructure:"use-redis" json:"use-redis" yaml:"use-redis"`                   // 使用redis
	UseMongo      bool   `mapstructure:"use-mongo" json:"use-mongo" yaml:"use-mongo"`                   // 使用mongo
	UseStrictAuth bool   `mapstructure:"use-strict-auth" json:"use-strict-auth" yaml:"use-strict-auth"` // 使用树形角色分配模式
	// Extend: Start Custom Configuration
	WorkFlowNumber     int    `mapstructure:"work_flow_number" default:"200" json:"work_flow_number" yaml:"work_flow_number"`
	UserDefaultGroupID string `mapstructure:"user_default-group-id" default:"888" json:"user_default-group-id" yaml:"user_default-group-id"` // 用户默认群组id
	DockerRun          bool   `mapstructure:"docker-run" default:false json:"docker-run" yaml:"docker-run"`                                  // 是否在docker中运行，如果是的话，无需自动生成jwtkey，直接填sk-9f73s3ljTXVcMT3Blb3ljTqtsKiGHXVcMT3BlbkFJLK7U与dify保持一致
	// Extend: Stop Custom Configuration
}
