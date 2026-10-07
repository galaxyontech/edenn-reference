resource "azurerm_storage_account" "this" {
  name                     = var.name
  location                 = var.location
  resource_group_name      = var.resource_group_name
  account_tier             = "Standard"
  account_replication_type = "ZRS"
  access_tier              = "Hot"
  min_tls_version          = "TLS1_2"
  https_traffic_only_enabled = true

  tags = var.tags

  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_storage_container" "this" {
  for_each = toset(var.containers)

  name               = each.value
  storage_account_id = azurerm_storage_account.this.id
}
