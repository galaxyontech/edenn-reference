# Edenn — Japan East Terraform IaC

## Status: Code Complete, Not Yet Deployed

**Branch:** `japan-prod`
**Date:** 2026-03-27
**Scope:** Japan East deployment only (no US West codification)

---

## What Was Done

Created complete Terraform IaC to deploy the Edenn API (FastAPI) to Azure Japan East.

### Files Created (26 files)

```
terraform/
  modules/
    resource-group/          # Azure Resource Group
      main.tf                #   azurerm_resource_group
      variables.tf           #   name, location, tags
      outputs.tf             #   name, location, id

    log-analytics/           # Log Analytics Workspace
      main.tf                #   azurerm_log_analytics_workspace
      variables.tf           #   name, location, resource_group_name, sku, retention
      outputs.tf             #   id, workspace_id, primary_shared_key

    storage/                 # Azure Blob Storage
      main.tf                #   azurerm_storage_account (Standard/ZRS/Hot/TLS1.2)
                             #   azurerm_storage_container (for_each)
      variables.tf           #   name, location, resource_group_name, containers list
      outputs.tf             #   name, id, primary_access_key, primary_blob_endpoint

    key-vault/               # Azure Key Vault (RBAC mode)
      main.tf                #   azurerm_key_vault (RBAC authorization, no access policies)
                             #   azurerm_role_assignment (deployer gets KV Administrator)
                             #   azurerm_key_vault_secret (for_each, ignore_changes on value)
      variables.tf           #   name, location, resource_group_name, secrets map
      outputs.tf             #   id, name, vault_uri, secret_uris (versionless)

    container-app-env/       # Container Apps Environment
      main.tf                #   azurerm_container_app_environment (Consumption, no VNet)
      variables.tf           #   name, location, resource_group_name, log_analytics_id, zone_redundancy
      outputs.tf             #   id, name

    container-app/           # Container App + Identity + Role Assignments
      main.tf                #   azurerm_user_assigned_identity (created first)
                             #   azurerm_role_assignment x2 (AcrPull + KV Secrets User)
                             #   azurerm_container_app (depends_on role assignments)
                             #     - 7 Key Vault secret references
                             #     - 13 plain env vars
                             #     - Liveness + readiness probes on /healthz
                             #     - HTTP scale rule (100 concurrent req)
                             #     - Min 2 / Max 30 replicas, 2 CPU / 4Gi memory
      variables.tf           #   19 variables (app_name, acr_*, kv_*, model_gateway_*, etc.)
      outputs.tf             #   id, fqdn, principal_id, identity_id, name, latest_revision_name

  environments/
    japan-east/
      versions.tf            # terraform >= 1.5, azurerm ~> 4.0
      providers.tf           # azurerm provider with subscription_id
      backend.tf             # Remote state: satfstateedenn/tfstate/japan-east/
      main.tf                # Wires all 6 modules together
      variables.tf           # All variable declarations (secrets marked sensitive)
      outputs.tf             # container_app_fqdn, storage_account_name, key_vault_name
      terraform.tfvars.example  # Template with real subscription/ACR values
```

### Other Changes

- `.gitignore` — Added terraform state files, `.terraform/` dirs, `terraform.tfvars`

### Architecture Decisions

| Decision | Why |
|----------|-----|
| **User-assigned managed identity** (not system-assigned) | Breaks chicken-and-egg: identity + roles created BEFORE Container App, so ACR pull and KV secret read work on first deploy |
| **RBAC for Key Vault** (not access policies) | Modern best practice, cleaner with managed identity |
| **Consumption plan** (no workload profile) | Cheaper, sufficient for v1. Pay-per-use with autoscaling |
| **No VNet** | App needs internet for ProviderA/ProviderC/the model gateway. VNet adds complexity with no benefit for v1 |
| **No Front Door** | Built-in Container App HTTPS ingress is sufficient. Add later for CDN/WAF |
| **No database** | API is completely stateless. Blob Storage handles all media |
| **Zone redundancy off** (default) | Can enable later; irreversible once enabled |
| **`ignore_changes = [value]` on KV secrets** | Terraform won't overwrite manually-rotated secrets |

### Resource Names (Japan East)

| Resource | Name |
|----------|------|
| Resource Group | `rg-edenn-japaneast` |
| Log Analytics | `law-edenn-jp` |
| Storage Account | `primarystorage` |
| Key Vault | `example-vault` |
| Container App Env | `cae-edenn-jp` |
| Container App | `prod-app` |
| Managed Identity | `identity-app` |

