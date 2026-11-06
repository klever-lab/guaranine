variable "subscription_id" {
  description = "Existing Azure for Students subscription ID, explicitly selected by the operator."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", var.subscription_id))
    error_message = "Supply an explicit Azure subscription UUID."
  }
}

variable "location" {
  description = "Azure region; availability, quota and cost must be checked before applying."
  type        = string
  default     = "eastus"
}

variable "name_prefix" {
  description = "Guaranine-only resource namespace; do not reuse another service's resource group."
  type        = string
  default     = "guaranine"

  validation {
    condition     = can(regex("^guaranine(-[a-z0-9]+)?$", var.name_prefix)) && length(var.name_prefix) <= 32
    error_message = "Use guaranine or guaranine-<lowercase alphanumeric suffix>, up to 32 characters."
  }
}

variable "vm_size" {
  description = "Exact SKU, without fallback. Default B2ls_v2 is burstable: 2 vCPUs and 4 GiB RAM."
  type        = string
  default     = "Standard_B2ls_v2"

  validation {
    condition     = startswith(var.vm_size, "Standard_")
    error_message = "Supply the exact Standard_ Azure SKU verified during preflight."
  }
}

variable "image" {
  description = "Exact cloud-init-capable Linux marketplace image; select and verify a version during preflight."
  type = object({
    publisher = string
    offer     = string
    sku       = string
    version   = string
  })

  validation {
    condition = (
      alltrue([for value in [var.image.publisher, var.image.offer, var.image.sku] : length(trimspace(value)) > 0]) &&
      can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+$", var.image.version))
    )
    error_message = "Use an explicit publisher/offer/SKU and numeric major.minor.patch version; latest is forbidden."
  }
}

variable "admin_username" {
  description = "Linux account used by the following Ansible task over Tailscale."
  type        = string
  default     = "guaranine"

  validation {
    condition     = can(regex("^[a-z][a-z0-9_-]{0,30}$", var.admin_username)) && var.admin_username != "root"
    error_message = "Use a non-root Linux account name, up to 31 characters."
  }
}

variable "admin_ssh_public_key" {
  description = "Operator's public SSH key only. Keep its private counterpart outside Terraform."
  type        = string

  validation {
    condition     = can(regex("^ssh-(rsa|ed25519) [A-Za-z0-9+/=]+( .*)?$", trimspace(var.admin_ssh_public_key)))
    error_message = "Supply an OpenSSH RSA or Ed25519 public key, never a private key."
  }
}

variable "os_disk_size_gib" {
  description = "SSD OS disk capacity in GiB; image minimum also applies. The OS disk is replaceable."
  type        = number
  default     = 32

  validation {
    condition     = var.os_disk_size_gib >= 32 && var.os_disk_size_gib <= 1024 && floor(var.os_disk_size_gib) == var.os_disk_size_gib
    error_message = "OS disk capacity must be an integer from 32 to 1024 GiB."
  }
}

variable "data_disk_size_gib" {
  description = "Retained SSD capacity in GiB for future local SQLite data; only grow existing disks."
  type        = number
  default     = 64

  validation {
    condition     = var.data_disk_size_gib >= 32 && var.data_disk_size_gib <= 1024 && floor(var.data_disk_size_gib) == var.data_disk_size_gib
    error_message = "Data disk capacity must be an integer from 32 to 1024 GiB."
  }
}

variable "tags" {
  description = "Additional cost attribution tags; ownership tags are fixed by this configuration."
  type        = map(string)
  default     = {}
}
