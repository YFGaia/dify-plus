package gaia

type ServiceGroup struct {
	SystemIntegratedService
	DashboardService
	QuotaService
	TenantsService
	TestService
	BatchWorkflowService
}