### Key Vault Secrets (7)

| Secret Name | Maps To Env Var |
|-------------|----------------|
| `azure-api-key` | `AZURE_API_KEY` |
| `provider_a-api-key` | `PROVIDER_A_API_KEY` |
| `provider_c-api-key` | `PROVIDER_C_API_KEY` |
| `provider_b-api-key` | `PROVIDER_B_API_KEY` |
| `storage-account-key` | `AZURE_STORAGE_ACCOUNT_KEY` |
| `admin-secret` | `EDEN_ADMIN_SECRET` |
| `provider_c-webhook-secret` | `PROVIDER_C_WEBHOOK_SECRET` |

---

## What's NOT Done (Next Steps)

Everything below must be done before `terraform apply`. Steps are sequential.

### Pre-Flight (Azure CLI, manual, one-time)

#### 1. Create Terraform state storage account

```bash
az group create --name rg-tfstate --location japaneast

az storage account create \
  --name satfstateedenn \
  --resource-group rg-tfstate \
  --location japaneast \
  --sku Standard_LRS

az storage container create \
  --name tfstate \
  --account-name satfstateedenn
```

#### 2. Geo-replicate ACR to japaneast

Without this, Container Apps pull images cross-Pacific (30-60s cold start penalty).

```bash
# Check current SKU:
az acr show --name exampleregistry --query sku.name -o tsv

# If Standard, upgrade to Premium (required for geo-replication):
az acr update --name exampleregistry --sku Premium

# Replicate:
az acr replication create \
  --location japaneast \
  --registry exampleregistry \
  --resource-group edenn-westus
```

#### 3. Verify Japan East quotas

```bash
az vm list-usage --location japaneast \
  --query '[?contains(name.localizedValue,`Core`)].{name:name.localizedValue,current:currentValue,limit:limit}'
```

Target: 20+ vCPU headroom.

#### 4. Create service principal for Terraform

```bash
az ad sp create-for-rbac \
  --name "sp-edenn-terraform" \
  --role Contributor \
  --scopes /subscriptions/00000000-0000-0000-0000-000000000000
```

Save output — you need `appId`, `password`, `tenant` for ARM_* env vars.

#### 5. Create the model gateway resource in Japan East

- Deploy new the model gateway resource in `japaneast` region
- Deploy model: `chat-standard` (matching current US config)
- Note the endpoint URL and API key

### Build & Push Docker Image

```bash
docker build --platform linux/amd64 -t registry.example.invalid/edenn-api:jp-prod .
az acr login --name exampleregistry
docker push registry.example.invalid/edenn-api:jp-prod
```

### Configure & Deploy

```bash
# 1. Create terraform.tfvars from example:
cp terraform/environments/japan-east/terraform.tfvars.example \
   terraform/environments/japan-east/terraform.tfvars

# 2. Edit terraform.tfvars — fill in model_gateway_endpoint and model_gateway_model

# 3. Export secrets:
export TF_VAR_azure_api_key="..."
export TF_VAR_provider_a_api_key="..."
export TF_VAR_provider_c_api_key="..."
export TF_VAR_provider_b_api_key="..."
export TF_VAR_admin_secret="..."
export TF_VAR_provider_c_webhook_secret="..."

# 4. Export ARM credentials (from service principal):
export ARM_CLIENT_ID="..."
export ARM_CLIENT_SECRET="..."
export ARM_SUBSCRIPTION_ID="00000000-0000-0000-0000-000000000000"
export ARM_TENANT_ID="..."

# 5. Init + Plan + Apply:
cd terraform/environments/japan-east
terraform init
terraform plan    # Review — should show ~10 resources to create
terraform apply   # Type "yes" to confirm
```

### Verify

```bash
# Get URL:
terraform output container_app_url

# Health check:
curl https://<fqdn>/healthz
# Expect: {"status": "ok"}

# Check resources:
az containerapp show --name prod-app -g rg-edenn-japaneast --query properties.runningStatus
az keyvault secret list --vault-name example-vault --query '[].name'
az storage container list --account-name primarystorage --query '[].name'

# End-to-end: submit a test video job via POST
```

### After IaC Is Tested

- Add GitHub Actions CI/CD (two workflows: app deploy + infra deploy)
- Consider enabling zone redundancy
- Consider adding Front Door for CDN/WAF
- Consider custom domain (`api-jp.edenn.com`) via Azure DNS or Container App custom domain
