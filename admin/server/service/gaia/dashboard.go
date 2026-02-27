package gaia

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia"
	gaiaReq "github.com/flipped-aurora/gin-vue-admin/server/model/gaia/request"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia/response"
	"github.com/gofrs/uuid/v5"
	"github.com/redis/go-redis/v9"
	"go.uber.org/zap"
)

type DashboardService struct{}

// GetAccountQuotaRankingData 分页获取【账号】额度排名列表
func (dashboardService *DashboardService) GetAccountQuotaRankingData(info gaiaReq.GetAccountQuotaRankingDataReq) (list []response.GetAccountQuotaRankingDataRes, total int64, err error) {
	limit := info.PageSize
	offset := info.PageSize * (info.Page - 1)

	db := global.GVA_DB.Model(&gaia.AccountMoneyExtend{}).Order("used_quota desc")
	var accountMoneys []gaia.AccountMoneyExtend
	err = db.Count(&total).Error
	if err != nil {
		return
	}

	if limit != 0 {
		db = db.Limit(limit).Offset(offset)
	}

	err = db.Find(&accountMoneys).Error
	if err != nil {
		err = fmt.Errorf("查询账号额度信息失败：%s", err.Error())
		return
	}

	// 账号ID集合，方便后面一次性查出
	var accountIds []uuid.UUID
	for _, money := range accountMoneys {
		accountIds = append(accountIds, money.AccountId)
	}

	// 查询用户信息
	var accountInfos = make(map[uuid.UUID]gaia.Account)
	var accounts []gaia.Account
	err = global.GVA_DB.Model(&gaia.Account{}).Where("id in ?", accountIds).Find(&accounts).Error
	if err != nil {
		err = fmt.Errorf("查询账户信息失败：%s", err.Error())
		return
	}
	for _, account := range accounts {
		accountInfos[account.ID] = account
	}

	// 拼接结果
	for i, money := range accountMoneys {
		var accountInfo gaia.Account
		var isExist bool
		if accountInfo, isExist = accountInfos[money.AccountId]; !isExist {
			global.GVA_LOG.Error("账户信息不存在!", zap.String("account_id", money.AccountId.String()))
			continue
		}
		row := response.GetAccountQuotaRankingDataRes{
			Ranking:    i + 1 + offset,
			Name:       accountInfo.Name,
			UsedQuota:  money.UsedQuota,
			TotalQuota: money.TotalQuota,
		}
		list = append(list, row)
	}

	return list, total, err
}

