mock_provider "azurerm" {
  override_during = plan
  mock_resource "azurerm_managed_disk" {
    defaults = {
      id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/guaranine-images-rg/providers/Microsoft.Compute/disks/guaranine-kairos-fixture"
    }
  }
}

variables {
  subscription_id = "00000000-0000-0000-0000-000000000000"
  image_name      = "guaranine-kairos-fixture"
  vhd_size_bytes  = 11736711680
  vhd_sha256      = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}

run "prepare_upload" {
  command = plan
  assert {
    condition = (
      azurerm_managed_disk.upload.create_option == "Upload" &&
      azurerm_managed_disk.upload.upload_size_bytes == var.vhd_size_bytes &&
      azurerm_managed_disk.upload.hyper_v_generation == "V2" &&
      output.source_disk_id == null
    )
    error_message = "Prepare a Gen2 upload disk without handing off an incomplete upload."
  }
}

run "finalize_after_upload" {
  command = plan
  variables { image_ready = true }
  assert {
    condition = (
      output.source_disk_id == azurerm_managed_disk.upload.id &&
      azurerm_managed_disk.upload.network_access_policy == "DenyAll" &&
      !azurerm_managed_disk.upload.public_network_access_enabled
    )
    error_message = "Hand off the finalized upload disk and close its export surface."
  }
}

run "reject_secret_or_url_digest" {
  command = plan
  variables { vhd_sha256 = "https://storage.invalid/image.vhd?sig=not-a-real-sas" }
  expect_failures = [var.vhd_sha256]
}

run "reject_unaligned_vhd" {
  command = plan
  variables { vhd_size_bytes = 11736711681 }
  expect_failures = [var.vhd_size_bytes]
}

run "reject_foreign_image_namespace" {
  command = plan
  variables { image_name = "unrelated-image" }
  expect_failures = [var.image_name]
}
