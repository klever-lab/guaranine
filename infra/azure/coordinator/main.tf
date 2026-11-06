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

resource "azurerm_linux_virtual_machine" "coordinator" {
  name                = "${var.name_prefix}-coordinator"
  computer_name       = "${var.name_prefix}-coordinator"
  resource_group_name = azurerm_resource_group.coordinator.name
  location            = azurerm_resource_group.coordinator.location
  size                = var.vm_size
  # Azure requires this metadata; Talos has no user accounts or SSH daemon.
  admin_username                  = "talos"
  disable_password_authentication = true
  network_interface_ids           = [azurerm_network_interface.coordinator.id]
  provision_vm_agent              = false
  allow_extension_operations      = false
  # The pinned upstream Azure VHD uses the ordinary, unsigned boot path.
  # A custom Secure Boot trust setup needs separate qualification.
  secure_boot_enabled = false
  vtpm_enabled        = false
  source_image_id     = var.talos_image_id
  tags                = local.tags

  admin_ssh_key {
    username   = "talos"
    public_key = trimspace(var.admin_ssh_public_key)
  }

  os_disk {
    name                 = "${var.name_prefix}-os"
    caching              = "ReadWrite"
    storage_account_type = "StandardSSD_LRS"
    disk_size_gb         = var.os_disk_size_gib
  }
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
