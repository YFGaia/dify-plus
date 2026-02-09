package gaia

import "github.com/flipped-aurora/gin-vue-admin/server/service"

type ApiGroup struct {
	DashboardApi
	QuotaApi
	TenantsApi
	SystemApi
	TestApi
	SystemOAuth2Api
	BatchWorkflowApi
	AppVersionApi
}

var (
	dashboardService        = service.ServiceGroupApp.GaiaServiceGroup.DashboardService
	tenantsService          = service.ServiceGroupApp.GaiaServiceGroup.TenantsService
	systemIntegratedService = service.ServiceGroupApp.GaiaServiceGroup.SystemIntegratedService
)
var QuotaService = service.ServiceGroupApp.GaiaServiceGroup.QuotaService
var TestService = service.ServiceGroupApp.GaiaServiceGroup.TestService
