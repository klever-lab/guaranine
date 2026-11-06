# AzureRM validates ARM IDs even when its APIs are mocked. Supply stable,
# schema-correct IDs; never replace the resources themselves with overrides.
mock_resource "azurerm_subnet" {
  defaults = {
    id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/guaranine-coordinator-rg/providers/Microsoft.Network/virtualNetworks/guaranine-vnet/subnets/coordinator"
  }
}

mock_resource "azurerm_network_security_group" {
  defaults = {
    id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/guaranine-coordinator-rg/providers/Microsoft.Network/networkSecurityGroups/guaranine-nsg"
  }
}

mock_resource "azurerm_public_ip" {
  defaults = {
    id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/guaranine-coordinator-rg/providers/Microsoft.Network/publicIPAddresses/guaranine-egress-ip"
  }
}

mock_resource "azurerm_network_interface" {
  defaults = {
    id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/guaranine-coordinator-rg/providers/Microsoft.Network/networkInterfaces/guaranine-nic"
  }
}

mock_resource "azurerm_linux_virtual_machine" {
  defaults = {
    id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/guaranine-coordinator-rg/providers/Microsoft.Compute/virtualMachines/guaranine-coordinator"
  }
}

mock_resource "azurerm_managed_disk" {
  defaults = {
    id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/guaranine-coordinator-rg/providers/Microsoft.Compute/disks/guaranine-data"
  }
}
