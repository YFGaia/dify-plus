package initialize

import (
	adapter "github.com/casbin/gorm-adapter/v3"
	"os"

	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/model/example"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia"
	"github.com/flipped-aurora/gin-vue-admin/server/model/system"

	"go.uber.org/zap"
	"gorm.io/gorm"
)

var tables = []interface{}{
	system.SysApi{},
	system.SysIgnoreApi{},
	system.SysUser{},
	system.SysBaseMenu{},
	system.JwtBlacklist{},
	system.SysAuthority{},
	system.SysDictionary{},
	system.SysOperationRecord{},
	system.SysAutoCodeHistory{},
	system.SysDictionaryDetail{},
	system.SysBaseMenuParameter{},
	system.SysBaseMenuBtn{},
	system.SysAuthorityBtn{},
	system.SysAutoCodePackage{},
	system.SysExportTemplate{},
	system.Condition{},
	system.JoinTemplate{},
	system.SysParams{},

	example.ExaFile{},
	example.ExaCustomer{},
	example.ExaFileChunk{},
	example.ExaFileUploadAndDownload{},

	adapter.CasbinRule{},

	// Extend gaia model
	gaia.AccountDingTalkExtend{},
	gaia.AppRequestTestBatch{},
	gaia.AppRequestTest{},
	gaia.SystemIntegration{},   // Extend System Integration
	gaia.ForwardingExtend{},    // Extend Forwarding Extend
	gaia.BatchWorkflow{},       // Extend Batch Workflow
	gaia.BatchWorkflowTask{},   // Extend Batch Workflow Task
	gaia.AppVersionConfig{},    // 应用版本全局配置（Token）
	gaia.AppVersionRelease{},   // 应用版本发布
	gaia.AppVersionDownload{},  // 应用版本各平台安装包
	gaia.ModelProviderConfig{}, // 模型提供商配置
	gaia.ModelProxyLog{},       // 模型中转请求日志
	system.SysUserGlobalCode{}, // Extend Global Code
	// Extend gaia model
}

func Gorm() *gorm.DB {
	switch global.GVA_CONFIG.System.DbType {
	case "mysql":
		global.GVA_ACTIVE_DBNAME = &global.GVA_CONFIG.Mysql.Dbname
		return GormMysql()
	case "pgsql":
		global.GVA_ACTIVE_DBNAME = &global.GVA_CONFIG.Pgsql.Dbname
		return GormPgSql()
	case "oracle":
		global.GVA_ACTIVE_DBNAME = &global.GVA_CONFIG.Oracle.Dbname
		return GormOracle()
	case "mssql":
		global.GVA_ACTIVE_DBNAME = &global.GVA_CONFIG.Mssql.Dbname
		return GormMssql()
	case "sqlite":
		global.GVA_ACTIVE_DBNAME = &global.GVA_CONFIG.Sqlite.Dbname
		return GormSqlite()
	default:
		global.GVA_ACTIVE_DBNAME = &global.GVA_CONFIG.Mysql.Dbname
		return GormMysql()
	}
}

func RegisterTables(db *gorm.DB) {
	var err error
	var count int64
	var menu system.SysBaseMenuBtn
	var authority system.SysAuthority
	if err = global.GVA_DB.Model(&menu).Count(&count).Error; count == 0 {
		if err = global.GVA_DB.Model(&authority).Count(&count).Error; count == 1 {
			return
		}
	}
	// auto
	err = db.AutoMigrate(
		system.SysApi{},
		system.SysIgnoreApi{},
		system.SysUser{},
		system.SysBaseMenu{},
		system.JwtBlacklist{},
		system.SysAuthority{},
		system.SysDictionary{},
		system.SysOperationRecord{},
		system.SysAutoCodeHistory{},
		system.SysDictionaryDetail{},
		system.SysBaseMenuParameter{},
		system.SysBaseMenuBtn{},
		system.SysAuthorityBtn{},
		system.SysAutoCodePackage{},
		system.SysExportTemplate{},
		system.Condition{},
		system.JoinTemplate{},
		system.SysParams{},

		example.ExaFile{},
		example.ExaCustomer{},
		example.ExaFileChunk{},
		example.ExaFileUploadAndDownload{},

		adapter.CasbinRule{},

		// Extend gaia model
		gaia.AccountDingTalkExtend{},
		gaia.AppRequestTestBatch{},
		gaia.AppRequestTest{},
		gaia.SystemIntegration{},   // Extend System Integration
		gaia.ForwardingExtend{},    // Extend Forwarding Extend
		gaia.BatchWorkflow{},       // Extend Batch Workflow
		gaia.BatchWorkflowTask{},   // Extend Batch Workflow Task
		gaia.AppVersionConfig{},    // 应用版本全局配置（Token）
		gaia.AppVersionRelease{},   // 应用版本发布
		gaia.AppVersionDownload{},  // 应用版本各平台安装包
		gaia.ModelProviderConfig{}, // 模型提供商配置
		gaia.ModelProxyLog{},       // 模型中转请求日志
		system.SysUserGlobalCode{}, // Extend Global Code
	)

	if err != nil {
		global.GVA_LOG.Error("register table failed", zap.Error(err))
		os.Exit(0)
	}

	//// 如果是PostgreSQL数据库，创建必要的序列
	//if global.GVA_CONFIG.System.DbType == "pgsql" {
	//	createPostgreSQLSequences(db)
	//}

	err = bizModel()

	if err != nil {
		global.GVA_LOG.Error("register biz_table failed", zap.Error(err))
		os.Exit(0)
	}
	global.GVA_LOG.Info("register table success")
}