// GetAppQuotaRankingData 分页获取【应用】配额排名数据
func (dashboardService *DashboardService) GetAppQuotaRankingData(info gaiaReq.GetAppQuotaRankingDataReq) (list []response.GetAppQuotaRankingDataRes, total int64, err error) {

	cacheKey := fmt.Sprintf("app_token_quota_ranking:%d:%d", info.Page, info.PageSize)
	var cachedResult struct {
		List  []response.GetAppQuotaRankingDataRes
		Total int64
	}

	if found, err := dashboardService.getCachedResult(cacheKey, &cachedResult); err == nil && found {
		return cachedResult.List, cachedResult.Total, nil
	}

	limit := info.PageSize
	offset := info.PageSize * (info.Page - 1)

	/**
	查出应用花费最多的应用排序
	*/
	// 创建子查询
	messageCosts := global.GVA_DB.Table("public.messages").
		Select("" +
			"app_id, " +
			"COUNT(id) as message_num, " +
			"SUM(CASE WHEN currency = 'RMB' THEN total_price / 7.26 ELSE total_price END) as message_cost").
		Group("app_id")

	workflowCosts := global.GVA_DB.Table("public.workflow_node_executions").
		Select("" +
			"app_id, " +
			"COUNT(id) as workflow_num, " +
			"SUM(CASE " +
			"  WHEN execution_metadata::json->>'currency' = 'RMB' " +
			"  THEN CAST((execution_metadata::json->>'total_price') AS NUMERIC) / 7.26 " +
			"  ELSE CAST((execution_metadata::json->>'total_price') AS NUMERIC) " +
			"END) AS workflow_cost").
		Where("execution_metadata IS NOT NULL AND execution_metadata != '' AND (execution_metadata::json->>'total_price') IS NOT NULL").
		Group("app_id")

	// 主查询
	query := global.GVA_DB.Table("(?) AS m", messageCosts).
		Select(""+
			"COALESCE(m.app_id, w.app_id) AS app_id, "+
			"COALESCE(m.message_cost, 0) AS message_cost, "+
			"COALESCE(w.workflow_cost, 0) AS workflow_cost, "+
			"COALESCE(m.message_num, 0) + COALESCE(w.workflow_num, 0) AS record_num, "+
			"COALESCE(m.message_cost, 0) + COALESCE(w.workflow_cost, 0) AS total_cost").
		Joins("FULL OUTER JOIN (?) AS w ON m.app_id = w.app_id", workflowCosts).
		Order("total_cost DESC")

	// 获取总数
	err = query.Count(&total).Error
	if err != nil {
		return nil, 0, fmt.Errorf("获取总数失败：%w", err)
	}

	// 应用分页
	if limit != 0 {
		query = query.Limit(limit).Offset(offset)
	}

	// 执行查询
	var results []struct {
		AppID        string  `gorm:"column:app_id"`
		TotalCost    float64 `gorm:"column:total_cost"`
		MessageCost  float64 `gorm:"column:message_cost"`
		WorkflowCost float64 `gorm:"column:workflow_cost"`
		RecordNum    float64 `gorm:"column:record_num"`
	}

	err = query.Find(&results).Error
	if err != nil {
		return nil, 0, fmt.Errorf("查询数据失败：%w", err)
	}

	// 构建返回结果并获取app_id集合
	appIDs := make([]string, 0, len(results))
	for _, r := range results {
		appIDs = append(appIDs, r.AppID)
	}

	/**
	查询APP信息
	*/
	var appInfos = make(map[string]gaia.Apps)
	var apps []gaia.Apps
	err = global.GVA_DB.Model(&gaia.Apps{}).Where("id in ?", appIDs).Find(&apps).Error
	if err != nil {
		err = fmt.Errorf("查询应用信息失败：%w", err)
		return
	}
	tenantIDs := make([]string, 0, len(results))
	for _, app := range apps {
		appInfos[app.ID.String()] = app
		tenantIDs = append(tenantIDs, app.TenantID.String())
	}

	/**
	查询所在的工作区信息
	*/
	var tenants []gaia.Tenants
	err = global.GVA_DB.Model(&gaia.Tenants{}).Where("id in ?", tenantIDs).Find(&tenants).Error
	if err != nil {
		err = fmt.Errorf("查询租户信息失败：%w", err)
		return
	}
	tenantMap := make(map[string]gaia.Tenants)
	for _, tenant := range tenants {
		tenantMap[tenant.Id] = tenant
	}

	/**
	查询工作区对应用户信息
	*/
	// 1. 查询 TenantAccountJoins
	var tenantAccountJoins []gaia.TenantAccountJoins
	err = global.GVA_DB.Model(&gaia.TenantAccountJoins{}).
		Where("tenant_id IN ? AND role = ?", tenantIDs, "owner").
		Find(&tenantAccountJoins).Error
	if err != nil {
		err = fmt.Errorf("查询租户账号关联信息失败：%w", err)
		return
	}

	// 2. 提取账号 ID
	accountIDs := make([]string, 0, len(tenantAccountJoins))
	tenantToAccountMap := make(map[string]string)
	for _, join := range tenantAccountJoins {
		accountIDs = append(accountIDs, join.AccountID.String())
		tenantToAccountMap[join.TenantID.String()] = join.AccountID.String()
	}

	// 3. 查询账号信息
	var accounts []gaia.Account
	err = global.GVA_DB.Model(&gaia.Account{}).
		Where("id IN ?", accountIDs).
		Find(&accounts).Error
	if err != nil {
		err = fmt.Errorf("查询账号信息失败：%w", err)
		return
	}

	// 4. 创建账号 ID 到账号名称的映射
	accountMap := make(map[string]string)
	for _, account := range accounts {
		accountMap[account.ID.String()] = account.Name
	}

	/**
	查出应用使用次数AppStatisticsExtend
	*/
	var appStatistics []gaia.AppStatisticsExtend
	err = global.GVA_DB.Model(&gaia.AppStatisticsExtend{}).
		Where("app_id in ?", appIDs).
		Find(&appStatistics).Error
	if err != nil {
		err = fmt.Errorf("查询应用界面使用次数信息失败：%w", err)
		return
	}
	var appStatisticsMap = make(map[string]gaia.AppStatisticsExtend)
	for _, appStatistic := range appStatistics {
		appStatisticsMap[appStatistic.AppID.String()] = appStatistic
	}

	// 组装数据
	for i, r := range results {
		appInfo := appInfos[r.AppID]
		tenantID := appInfo.TenantID.String()
		accountID := tenantToAccountMap[tenantID]
		accountName := accountMap[accountID]
		appStatistic := appStatisticsMap[r.AppID]
		list = append(list, response.GetAppQuotaRankingDataRes{
			Ranking:      i + 1 + offset,
			Name:         appInfo.Name,
			AccountName:  accountName,
			Mode:         appInfos[r.AppID].Mode,
			AppID:        r.AppID,
			TotalCost:    r.TotalCost,
			MessageCost:  r.MessageCost,
			WorkflowCost: r.WorkflowCost,
			RecordNum:    r.RecordNum,
			UseNum:       appStatistic.Number,
		})
	}

	// TODO 因为只缓存了3页，
	if total >= 30 {
		total = 30
	}

	// 在返回结果之前，缓存结果
	result := struct {
		List  []response.GetAppQuotaRankingDataRes
		Total int64
	}{list, total}

	if err := dashboardService.cacheResult(cacheKey, result, 24*time.Hour); err != nil {
		global.GVA_LOG.Error("Failed to cache result", zap.Error(err))
	}

	return list, total, nil
}

