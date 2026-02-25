package cron

import (
	"context"
	"fmt"
	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/model/common/request"
	gaiaReq "github.com/flipped-aurora/gin-vue-admin/server/model/gaia/request"
	"github.com/flipped-aurora/gin-vue-admin/server/service/gaia"
	"github.com/flipped-aurora/gin-vue-admin/server/service/system"
	"github.com/robfig/cron/v3"
	"time"
)

// 返回一个支持至 秒 级别的 cron
func newWithSeconds() *cron.Cron {
	secondParser := cron.NewParser(cron.Second | cron.Minute |
		cron.Hour | cron.Dom | cron.Month | cron.DowOptional | cron.Descriptor)
	return cron.New(cron.WithParser(secondParser), cron.WithChain())
}

func Corn() {
	var lock bool
	c := newWithSeconds()
	// 每分钟同步一次用户列表
	if _, err := c.AddFunc("0 */1 * * * *", func() {
		if global.GVA_DB == nil {
			global.GVA_LOG.Info("【定时任务-每1分钟执行1次】同步用户列表任务，数据库没有初始化，暂未开始同步")
			return
		}

		if lock {
			return
		}
		lock = true
		user := system.UserExtendService{}
		user.SyncUser()
		gaia.SyncUserStatus()
		lock = false
	}); err != nil {
		global.GVA_LOG.Fatal("Start Cron Error:" + err.Error())
		time.Sleep(5)
		return
	}
	global.GVA_LOG.Info("【定时任务-每1分钟执行1次】同步用户列表任务，已启动！")

	// 一天同步一次～待改目前没啥用【应用使用分析数据】
	if _, err := c.AddFunc("0 0 1 * * *", func() {
		if global.GVA_DB == nil {
			global.GVA_LOG.Info("【定时任务-每6分钟执行1次】同步应用使用分析数据任务，数据库没有初始化，暂未开始同步")
			return
		}
		dashService := gaia.DashboardService{}
		// 缓存前3页
		for i := 1; i <= 3; i++ {
			req := gaiaReq.GetAppQuotaRankingDataReq{
				PageInfo: request.PageInfo{
					Page:     i,
					PageSize: 10,
				},
			}
			// 先删除缓存
			cacheKey := fmt.Sprintf("app_token_quota_ranking:%d:%d", i, 10)
			global.GVA_REDIS.Del(context.Background(), cacheKey)

			// 再获取数据
			_, _, err := dashService.GetAppQuotaRankingData(req)
			if err != nil {
				global.GVA_LOG.Error("每10分钟同步一次应用使用分析 获取信息出错:" + err.Error())
				return
			}
			time.Sleep(time.Second * 10)
		}

	}); err != nil {
		global.GVA_LOG.Fatal("每10分钟同步一次应用使用分析 出错:" + err.Error())
		return
	}
	global.GVA_LOG.Info("【定时任务-每6分钟执行1次】同步应用使用分析数据任务，已启动！")

	c.Start()
}
