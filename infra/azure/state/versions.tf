terraform {
  required_version = "= 1.17.1"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "= 5.13.0"
    }
  }

  backend "azurerm" {
    use_azuread_auth = true
    use_cli          = true
  }
}

provider "azurerm" {
  subscription_id                 = var.subscription_id
  resource_provider_registrations = "none"
  storage_use_azuread             = true

  features {
    resource_group {
      prevent_deletion_if_contains_resources = true
    }
  }
}
