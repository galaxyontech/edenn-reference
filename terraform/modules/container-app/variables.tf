########################################
# Required Variables
########################################

variable "app_name" {
  description = "Name of the Container App"
  type        = string
}

variable "resource_group_name" {
  description = "Name of the resource group"
  type        = string
}

variable "location" {
  description = "Azure region for the user-assigned managed identity"
  type        = string
}

variable "environment_id" {
  description = "Container App Environment ID"
  type        = string
}

variable "acr_login_server" {
  description = "ACR login server hostname (e.g. registry.example.invalid)"
  type        = string
}

variable "acr_id" {
  description = "Full resource ID of the Azure Container Registry (used as role assignment scope)"
  type        = string
}

variable "key_vault_id" {
  description = "Full resource ID of the Key Vault (used as role assignment scope)"
  type        = string
}

variable "kv_secret_uris" {
  description = "Map of secret name to versionless Key Vault secret URI"
  type        = map(string)
}

variable "model_gateway_endpoint" {
  description = "the model gateway service endpoint URL"
  type        = string
}

variable "model_gateway_model" {
  description = "the model gateway deployment / model name"
  type        = string
}

variable "storage_account_url" {
  description = "Azure Storage account blob endpoint URL"
  type        = string
}

variable "storage_account_name" {
  description = "Azure Storage account name"
  type        = string
}

########################################
# Optional Variables (with defaults)
########################################

variable "image_tag" {
  description = "Container image tag to deploy"
  type        = string
  default     = "jp-prod"
}

variable "azure_api_version" {
  description = "the model gateway API version"
  type        = string
  default     = "2024-12-01-preview"
}

variable "min_replicas" {
  description = "Minimum number of container replicas"
  type        = number
  default     = 2
}

variable "max_replicas" {
  description = "Maximum number of container replicas"
  type        = number
  default     = 100
}

variable "cpu" {
  description = "CPU cores allocated to each container replica"
  type        = number
  default     = 2.0
}

variable "memory" {
  description = "Memory allocated to each container replica (e.g. 4Gi)"
  type        = string
  default     = "4Gi"
}

variable "log_level" {
  description = "Application log level"
  type        = string
  default     = "INFO"
}

variable "allowed_origins" {
  description = "CORS allowed origins"
  type        = string
  default     = "*"
}

variable "upload_container_name" {
  description = "Name of the blob container for user uploads"
  type        = string
  default     = "user-uploads"
}

variable "output_container_name" {
  description = "Name of the blob container for generated media"
  type        = string
  default     = "generated-media"
}

variable "audio_container_name" {
  description = "Name of the blob container for audio files"
  type        = string
  default     = "audio"
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default     = {}
}

variable "cos_region" {
  description = "Tencent COS region (e.g. ap-guangzhou)"
  type        = string
}

variable "cos_bucket_domain" {
  description = "Tencent COS bucket service domain (e.g. objectstore.example.invalid)"
  type        = string
}

variable "cos_video_bucket" {
  description = "Tencent COS bucket name for video uploads"
  type        = string
}

variable "cos_audio_bucket" {
  description = "Tencent COS bucket name for audio uploads"
  type        = string
}

variable "cos_image_bucket" {
  description = "Tencent COS bucket name for image uploads"
  type        = string
}

variable "cos_video_domain" {
  description = "Public CDN/custom domain serving the video bucket"
  type        = string
}

variable "cos_audio_domain" {
  description = "Public CDN/custom domain serving the audio bucket"
  type        = string
}

variable "cos_image_domain" {
  description = "Public CDN/custom domain serving the image bucket"
  type        = string
}

variable "media_storage_provider" {
  description = "Media storage provider: auto, azure, or tencent_cos"
  type        = string
  default     = "auto"
}

variable "extra_secret_env" {
  description = "Additional runtime env vars whose values come from Key Vault secrets. Map env var name to Key Vault secret name."
  type        = map(string)
  default     = {}
}

variable "extra_plain_env" {
  description = "Additional non-secret runtime env vars to set directly on the container."
  type        = map(string)
  default     = {}
}