// GetAppTokenQuotaRankingData 分页获取【应用密钥】配额排名数据列表
func (dashboardService *DashboardService) GetAppTokenQuotaRankingData(info gaiaReq.GetAppTokenQuotaRankingDataReq) (list []response.GetAppTokenQuotaRankingDataRes, total int64, err error) {
	limit := info.PageSize
	offset := info.PageSize * (info.Page - 1)

	db := global.GVA_DB.Model(&gaia.ApiTokenMoneyExtend{}).Order("accumulated_quota desc")
	var apiTokenMoneys []gaia.ApiTokenMoneyExtend
	err = db.Count(&total).Error
	if err != nil {
		return
	}

	if limit != 0 {
		db = db.Limit(limit).Offset(offset)
	}

	err = db.Find(&apiTokenMoneys).Error
	if err != nil {
		err = fmt.Errorf("查询密钥额度信息失败：%s", err.Error())
		return
	}

	// Api_token ID集合，方便后面一次性查出
	var apiTokenIds []uuid.UUID
	for _, money := range apiTokenMoneys {
		apiTokenIds = append(apiTokenIds, money.AppTokenID)
	}

	// 查询密钥信息
	var apiTokenInfos = make(map[uuid.UUID]gaia.ApiTokens)
	var apiIDs []uuid.UUID
	var apiTokens []gaia.ApiTokens
	err = global.GVA_DB.Where("id in ?", apiTokenIds).Find(&apiTokens).Error
	if err != nil {
		err = fmt.Errorf("查询密钥信息失败：%s", err.Error())
		return
	}
	for _, apiToken := range apiTokens {
		apiTokenInfos[apiToken.ID] = apiToken
		apiIDs = append(apiIDs, apiToken.AppID)
	}

	// 查询对应应用信息
	var appInfos = make(map[uuid.UUID]gaia.Apps)
	var apps []gaia.Apps
	err = global.GVA_DB.Where("id in ?", apiIDs).Find(&apps).Error
	if err != nil {
		err = fmt.Errorf("查询应用信息失败：%s", err.Error())
		return
	}
	for _, app := range apps {
		appInfos[app.ID] = app
	}

	// 拼接结果
	for i, money := range apiTokenMoneys {
		var apiToken gaia.ApiTokens
		var isExist bool
		if apiToken, isExist = apiTokenInfos[money.AppTokenID]; !isExist {
			global.GVA_LOG.Error("密钥信息不存在!", zap.String("account_id", money.AppTokenID.String()))
			continue
		}
		var appInfo gaia.Apps
		if appInfo, isExist = appInfos[apiToken.AppID]; !isExist {
			global.GVA_LOG.Error("密钥对应应用信息不存在!", zap.String("account_id", apiToken.AppID.String()))
			continue
		}
		row := response.GetAppTokenQuotaRankingDataRes{
			Ranking:          i + 1 + offset,
			Name:             appInfo.Name,
			AppToken:         apiToken.GenerateToken(),
			AccumulatedQuota: money.AccumulatedQuota,
			DayUsedQuota:     money.DayUsedQuota,
			MonthUsedQuota:   money.MonthUsedQuota,
			DayLimitQuota:    money.DayLimitQuota,
			MonthLimitQuota:  money.MonthLimitQuota,
		}
		list = append(list, row)
	}

	return list, total, err
}

