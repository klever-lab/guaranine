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
      # The OS disk is a separate Terraform resource and survives VM replacement.
      delete_os_disk_on_deletion = false
    }
  }
}

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
  default     = "guaranine-kairos"

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

variable "image_id" {
  description = "Exact private Kairos Azure managed-image or gallery-version ID. Machine configuration is embedded locally in its VHD, never Terraform custom_data."
  type        = string

  validation {
    condition = (
      can(regex("^/subscriptions/[0-9a-fA-F-]+/resourceGroups/[^/]+/providers/Microsoft.Compute/(images/[^/]+|galleries/[^/]+/images/[^/]+/versions/[0-9]+\\.[0-9]+\\.[0-9]+)$", var.image_id)) &&
      !can(regex("(?i)(^|/)latest($|/)", var.image_id))
    )
    error_message = "Supply a private managed-image ID or explicit gallery image version, never a marketplace image, URL, or latest alias."
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
  description = "Retained SSD capacity in GiB for K3s data; only grow existing disks."
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

locals {
  tags = merge(var.tags, {
    project    = "guaranine"
    managed_by = "terraform"
    role       = "coordinator"
  })
}

resource "azurerm_resource_group" "coordinator" {
  name     = "${var.name_prefix}-coordinator-rg"
  location = var.location
  tags     = local.tags
}

# No peering, VPN gateway, or dependency on any existing Azure service.
resource "azurerm_virtual_network" "coordinator" {
  name                = "${var.name_prefix}-vnet"
  resource_group_name = azurerm_resource_group.coordinator.name
  location            = azurerm_resource_group.coordinator.location
  address_space       = ["10.80.0.0/24"]
  tags                = local.tags
}

resource "azurerm_subnet" "coordinator" {
  name                            = "coordinator"
  resource_group_name             = azurerm_resource_group.coordinator.name
  virtual_network_name            = azurerm_virtual_network.coordinator.name
  address_prefixes                = ["10.80.0.0/27"]
  default_outbound_access_enabled = false
}

resource "azurerm_network_security_group" "coordinator" {
  name                = "${var.name_prefix}-nsg"
  resource_group_name = azurerm_resource_group.coordinator.name
  location            = azurerm_resource_group.coordinator.location
  tags                = local.tags

  # Overrides Azure's default VNet and load-balancer inbound allows as well.
  security_rule {
    name                       = "deny-all-inbound"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  # Private inter-host traffic must go through the encrypted tailnet, even if
  # someone later adds a host in this VNet. Internet egress uses Azure's default
  # AllowInternetOutBound rule for package downloads and Tailscale/DERP.
  security_rule {
    name                       = "deny-vnet-outbound"
    priority                   = 100
    direction                  = "Outbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "VirtualNetwork"
  }
}

resource "azurerm_subnet_network_security_group_association" "coordinator" {
  subnet_id                 = azurerm_subnet.coordinator.id
  network_security_group_id = azurerm_network_security_group.coordinator.id
}

# Explicit outbound connectivity; no inbound service is permitted by the NSG.
# Avoid an implicit outbound IP or the recurring cost of a dedicated NAT gateway.
resource "azurerm_public_ip" "egress" {
  name                = "${var.name_prefix}-egress-ip"
  resource_group_name = azurerm_resource_group.coordinator.name
  location            = azurerm_resource_group.coordinator.location
  allocation_method   = "Static"
  sku                 = "Standard"
  ip_version          = "IPv4"
  tags                = local.tags
}

resource "azurerm_network_interface" "coordinator" {
  name                  = "${var.name_prefix}-nic"
  resource_group_name   = azurerm_resource_group.coordinator.name
  location              = azurerm_resource_group.coordinator.location
  ip_forwarding_enabled = false
  tags                  = local.tags

  ip_configuration {
    name                          = "coordinator"
    subnet_id                     = azurerm_subnet.coordinator.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.egress.id
  }

  depends_on = [azurerm_subnet_network_security_group_association.coordinator]
}

# Seed the boot disk once; Kairos owns installed state and native OS upgrades.
# Attach the prepared Gen2 disk without Azure guest provisioning or a VM agent.
resource "azurerm_managed_disk" "coordinator_os" {
  name                          = "${var.name_prefix}-os"
  resource_group_name           = azurerm_resource_group.coordinator.name
  location                      = azurerm_resource_group.coordinator.location
  storage_account_type          = "StandardSSD_LRS"
  create_option                 = "FromImage"
  image_reference_id            = var.image_id
  os_type                       = "Linux"
  hyper_v_generation            = "V2"
  disk_size_gb                  = var.os_disk_size_gib
  network_access_policy         = "DenyAll"
  public_network_access_enabled = false
  tags                          = local.tags

  lifecycle {
    # Creation metadata is not an OS upgrade mechanism. This also permits
    # adoption of Azure-created boot disks whose origin is an image snapshot.
    ignore_changes = [create_option, image_reference_id, source_resource_id]
  }
}

resource "azurerm_linux_virtual_machine" "coordinator" {
  name                            = "${var.name_prefix}-coordinator"
  resource_group_name             = azurerm_resource_group.coordinator.name
  location                        = azurerm_resource_group.coordinator.location
  size                            = var.vm_size
  disable_password_authentication = true
  network_interface_ids           = [azurerm_network_interface.coordinator.id]
  allow_extension_operations      = false
  # The pinned upstream Azure VHD uses the ordinary, unsigned boot path.
  # A custom Secure Boot trust setup needs separate qualification.
  secure_boot_enabled = false
  vtpm_enabled        = false
  os_managed_disk_id  = azurerm_managed_disk.coordinator_os.id
  tags                = local.tags

  os_disk {
    caching = "ReadWrite"
  }

  boot_diagnostics {}
}

# Independent of VM identity/image/size. Terraform must refuse deletion or
# replacement unless an operator deliberately removes this lifecycle guard.
resource "azurerm_managed_disk" "coordinator_data" {
  name                          = "${var.name_prefix}-data"
  resource_group_name           = azurerm_resource_group.coordinator.name
  location                      = azurerm_resource_group.coordinator.location
  storage_account_type          = "StandardSSD_LRS"
  create_option                 = "Empty"
  disk_size_gb                  = var.data_disk_size_gib
  network_access_policy         = "DenyAll"
  public_network_access_enabled = false
  tags                          = local.tags

  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_virtual_machine_data_disk_attachment" "coordinator_data" {
  managed_disk_id    = azurerm_managed_disk.coordinator_data.id
  virtual_machine_id = azurerm_linux_virtual_machine.coordinator.id
  create_option      = "Attach"
  lun                = 0
  caching            = "None"
}

output "handoff" {
  description = "Non-secret Kairos inventory. Discover the node's tailnet address after boot; Azure IPs are never cluster/API endpoints."
  value = {
    subscription_id = var.subscription_id
    resource_group  = azurerm_resource_group.coordinator.name
    vm_name         = azurerm_linux_virtual_machine.coordinator.name
    vm_id           = azurerm_linux_virtual_machine.coordinator.id
    image_id        = var.image_id
    os_disk_id      = azurerm_managed_disk.coordinator_os.id
    data_disk_id    = azurerm_managed_disk.coordinator_data.id
    data_disk_lun   = azurerm_virtual_machine_data_disk_attachment.coordinator_data.lun
    transport       = "tailscale"
  }
}

output "egress_public_ip" {
  description = "For outbound cost/network diagnostics only, never for SSH or Kubernetes access."
  value       = azurerm_public_ip.egress.ip_address
}
