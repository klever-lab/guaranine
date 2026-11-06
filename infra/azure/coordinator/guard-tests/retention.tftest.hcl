# Intentionally fails in the second run. The shared validation runner requires
# the specific prevent_destroy diagnostic for the persistent disk, and no other
# errors. This verifies Terraform's actual lifecycle behavior with no Azure.
mock_provider "azurerm" {
  source = "tests/fixtures"
}

variables {
  subscription_id      = "00000000-0000-0000-0000-000000000000"
  admin_ssh_public_key = file("tests/fixtures/operator.pub")
  talos_image_id       = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/guaranine-images-rg/providers/Microsoft.Compute/images/talos-v1-14-5-fixture"
}

run "seed_mock_state" {
  command = apply
}

run "refuse_disk_replacement" {
  command = plan
  plan_options {
    replace = [azurerm_managed_disk.coordinator_data]
  }
}
