mock_provider "azurerm" {}

variables {
  subscription_id = "00000000-0000-0000-0000-000000000000"
  image_name      = "guaranine-kairos-fixture"
  vhd_size_bytes  = 11736711680
  vhd_sha256      = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}

run "seed_mock_state" { command = apply }
run "unchanged_plan" { command = plan }
