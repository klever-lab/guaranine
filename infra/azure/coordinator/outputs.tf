output "ansible_handoff" {
  description = "Non-secret inventory metadata. Discover the Tailscale endpoint after enrollment; neither Azure IP is an SSH inventory address."
  value = {
    subscription_id = var.subscription_id
    resource_group  = azurerm_resource_group.coordinator.name
    vm_name         = azurerm_linux_virtual_machine.coordinator.name
    vm_id           = azurerm_linux_virtual_machine.coordinator.id
    admin_username  = var.admin_username
    data_disk_id    = azurerm_managed_disk.coordinator_data.id
    data_disk_lun   = azurerm_virtual_machine_data_disk_attachment.coordinator_data.lun
    transport       = "tailscale"
  }
}

output "egress_public_ip" {
  description = "For outbound cost/network diagnostics only, never for SSH or Kubernetes access."
  value       = azurerm_public_ip.egress.ip_address
}
