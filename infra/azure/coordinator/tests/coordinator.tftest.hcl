# All calls use a mock provider, including the in-memory lifecycle setup.
mock_provider "azurerm" {
  source          = "tests/fixtures"
  override_during = plan
}

variables {
  subscription_id = "00000000-0000-0000-0000-000000000000"
  image_id        = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/guaranine-images-rg/providers/Microsoft.Compute/images/kairos-v4-4-0-fixture"
}

run "default_sizing_and_os" {
  command = plan

  assert {
    condition = (
      azurerm_linux_virtual_machine.coordinator.size == "Standard_B2ls_v2" &&
      azurerm_managed_disk.coordinator_os.storage_account_type == "StandardSSD_LRS" &&
      azurerm_managed_disk.coordinator_os.create_option == "FromImage" &&
      azurerm_managed_disk.coordinator_os.image_reference_id == var.image_id &&
      azurerm_managed_disk.coordinator_os.disk_size_gb == 32 &&
      azurerm_managed_disk.coordinator_os.network_access_policy == "DenyAll" &&
      !azurerm_managed_disk.coordinator_os.public_network_access_enabled &&
      azurerm_linux_virtual_machine.coordinator.os_managed_disk_id == azurerm_managed_disk.coordinator_os.id &&
      azurerm_linux_virtual_machine.coordinator.source_image_id == null
    )
    error_message = "Defaults must remain the exact 2-vCPU / 4-GiB SKU, SSD OS disk and pinned image."
  }

  assert {
    condition = (
      azurerm_linux_virtual_machine.coordinator.disable_password_authentication &&
      !azurerm_linux_virtual_machine.coordinator.secure_boot_enabled &&
      !azurerm_linux_virtual_machine.coordinator.vtpm_enabled &&
      !azurerm_linux_virtual_machine.coordinator.allow_extension_operations &&
      length(azurerm_linux_virtual_machine.coordinator.boot_diagnostics) == 1 &&
      azurerm_linux_virtual_machine.coordinator.boot_diagnostics[0].storage_account_uri == null &&
      azurerm_linux_virtual_machine.coordinator.admin_password == null &&
      azurerm_linux_virtual_machine.coordinator.admin_username == null &&
      length(azurerm_linux_virtual_machine.coordinator.admin_ssh_key) == 0 &&
      azurerm_linux_virtual_machine.coordinator.custom_data == null
    )
    error_message = "Kairos must have no cloud-init, VM agent, extension operations, or credentials in Terraform custom_data."
  }
}

run "tailscale_only_network" {
  command = plan

  assert {
    condition = (
      length(azurerm_network_security_group.coordinator.security_rule) == 2 &&
      alltrue([for rule in azurerm_network_security_group.coordinator.security_rule :
        rule.access == "Deny" && rule.priority == 100 && rule.protocol == "*" &&
        rule.source_address_prefix == "*" && rule.source_port_range == "*" && rule.destination_port_range == "*" &&
        (rule.direction == "Inbound" ? rule.destination_address_prefix == "*" : rule.destination_address_prefix == "VirtualNetwork")
      ]) &&
      length([for rule in azurerm_network_security_group.coordinator.security_rule : rule if rule.direction == "Inbound"]) == 1
    )
    error_message = "Deny all inbound ports from every source, including VNet defaults, and deny direct VNet egress."
  }

  assert {
    condition = (
      !azurerm_subnet.coordinator.default_outbound_access_enabled &&
      !azurerm_network_interface.coordinator.ip_forwarding_enabled &&
      azurerm_public_ip.egress.sku == "Standard" &&
      azurerm_public_ip.egress.allocation_method == "Static" &&
      azurerm_subnet_network_security_group_association.coordinator.subnet_id == azurerm_subnet.coordinator.id &&
      azurerm_subnet_network_security_group_association.coordinator.network_security_group_id == azurerm_network_security_group.coordinator.id &&
      azurerm_network_interface.coordinator.ip_configuration[0].subnet_id == azurerm_subnet.coordinator.id &&
      azurerm_network_interface.coordinator.ip_configuration[0].public_ip_address_id == azurerm_public_ip.egress.id
    )
    error_message = "Attach the isolated subnet/NSG and explicit outbound IP; do not use implicit outbound access or forwarding."
  }
}

run "persistent_ssd" {
  command = plan

  assert {
    condition = (
      azurerm_managed_disk.coordinator_data.storage_account_type == "StandardSSD_LRS" &&
      azurerm_managed_disk.coordinator_data.disk_size_gb == 64 &&
      azurerm_managed_disk.coordinator_data.create_option == "Empty" &&
      azurerm_managed_disk.coordinator_data.network_access_policy == "DenyAll" &&
      !azurerm_managed_disk.coordinator_data.public_network_access_enabled &&
      azurerm_virtual_machine_data_disk_attachment.coordinator_data.managed_disk_id == azurerm_managed_disk.coordinator_data.id &&
      azurerm_virtual_machine_data_disk_attachment.coordinator_data.virtual_machine_id == azurerm_linux_virtual_machine.coordinator.id &&
      azurerm_virtual_machine_data_disk_attachment.coordinator_data.create_option == "Attach" &&
      azurerm_virtual_machine_data_disk_attachment.coordinator_data.lun == 0 &&
      azurerm_virtual_machine_data_disk_attachment.coordinator_data.caching == "None"
    )
    error_message = "Retain a separate SSD at stable LUN 0 with no public disk export or write cache."
  }
}

run "explicit_configuration" {
  command = plan

  variables {
    location           = "westeurope"
    vm_size            = "Standard_B4ls_v2"
    os_disk_size_gib   = 64
    data_disk_size_gib = 128

    image_id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/guaranine-images-rg/providers/Microsoft.Compute/images/kairos-v4-4-0-fixture-alternate"
    tags     = { cost_center = "students", project = "wrong", managed_by = "wrong" }
  }

  assert {
    condition = (
      azurerm_linux_virtual_machine.coordinator.location == "westeurope" &&
      azurerm_linux_virtual_machine.coordinator.size == "Standard_B4ls_v2" &&
      azurerm_managed_disk.coordinator_data.disk_size_gb == 128 &&
      azurerm_managed_disk.coordinator_os.image_reference_id == var.image_id &&
      azurerm_managed_disk.coordinator_os.disk_size_gb == 64 &&
      output.handoff.transport == "tailscale" &&
      azurerm_resource_group.coordinator.tags.project == "guaranine" &&
      azurerm_resource_group.coordinator.tags.managed_by == "terraform" &&
      azurerm_resource_group.coordinator.tags.cost_center == "students"
    )
    error_message = "Use exactly the requested region/SKU/image/capacity and preserve Guaranine ownership."
  }
}

run "reject_latest_image" {
  command = plan
  variables {
    image_id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/guaranine-images-rg/providers/Microsoft.Compute/galleries/kairos/images/node/versions/latest"
  }
  expect_failures = [var.image_id]
}

run "reject_small_os_disk" {
  command = plan
  variables {
    os_disk_size_gib = 16
  }
  expect_failures = [var.os_disk_size_gib]
}

run "reject_small_data_disk" {
  command = plan
  variables {
    data_disk_size_gib = 16
  }
  expect_failures = [var.data_disk_size_gib]
}

run "reject_fractional_capacity" {
  command = plan
  variables {
    data_disk_size_gib = 64.5
  }
  expect_failures = [var.data_disk_size_gib]
}

run "reject_foreign_namespace" {
  command = plan
  variables {
    name_prefix = "symphony"
  }
  expect_failures = [var.name_prefix]
}

