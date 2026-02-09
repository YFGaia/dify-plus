package initialize

import (
	"github.com/flipped-aurora/gin-vue-admin/server/router"
	"github.com/gin-gonic/gin"
)

func holder(routers ...*gin.RouterGroup) {
	_ = routers
	_ = router.RouterGroupApp
}
func initBizRouter(routers ...*gin.RouterGroup) {
	privateGroup := routers[0]
	publicGroup := routers[1]
	holder(publicGroup, privateGroup)
	{
		gaiaRouter := router.RouterGroupApp.Gaia
		gaiaRouter.InitDashboardRouter(privateGroup, publicGroup)
		gaiaRouter.InitQuotaRouter(privateGroup, publicGroup)
		gaiaRouter.InitTenantsRouter(privateGroup, publicGroup)
		gaiaRouter.InitTestRouter(privateGroup, publicGroup)
		gaiaRouter.InitSystemRouter(privateGroup)
		gaiaRouter.InitWorkflowRouter(privateGroup)
		gaiaRouter.InitAppVersionRouter(publicGroup, privateGroup)
	}
}
