mock_provider "azurerm" {
  source          = "tests/fixtures"
  override_during = plan
}

variables {
  subscription_id      = "00000000-0000-0000-0000-000000000000"
  operator_object_id   = "11111111-1111-1111-1111-111111111111"
  storage_account_name = "guaraninemockstate"
  operator_ipv4_cidrs  = ["203.0.113.10/32"]
}

run "entra_only_private_state" {
  command = plan

  assert {
    condition = (
      !azurerm_storage_account.state.shared_access_key_enabled &&
      azurerm_storage_account.state.default_to_oauth_authentication &&
      !azurerm_storage_account.state.allow_nested_items_to_be_public &&
      !azurerm_storage_account.state.local_user_enabled &&
      azurerm_storage_account.state.min_tls_version == "TLS1_2" &&
      azurerm_storage_account.state.https_traffic_only_enabled &&
      azurerm_storage_account.state.infrastructure_encryption_enabled &&
      azurerm_storage_container.state.container_access_type == "private" &&
      azurerm_storage_account.state.network_rules[0].default_action == "Deny" &&
      azurerm_storage_account.state.network_rules[0].bypass == toset(["None"]) &&
      azurerm_storage_account.state.network_rules[0].ip_rules == toset(["203.0.113.10"])
    )
    error_message = "State must use TLS/Entra ID, disable shared keys/public blobs and restrict the data plane to operator egress."
  }

  assert {
    condition = (
      azurerm_role_assignment.state_operator.role_definition_name == "Storage Blob Data Contributor" &&
      azurerm_role_assignment.state_operator.principal_id == "11111111-1111-1111-1111-111111111111" &&
      azurerm_role_assignment.state_operator.scope == azurerm_storage_account.state.id &&
      azurerm_resource_group.state.name == "guaranine-tfstate-rg" &&
      azurerm_storage_account.state.account_replication_type == "LRS" &&
      azurerm_storage_account.state.blob_properties[0].versioning_enabled &&
      azurerm_storage_account.state.blob_properties[0].delete_retention_policy[0].days == 30 &&
      azurerm_storage_account.state.blob_properties[0].container_delete_retention_policy[0].days == 30
    )
    error_message = "Scope the existing user's blob role to the separate protected state account and retain recoverable versions."
  }
}

run "configurable_state_region_and_network" {
  command = plan
  variables {
    location            = "westeurope"
    operator_ipv4_cidrs = ["198.51.100.0/24", "203.0.113.7/32"]
  }

  assert {
    condition = (
      azurerm_storage_account.state.location == "westeurope" &&
      azurerm_storage_account.state.network_rules[0].ip_rules == toset(["198.51.100.0/24", "203.0.113.7"])
    )
    error_message = "Use exactly the selected region and normalize only /32 address rules."
  }
}

run "reject_world_access" {
  command = plan
  variables {
    operator_ipv4_cidrs = ["0.0.0.0/0"]
  }
  expect_failures = [var.operator_ipv4_cidrs]
}

run "reject_empty_firewall" {
  command = plan
  variables {
    operator_ipv4_cidrs = []
  }
  expect_failures = [var.operator_ipv4_cidrs]
}

run "reject_unstable_account_name" {
  command = plan
  variables {
    storage_account_name = "otherproject"
  }
  expect_failures = [var.storage_account_name]
}
