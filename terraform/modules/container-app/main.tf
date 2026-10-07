########################################
# User-Assigned Managed Identity
# Created BEFORE the Container App so
# roles can be assigned before the app
# tries to pull images or read secrets.
########################################

resource "azurerm_user_assigned_identity" "main" {
  name                = "${var.app_name}-identity"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

########################################
# Role Assignments (before Container App)
########################################

resource "azurerm_role_assignment" "acr_pull" {
  scope                = var.acr_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.main.principal_id
}

resource "azurerm_role_assignment" "kv_reader" {
  scope                = var.key_vault_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.main.principal_id
}

########################################
# Container App
########################################

resource "azurerm_container_app" "main" {
  name                         = var.app_name
  container_app_environment_id = var.environment_id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"

  tags = var.tags

  # Roles must be propagated before the app tries to use them
  depends_on = [
    azurerm_role_assignment.acr_pull,
    azurerm_role_assignment.kv_reader,
  ]

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.main.id]
  }

  registry {
    server   = var.acr_login_server
    identity = azurerm_user_assigned_identity.main.id
  }

  # ── Key Vault secret references ────────────────────────────────

  secret {
    name                = "azure-api-key"
    key_vault_secret_id = var.kv_secret_uris["azure-api-key"]
    identity            = azurerm_user_assigned_identity.main.id
  }

  secret {
    name                = "provider_a-api-key"
    key_vault_secret_id = var.kv_secret_uris["provider_a-api-key"]
    identity            = azurerm_user_assigned_identity.main.id
  }

  secret {
    name                = "provider_c-api-key"
    key_vault_secret_id = var.kv_secret_uris["provider_c-api-key"]
    identity            = azurerm_user_assigned_identity.main.id
  }

  secret {
    name                = "provider_b-api-key"
    key_vault_secret_id = var.kv_secret_uris["provider_b-api-key"]
    identity            = azurerm_user_assigned_identity.main.id
  }

  secret {
    name                = "storage-account-key"
    key_vault_secret_id = var.kv_secret_uris["storage-account-key"]
    identity            = azurerm_user_assigned_identity.main.id
  }

  secret {
    name                = "admin-secret"
    key_vault_secret_id = var.kv_secret_uris["admin-secret"]
    identity            = azurerm_user_assigned_identity.main.id
  }

  secret {
    name                = "provider_c-webhook-secret"
    key_vault_secret_id = var.kv_secret_uris["provider_c-webhook-secret"]
    identity            = azurerm_user_assigned_identity.main.id
  }

  secret {
    name                = "cos-secret-id"
    key_vault_secret_id = var.kv_secret_uris["cos-secret-id"]
    identity            = azurerm_user_assigned_identity.main.id
  }

  secret {
    name                = "cos-secret-key"
    key_vault_secret_id = var.kv_secret_uris["cos-secret-key"]
    identity            = azurerm_user_assigned_identity.main.id
  }

  dynamic "secret" {
    for_each = var.extra_secret_env

    content {
      name                = secret.value
      key_vault_secret_id = var.kv_secret_uris[secret.value]
      identity            = azurerm_user_assigned_identity.main.id
    }
  }

  # ── Ingress ────────────────────────────────────────────────────

  ingress {
    external_enabled = true
    target_port      = 8080
    transport        = "auto"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  # ── Template ───────────────────────────────────────────────────

  template {
    min_replicas = var.min_replicas
    max_replicas = var.max_replicas

    container {
      name   = "edenn-api"
      image  = "${var.acr_login_server}/edenn-api:${var.image_tag}"
      cpu    = var.cpu
      memory = var.memory

      # Secret env vars (Key Vault references)
      env {
        name        = "AZURE_API_KEY"
        secret_name = "azure-api-key"
      }

      env {
        name        = "PROVIDER_A_API_KEY"
        secret_name = "provider_a-api-key"
      }

      env {
        name        = "PROVIDER_C_API_KEY"
        secret_name = "provider_c-api-key"
      }

      env {
        name        = "PROVIDER_B_API_KEY"
        secret_name = "provider_b-api-key"
      }

      env {
        name        = "AZURE_STORAGE_ACCOUNT_KEY"
        secret_name = "storage-account-key"
      }

      env {
        name        = "EDEN_ADMIN_SECRET"
        secret_name = "admin-secret"
      }

      env {
        name        = "PROVIDER_C_WEBHOOK_SECRET"
        secret_name = "provider_c-webhook-secret"
      }

      dynamic "env" {
        for_each = var.extra_secret_env

        content {
          name        = env.key
          secret_name = env.value
        }
      }

      # Plain env vars
      env {
        name  = "AZURE_ENDPOINT"
        value = var.model_gateway_endpoint
      }

      env {
        name  = "AZURE_MODEL"
        value = var.model_gateway_model
      }

      env {
        name  = "AZURE_API_VERSION"
        value = var.azure_api_version
      }

      env {
        name  = "AZURE_STORAGE_ACCOUNT_URL"
        value = var.storage_account_url
      }

      env {
        name  = "AZURE_STORAGE_ACCOUNT_NAME"
        value = var.storage_account_name
      }

      env {
        name  = "AZURE_STORAGE_UPLOAD_CONTAINER"
        value = var.upload_container_name
      }

      env {
        name  = "AZURE_STORAGE_OUTPUT_CONTAINER"
        value = var.output_container_name
      }

      env {
        name  = "AZURE_STORAGE_AUDIO_CONTAINER"
        value = var.audio_container_name
      }

      env {
        name  = "API_WORKDIR"
        value = "/app/api_workdir"
      }

      env {
        name  = "API_LOG_LEVEL"
        value = var.log_level
      }

      env {
        name  = "API_ALLOWED_ORIGINS"
        value = var.allowed_origins
      }

      env {
        name  = "PROVIDER_A_TIMEOUT"
        value = "120"
      }

      env {
        name  = "AZURE_STORAGE_SAS_TTL_MINUTES"
        value = "1440"
      }

      dynamic "env" {
        for_each = var.extra_plain_env

        content {
          name  = env.key
          value = env.value
        }
      }

      # Tencent COS — secret env vars
      env {
        name        = "COS_SECRET_ID"
        secret_name = "cos-secret-id"
      }

      env {
        name        = "COS_SECRET_KEY"
        secret_name = "cos-secret-key"
      }

      # Tencent COS — plain env vars
      env {
        name  = "COS_REGION"
        value = var.cos_region
      }

      env {
        name  = "COS_BUCKET_DOMAIN"
        value = var.cos_bucket_domain
      }

      env {
        name  = "COS_VIDEO_BUCKET"
        value = var.cos_video_bucket
      }

      env {
        name  = "COS_AUDIO_BUCKET"
        value = var.cos_audio_bucket
      }

      env {
        name  = "COS_IMAGE_BUCKET"
        value = var.cos_image_bucket
      }

      env {
        name  = "COS_VIDEO_DOMAIN"
        value = var.cos_video_domain
      }

      env {
        name  = "COS_AUDIO_DOMAIN"
        value = var.cos_audio_domain
      }

      env {
        name  = "COS_IMAGE_DOMAIN"
        value = var.cos_image_domain
      }

      env {
        name  = "MEDIA_STORAGE_PROVIDER"
        value = var.media_storage_provider
      }

      # Liveness probe
      liveness_probe {
        transport               = "HTTP"
        port                    = 8080
        path                    = "/healthz"
        initial_delay           = 10
        interval_seconds        = 30
        timeout                 = 5
        failure_count_threshold = 3
      }

      # Readiness probe
      readiness_probe {
        transport               = "HTTP"
        port                    = 8080
        path                    = "/healthz"
        interval_seconds        = 10
        timeout                 = 5
        failure_count_threshold = 3
      }

      # Startup probe (allows longer init before liveness kicks in)
      startup_probe {
        transport               = "HTTP"
        port                    = 8080
        path                    = "/healthz"
        interval_seconds        = 5
        timeout                 = 3
        failure_count_threshold = 10
      }
    }

    http_scale_rule {
      name                = "http-scaling"
      concurrent_requests = "1"
    }
  }
}
