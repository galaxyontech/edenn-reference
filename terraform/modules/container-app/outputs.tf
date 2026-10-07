output "id" {
  description = "Container App resource ID"
  value       = azurerm_container_app.main.id
}

output "fqdn" {
  description = "Container App fully qualified domain name"
  value       = azurerm_container_app.main.ingress[0].fqdn
}

output "principal_id" {
  description = "User-assigned managed identity principal ID"
  value       = azurerm_user_assigned_identity.main.principal_id
}

output "identity_id" {
  description = "User-assigned managed identity resource ID"
  value       = azurerm_user_assigned_identity.main.id
}

output "name" {
  description = "Container App name"
  value       = azurerm_container_app.main.name
}

output "latest_revision_name" {
  description = "Name of the latest deployed revision"
  value       = azurerm_container_app.main.latest_revision_name
}
