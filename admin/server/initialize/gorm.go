package initialize

import (
	"fmt"
	"log"
	"os"

	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/model/example"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia"
	"github.com/flipped-aurora/gin-vue-admin/server/model/system"

	"go.uber.org/zap"
	"gorm.io/gorm"
)

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

func RegisterTables() {
	db := global.GVA_DB

	err := db.AutoMigrate(
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

		// Extend gaia model
		gaia.AccountDingTalkExtend{},
		gaia.AppRequestTestBatch{},
		gaia.AppRequestTest{},
		gaia.SystemIntegration{},   // Extend System Integration
		gaia.ForwardingExtend{},    // Extend Forwarding Extend
		gaia.BatchWorkflow{},       // Extend Batch Workflow
		gaia.BatchWorkflowTask{},   // Extend Batch Workflow Task
		system.SysUserGlobalCode{}, // Extend Global Code
		// Extend gaia model
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

// createPostgreSQLSequences 为PostgreSQL数据库创建必要的序列
func createPostgreSQLSequences(db *gorm.DB) {
	// 需要创建序列的表列表
	tables := []string{
		"sys_users",
		"sys_apis",
		"sys_base_menus",
		"sys_authorities",
		"sys_dictionaries",
		"sys_operation_records",
		"sys_auto_code_histories",
		"sys_dictionary_details",
		"sys_base_menu_parameters",
		"sys_base_menu_btns",
		"sys_authority_btns",
		"sys_auto_code_packages",
		"sys_export_templates",
		"conditions",
		"join_templates",
		"sys_params",
		"exa_files",
		"exa_customers",
		"exa_file_chunks",
		"exa_file_upload_and_downloads",
		"account_ding_talk_extends",
		"app_request_test_batches",
		"app_request_tests",
		"system_integrations",
		"forwarding_extends",
		"batch_workflows",
		"batch_workflow_tasks",
		"sys_user_global_codes",
	}

	for _, table := range tables {
		sequenceName := fmt.Sprintf("%s_id_seq", table)

		// 检查序列是否已存在
		var exists bool
		checkSQL := "SELECT EXISTS (SELECT 1 FROM pg_sequences WHERE sequencename = ?)"
		if err := db.Raw(checkSQL, sequenceName).Scan(&exists).Error; err != nil {
			log.Printf("检查序列 %s 是否存在时出错: %v", sequenceName, err)
			continue
		}

		if !exists {
			// 创建序列
			createSQL := fmt.Sprintf("CREATE SEQUENCE IF NOT EXISTS %s START 1 INCREMENT 1", sequenceName)
			if err := db.Exec(createSQL).Error; err != nil {
				log.Printf("创建序列 %s 时出错: %v", sequenceName, err)
				continue
			}

			// 将序列设置为表的默认值
			alterSQL := fmt.Sprintf("ALTER TABLE %s ALTER COLUMN id SET DEFAULT nextval('%s')", table, sequenceName)
			if err := db.Exec(alterSQL).Error; err != nil {
				log.Printf("设置表 %s 的ID默认值时出错: %v", table, err)
				continue
			}

			// 更新序列的当前值（如果表中已有数据）
			updateSQL := fmt.Sprintf("SELECT setval('%s', COALESCE((SELECT MAX(id) FROM %s), 1), true)", sequenceName, table)
			if err := db.Exec(updateSQL).Error; err != nil {
				log.Printf("更新序列 %s 的当前值时出错: %v", sequenceName, err)
			}

			log.Printf("成功为表 %s 创建序列 %s", table, sequenceName)
		}
	}
}
