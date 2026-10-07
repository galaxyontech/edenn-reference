output "id" {
  description = "The resource ID of the Key Vault."
  value       = azurerm_key_vault.main.id
}

output "name" {
  description = "The name of the Key Vault."
  value       = azurerm_key_vault.main.name
}

output "vault_uri" {
  description = "The URI of the Key Vault (e.g. https://<name>.vault.azure.net/)."
  value       = azurerm_key_vault.main.vault_uri
}

output "secret_uris" {
  description = "Map of secret name to versionless Key Vault secret URI. Reference these URIs in Container App secret definitions."
  value       = { for k, v in azurerm_key_vault_secret.main : k => v.versionless_id }
}
