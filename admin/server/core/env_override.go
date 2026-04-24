package core

import (
	"fmt"
	"os"
	"strconv"

	"github.com/flipped-aurora/gin-vue-admin/server/global"
)

// overrideDBFromEnv 从环境变量覆盖数据库配置
// 优先级：环境变量 > 配置文件
func overrideDBFromEnv() {
	// 数据库类型
	if dbType := os.Getenv("DB_TYPE"); dbType != "" {
		switch dbType {
		case "mysql":
			global.GVA_CONFIG.System.DbType = "mysql"
			overrideMysqlFromEnv()
		case "postgresql", "postgres":
			global.GVA_CONFIG.System.DbType = "pgsql"
			overridePgsqlFromEnv()
		default:
			global.GVA_CONFIG.System.DbType = "pgsql"
			overridePgsqlFromEnv()
		}
		fmt.Printf("Database type overridden from DB_TYPE environment variable: %s\n", dbType)
	}
}

// overrideMysqlFromEnv 从环境变量覆盖 MySQL 配置
func overrideMysqlFromEnv() {
	cfg := &global.GVA_CONFIG.Mysql
	if host := os.Getenv("DB_HOST"); host != "" {
		cfg.Path = host
	}
	if port := os.Getenv("DB_PORT"); port != "" {
		cfg.Port = port
	} else {
		cfg.Port = "3306"
	}
	if username := os.Getenv("DB_USERNAME"); username != "" {
		cfg.Username = username
	}
	if password := os.Getenv("DB_PASSWORD"); password != "" {
		cfg.Password = password
	}
	if dbname := os.Getenv("DB_DATABASE"); dbname != "" {
		cfg.Dbname = dbname
	}
	if config := os.Getenv("DB_CONFIG"); config != "" {
		cfg.Config = config
	}
}

// overridePgsqlFromEnv 从环境变量覆盖 PostgreSQL 配置
func overridePgsqlFromEnv() {
	cfg := &global.GVA_CONFIG.Pgsql
	if host := os.Getenv("DB_HOST"); host != "" {
		cfg.Path = host
	}
	if port := os.Getenv("DB_PORT"); port != "" {
		cfg.Port = port
	} else {
		cfg.Port = "5432"
	}
	if username := os.Getenv("DB_USERNAME"); username != "" {
		cfg.Username = username
	}
	if password := os.Getenv("DB_PASSWORD"); password != "" {
		cfg.Password = password
	}
	if dbname := os.Getenv("DB_DATABASE"); dbname != "" {
		cfg.Dbname = dbname
	}
	if config := os.Getenv("DB_CONFIG"); config != "" {
		cfg.Config = config
	} else {
		cfg.Config = "sslmode=disable TimeZone=Asia/Shanghai"
	}
}

// overrideRedisFromEnv 从环境变量覆盖 Redis 配置
func overrideRedisFromEnv() {
	// 覆盖主 Redis 配置
	if host := os.Getenv("REDIS_HOST"); host != "" {
		port := os.Getenv("REDIS_PORT")
		if port == "" {
			port = "6379"
		}
		global.GVA_CONFIG.Redis.Addr = host + ":" + port
	}
	if password := os.Getenv("REDIS_PASSWORD"); password != "" {
		global.GVA_CONFIG.Redis.Password = password
	}
	if db := os.Getenv("REDIS_DB"); db != "" {
		if dbNum, err := strconv.Atoi(db); err == nil {
			global.GVA_CONFIG.Redis.DB = dbNum
		}
	}

	// 覆盖 Dify Redis 配置（与主 Redis 相同）
	if host := os.Getenv("REDIS_HOST"); host != "" {
		port := os.Getenv("REDIS_PORT")
		if port == "" {
			port = "6379"
		}
		global.GVA_CONFIG.DifyRedis.Addr = host + ":" + port
	}
	if password := os.Getenv("REDIS_PASSWORD"); password != "" {
		global.GVA_CONFIG.DifyRedis.Password = password
	}
	if db := os.Getenv("REDIS_DB"); db != "" {
		if dbNum, err := strconv.Atoi(db); err == nil {
			global.GVA_CONFIG.DifyRedis.DB = dbNum
		}
	}

	fmt.Printf("Redis configuration overridden from environment variables: %s\n", global.GVA_CONFIG.Redis.Addr)
}

// overrideAllFromEnv 从环境变量覆盖所有配置
func overrideAllFromEnv() {
        overrideDBFromEnv()
        overrideRedisFromEnv()
        overrideGaiaFromEnv()
}

// overrideGaiaFromEnv 从环境变量覆盖 Gaia 配置
func overrideGaiaFromEnv() {
        // BEDROCK_PROXY: 全局 Bedrock 反向代理地址，与 Dify Python 侧 BEDROCK_PROXY 含义一致
        if proxy := os.Getenv("BEDROCK_PROXY"); proxy != "" {
                global.GVA_CONFIG.Gaia.BedrockProxy = proxy
                fmt.Printf("Bedrock proxy overridden from BEDROCK_PROXY environment variable: %s\n", proxy)
        }
}
