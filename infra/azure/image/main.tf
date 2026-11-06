# Private Talos VHD upload staging and image publication; no VM/server is created.
resource "azurerm_resource_group" "image" {
  name     = "guaranine-images-rg"
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
    contains   = "private-talos-machine-configuration"
  }
}

# image_ready is set only after an operator uploads the VHD and revokes its
# short-lived write SAS. Neither the SAS nor VHD contents enter Terraform.
resource "azurerm_image" "talos" {
  count               = var.image_ready ? 1 : 0
  name                = "${var.image_name}-${substr(var.vhd_sha256, 0, 12)}"
  resource_group_name = azurerm_resource_group.image.name
  location            = azurerm_resource_group.image.location
  hyper_v_generation  = "V2"
  os_disk {
    os_type         = "Linux"
    os_state        = "Generalized"
    managed_disk_id = azurerm_managed_disk.upload.id
    caching         = "ReadWrite"
    storage_type    = "StandardSSD_LRS"
  }
  tags = { project = "guaranine", managed_by = "terraform", vhd_sha256 = var.vhd_sha256 }
}
