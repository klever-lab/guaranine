mock_provider "azurerm" {
  source = "tests/fixtures"
}

variables {
  subscription_id      = "00000000-0000-0000-0000-000000000000"
  operator_object_id   = "11111111-1111-1111-1111-111111111111"
  storage_account_name = "guaraninemockstate"
  operator_ipv4_cidrs  = ["203.0.113.10/32"]
}

run "seed_mock_state" {
  command = apply
}

# The validation runner requires no resource or output changes in this plan.
run "unchanged_plan" {
  command = plan
}
