output "talos_handoff" {
  description = "Non-secret Talos inventory. Discover the node's tailnet address after boot; Azure IPs are never cluster/API endpoints."
  value = {
    subscription_id = var.subscription_id
    resource_group  = azurerm_resource_group.coordinator.name
    vm_name         = azurerm_linux_virtual_machine.coordinator.name
    vm_id           = azurerm_linux_virtual_machine.coordinator.id
    talos_image_id  = var.talos_image_id
    data_disk_id    = azurerm_managed_disk.coordinator_data.id
    data_disk_lun   = azurerm_virtual_machine_data_disk_attachment.coordinator_data.lun
    transport       = "tailscale"
  }
}

output "egress_public_ip" {
  description = "For outbound cost/network diagnostics only, never for SSH or Kubernetes access."
  value       = azurerm_public_ip.egress.ip_address
}
