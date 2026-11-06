mock_provider "azurerm" {
  source = "tests/fixtures"
}

variables {
  subscription_id      = "00000000-0000-0000-0000-000000000000"
  admin_ssh_public_key = file("tests/fixtures/operator.pub")
  image = {
    publisher = "Canonical"
    offer     = "ubuntu-24_04-lts"
    sku       = "server"
    version   = "24.04.202610210"
  }
}

# Mocked apply seeds Terraform's in-memory state without Azure calls.
run "seed_mock_state" {
  command = apply
}

# The validation runner also checks the actual JSON plan for only no-op actions.
run "unchanged_plan" {
  command = plan
}

run "replace_vm_keep_data" {
  command = plan
  plan_options {
    replace = [azurerm_linux_virtual_machine.coordinator]
  }

  assert {
    condition = (
      azurerm_managed_disk.coordinator_data.id == run.seed_mock_state.ansible_handoff.data_disk_id &&
      azurerm_virtual_machine_data_disk_attachment.coordinator_data.managed_disk_id == run.seed_mock_state.ansible_handoff.data_disk_id
    )
    error_message = "Replacing the coordinator VM must reuse the existing persistent data disk."
  }
}
