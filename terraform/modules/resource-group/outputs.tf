output "name" {
  description = "The name of the resource group."
  value       = azurerm_resource_group.this.name
}

output "location" {
  description = "The Azure region of the resource group."
  value       = azurerm_resource_group.this.location
}

output "id" {
  description = "The resource ID of the resource group."
  value       = azurerm_resource_group.this.id
}
