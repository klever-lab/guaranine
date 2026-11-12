terraform {
  required_version = "= 1.17.1"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "= 5.13.0"
    }
  }

  # One operator; keep state on protected storage outside the checkout.
  backend "local" {}
}

provider "azurerm" {
  subscription_id                 = var.subscription_id
  resource_provider_registrations = "none"

  features {
    resource_group {
      prevent_deletion_if_contains_resources = true
    }
    virtual_machine {
      delete_os_disk_on_deletion = true
    }
  }
}

variable "subscription_id" {
  type        = string
  description = "Explicit student subscription, selected after credit/quota/cost preflight."
  validation {
    condition     = can(regex("^[0-9a-fA-F-]{36}$", var.subscription_id))
    error_message = "Use an explicit subscription UUID."
  }
}

variable "location" {
  type    = string
  default = "eastus"
}

variable "image_name" {
  type        = string
  description = "Unique artifact identity; use a new name/state for new private image contents."
  validation {
    condition     = can(regex("^guaranine-kairos-[a-z0-9-]+$", var.image_name)) && length(var.image_name) <= 48
    error_message = "Use guaranine-kairos-<version>-<node>-<hash>, never a shared/latest image name."
  }
}

variable "vhd_size_bytes" {
  type        = number
  description = "Exact uncompressed fixed VHD file size, including its 512-byte footer."
  validation {
    condition     = var.vhd_size_bytes > 1073741824 && floor(var.vhd_size_bytes) == var.vhd_size_bytes && var.vhd_size_bytes % 512 == 0
    error_message = "Use the actual fixed VHD length in bytes (more than 1 GiB, aligned to 512 bytes)."
  }
}

variable "vhd_sha256" {
  type        = string
  description = "SHA-256 of the private uncompressed VHD; no file contents or credentials."
  validation {
    condition     = can(regex("^[0-9a-f]{64}$", var.vhd_sha256))
    error_message = "Use the VHD's lowercase SHA-256 digest."
  }
}

variable "image_ready" {
  type        = bool
  default     = false
  description = "Explicitly set true only after VHD upload completes and its write SAS is revoked."
}

# Private Kairos VHD upload; the controller root copies the finalized disk.
resource "azurerm_resource_group" "image" {
  name     = "${var.image_name}-${substr(var.vhd_sha256, 0, 12)}-rg"
  location = var.location
  tags     = { project = "guaranine", managed_by = "terraform" }
}

resource "azurerm_managed_disk" "upload" {
  name                          = "${var.image_name}-${substr(var.vhd_sha256, 0, 12)}"
  resource_group_name           = azurerm_resource_group.image.name
  location                      = azurerm_resource_group.image.location
  storage_account_type          = "StandardSSD_LRS"
  create_option                 = "Upload"
  upload_size_bytes             = var.vhd_size_bytes
  os_type                       = "Linux"
  hyper_v_generation            = "V2"
  network_access_policy         = var.image_ready ? "DenyAll" : "AllowAll"
  public_network_access_enabled = !var.image_ready
  tags = {
    project    = "guaranine"
    managed_by = "terraform"
    vhd_sha256 = var.vhd_sha256
    contains   = "private-kairos-machine-configuration"
  }
}

output "upload_disk_id" {
  value = azurerm_managed_disk.upload.id
}

output "source_disk_id" {
  value = var.image_ready ? azurerm_managed_disk.upload.id : null
}
