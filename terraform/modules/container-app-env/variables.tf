variable "name" {
  description = "The name of the Container Apps environment."
  type        = string
}

variable "location" {
  description = "The Azure region where the environment will be created."
  type        = string
}

variable "resource_group_name" {
  description = "The name of the resource group in which to create the environment."
  type        = string
}

variable "log_analytics_workspace_id" {
  description = "The resource ID of the Log Analytics workspace to associate with this environment."
  type        = string
}
