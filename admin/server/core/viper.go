package core

import (
	"flag"
	"fmt"
	"github.com/flipped-aurora/gin-vue-admin/server/core/internal"
	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/fsnotify/fsnotify"
	"github.com/gin-gonic/gin"
	"github.com/spf13/viper"
	"os"
	"path/filepath"
)

// Extend: Override JWT signing key from environment variable
// This ensures admin-server uses the same JWT signing key as the API server
func overrideJWTSigningKeyFromEnv() {
	// Check JWT_SIGNING_KEY first, then fall back to SECRET_KEY
	if jwtKey := os.Getenv("JWT_SIGNING_KEY"); jwtKey != "" {
		global.GVA_CONFIG.JWT.SigningKey = jwtKey
		fmt.Printf("JWT signing key overridden from JWT_SIGNING_KEY environment variable\n")
	} else if secretKey := os.Getenv("SECRET_KEY"); secretKey != "" {
		global.GVA_CONFIG.JWT.SigningKey = secretKey
		fmt.Printf("JWT signing key overridden from SECRET_KEY environment variable\n")
	}
}

// Viper //
// 优先级：命令行 > 环境变量 > 默认值
// Author [SliverHorn](https://github.com/SliverHorn)
func Viper(path ...string) *viper.Viper {
	var config string

	if len(path) == 0 {
		flag.StringVar(&config, "c", "", "choose config file.")
		flag.Parse()
		if config == "" {
			// 判断 internal.ConfigEnv 常量存储的环境变量是否为空
			if configEnv := os.Getenv(internal.ConfigEnv); configEnv == "" {
				switch gin.Mode() {
				case gin.DebugMode:
					config = internal.ConfigDefaultFile
				case gin.ReleaseMode:
					config = internal.ConfigReleaseFile
				case gin.TestMode:
					config = internal.ConfigTestFile
				}
				fmt.Printf("您正在使用 gin 模式的%s环境名称，config 的路径为%s\n", gin.Mode(), config)
			} else { // internal.ConfigEnv 常量存储的环境变量不为空 将值赋值于 config
				config = configEnv
				fmt.Printf("您正在使用%s环境变量，config 的路径为%s\n", internal.ConfigEnv, config)
			}
		} else { // 命令行参数不为空 将值赋值于 config
			fmt.Printf("您正在使用命令行的 -c 参数传递的值，config 的路径为%s\n", config)
		}
	} else { // 函数传递的可变参数的第一个值赋值于 config
		config = path[0]
		fmt.Printf("您正在使用 func Viper() 传递的值，config 的路径为%s\n", config)
	}

	v := viper.New()
	v.SetConfigFile(config)
	v.SetConfigType("yaml")
	err := v.ReadInConfig()
	if err != nil {
		panic(fmt.Errorf("Fatal error config file: %s \n", err))
	}
	v.WatchConfig()

	v.OnConfigChange(func(e fsnotify.Event) {
		fmt.Println("config file changed:", e.Name)
		if err = v.Unmarshal(&global.GVA_CONFIG); err != nil {
			fmt.Println(err)
		}
		// Extend: Override JWT signing key from environment variable
		overrideJWTSigningKeyFromEnv()
	})
	if err = v.Unmarshal(&global.GVA_CONFIG); err != nil {
		panic(err)
	}

	// Extend: Override JWT signing key from environment variable after initial load
	overrideJWTSigningKeyFromEnv()

	// Extend: Override database and redis configuration from environment variables
	// This allows admin-server to use the same configuration as docker-compose
	overrideAllFromEnv()

	// root 适配性 根据 root 位置去找到对应迁移位置，保证 root 路径有效
	global.GVA_CONFIG.AutoCode.Root, _ = filepath.Abs("..")

	return v
}
