locals {
  tags = {
    project    = "guaranine"
    managed_by = "terraform"
    role       = "terraform-state"
  }
}

# Kept outside the coordinator RG so coordinator recovery or removal does not
# remove its state. Nothing here owns or references existing Azure services.
resource "azurerm_resource_group" "state" {
  name     = "guaranine-tfstate-rg"
  location = var.location
  tags     = local.tags

  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_storage_account" "state" {
  name                              = var.storage_account_name
  resource_group_name               = azurerm_resource_group.state.name
  location                          = azurerm_resource_group.state.location
  account_tier                      = "Standard"
  account_replication_type          = "LRS"
  account_kind                      = "StorageV2"
  min_tls_version                   = "TLS1_2"
  https_traffic_only_enabled        = true
  shared_access_key_enabled         = false
  default_to_oauth_authentication   = true
  allow_nested_items_to_be_public   = false
  cross_tenant_replication_enabled  = false
  local_user_enabled                = false
  infrastructure_encryption_enabled = true
  tags                              = local.tags

  network_rules {
    default_action = "Deny"
    bypass         = ["None"]
    # Azure expects individual IPs instead of /32 CIDRs for single addresses.
    ip_rules = [for cidr in var.operator_ipv4_cidrs : trimsuffix(cidr, "/32")]
  }

  blob_properties {
    versioning_enabled = true
    delete_retention_policy {
      days = 30
    }
    container_delete_retention_policy {
      days = 30
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

# Bootstrap needs role-assignment permission. No application identity, account
# key, password, or credential is minted or exported by Terraform.
resource "azurerm_role_assignment" "state_operator" {
  scope                = azurerm_storage_account.state.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = var.operator_object_id
  principal_type       = "User"
}

resource "azurerm_storage_container" "state" {
  name                  = "tfstate"
  storage_account_id    = azurerm_storage_account.state.id
  container_access_type = "private"

  depends_on = [azurerm_role_assignment.state_operator]

  lifecycle {
    prevent_destroy = true
  }
}
