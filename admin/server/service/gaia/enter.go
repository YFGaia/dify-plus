package gaia

type ServiceGroup struct {
	SystemIntegratedService
	DashboardService
	QuotaService
	TenantsService
	TestService
	BatchWorkflowService
	// extned: app version
	AppVersionService
	// extend: model provider
	ModelProviderService
}
