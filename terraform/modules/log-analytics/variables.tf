variable "name" {
  description = "The name of the Log Analytics workspace."
  type        = string
}

variable "location" {
  description = "The Azure region where the workspace will be created."
  type        = string
}

variable "resource_group_name" {
  description = "The name of the resource group in which to create the workspace."
  type        = string
}

variable "sku" {
  description = "The SKU of the Log Analytics workspace."
  type        = string
  default     = "PerGB2018"
}

variable "retention_in_days" {
  description = "The number of days to retain log data."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Tags to apply to the workspace."
  type        = map(string)
  default     = {}
}
