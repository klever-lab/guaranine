output "backend" {
  description = "Non-secret Azure Blob backend coordinates. Use distinct state/bootstrap.tfstate and coordinator/terraform.tfstate keys."
  value = {
    resource_group_name  = azurerm_resource_group.state.name
    storage_account_name = azurerm_storage_account.state.name
    container_name       = azurerm_storage_container.state.name
  }
}
