output "upload_disk_id" {
  value = azurerm_managed_disk.upload.id
}

output "talos_image_id" {
  value = var.image_ready ? azurerm_image.talos[0].id : null
}