// GetAppTokenDailyQuotaData 获取每天密钥花费数据列表
func (dashboardService *DashboardService) GetAppTokenDailyQuotaData(info gaiaReq.GetAppTokenDailyQuotaDataReq) (list []response.GetAppTokenDailyQuotaDataRes, err error) {

	db := global.GVA_DB.Select("DATE(stat_at) as stat_at, SUM(day_used_quota) as day_used_quota").Model(&gaia.ApiTokenMoneyDailyStatExtend{}).Order("stat_at desc").Group("DATE(stat_at)")
	var apiTokenMoneyDailyStatExtends []gaia.ApiTokenMoneyDailyStatExtend

	if info.AppId != "" {
		db = db.Where("app_token_id = ?", info.AppId)
	}

	if !info.StatAt.IsZero() {
		db = db.Where("stat_at = ?", info.StatAt)
	}

	err = db.Find(&apiTokenMoneyDailyStatExtends).Error
	if err != nil {
		err = fmt.Errorf("查询每日密钥花费信息失败：%s", err.Error())
		return
	}

	// 拼接结果
	for _, money := range apiTokenMoneyDailyStatExtends {
		row := response.GetAppTokenDailyQuotaDataRes{
			StatDate:  money.StatAt.Format("2006-01-02"),
			TotalUsed: money.DayUsedQuota,
		}
		list = append(list, row)
	}

	return list, err
}

// GetAiImageQuotaRankingData 获取【AI图片】使用量排名数据列表
func (dashboardService *DashboardService) GetAiImageQuotaRankingData(info gaiaReq.GetAiImageQuotaRankingDataReq) (list []response.GetAiImageQuotaRankingRes, err error) {
	limit := info.PageSize
	offset := info.PageSize * (info.Page - 1)

	db := global.GVA_DB.Table("account_layover_record_extend")
	db = db.Select(`
        forwarding_extend.address,
        forwarding_extend.path,
        SUM(account_layover_record_extend.money) AS total_cost,
        COUNT(*) AS record_num,
        account_layover_record_extend.info->>'model' AS model
    `)
	db = db.Joins("RIGHT JOIN accounts ON account_layover_record_extend.account_id = accounts.id")
	db = db.Joins("RIGHT JOIN forwarding_extend ON account_layover_record_extend.forwarding_id = forwarding_extend.id")

	// 添加时间范围筛选
	if !info.StatAt.IsZero() {
		startDate := info.StatAt
		endDate := startDate.AddDate(0, 1, 0) // 假设查询一个月的数据
		db = db.Where("account_layover_record_extend.created_at BETWEEN ? AND ?", startDate, endDate)
	}
	db = db.Having("SUM(account_layover_record_extend.money) > 0")
	db = db.Group("forwarding_extend.id, forwarding_extend.address, forwarding_extend.path, account_layover_record_extend.info->>'model'")
	db = db.Order("total_cost DESC")

	// 应用分页
	if limit != 0 {
		db = db.Limit(limit).Offset(offset)
	}

	var results []struct {
		Address   string
		Path      string
		TotalCost float64
		RecordNum int
		Model     string
	}

	err = db.Find(&results).Error
	if err != nil {
		return nil, fmt.Errorf("查询AI图片使用量排名数据失败：%s", err.Error())
	}

	// 构建响应
	for i, result := range results {
		row := response.GetAiImageQuotaRankingRes{
			Ranking:   i + 1, // 假设按查询结果顺序排名
			Address:   result.Address,
			Path:      result.Path,
			Model:     result.Model,
			TotalCost: result.TotalCost,
			RecordNum: result.RecordNum,
		}
		list = append(list, row)
	}

	return list, nil
}

func (dashboardService *DashboardService) cacheResult(key string, data interface{}, expiration time.Duration) error {

	jsonData, err := json.Marshal(data)
	if err != nil {
		return fmt.Errorf("failed to marshal data: %w", err)
	}
	return global.GVA_REDIS.Set(context.Background(), key, jsonData, expiration).Err()
}

func (dashboardService *DashboardService) getCachedResult(key string, result interface{}) (bool, error) {
	data, err := global.GVA_REDIS.Get(context.Background(), key).Bytes()
	if err != nil {
		if errors.Is(err, redis.Nil) {
			return false, nil
		}
		return false, fmt.Errorf("failed to get cached data: %w", err)
	}

	err = json.Unmarshal(data, result)
	if err != nil {
		return false, fmt.Errorf("failed to unmarshal cached data: %w", err)
	}

	return true, nil
}
