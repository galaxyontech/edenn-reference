resource "azurerm_container_app_environment" "this" {
  name                       = var.name
  location                   = var.location
  resource_group_name        = var.resource_group_name
  log_analytics_workspace_id = var.log_analytics_workspace_id
  # zone_redundancy_enabled requires infrastructure_subnet_id (VNet).
  # Omitted for Consumption plan without VNet.
}
