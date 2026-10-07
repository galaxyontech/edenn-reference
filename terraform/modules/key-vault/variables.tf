variable "name" {
  description = "Key Vault name (globally unique, max 24 characters)."
  type        = string
}

variable "location" {
  description = "Azure region where the Key Vault will be created."
  type        = string
}

variable "resource_group_name" {
  description = "Name of the resource group to deploy the Key Vault into."
  type        = string
}

variable "secrets" {
  description = "Map of secret names to their initial values. Values are marked sensitive and will not be overwritten after initial creation."
  type        = map(string)
  sensitive   = true
  default     = {}
}

variable "tags" {
  description = "Map of tags to apply to all resources in this module."
  type        = map(string)
  default     = {}
}
