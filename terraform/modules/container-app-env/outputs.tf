output "id" {
  description = "The resource ID of the Container Apps environment."
  value       = azurerm_container_app_environment.this.id
}

output "name" {
  description = "The name of the Container Apps environment."
  value       = azurerm_container_app_environment.this.name
}
