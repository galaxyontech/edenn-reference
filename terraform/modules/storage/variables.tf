variable "name" {
  description = "Storage account name"
  type        = string
}

variable "location" {
  description = "Azure region"
  type        = string
}

variable "resource_group_name" {
  description = "Resource group name"
  type        = string
}

variable "containers" {
  description = "List of blob container names to create"
  type        = list(string)
  default     = ["user-uploads", "generated-media", "audio"]
}

variable "tags" {
  description = "Tags to apply to resources"
  type        = map(string)
  default     = {}
}
