mock_provider "openstack" {
  source = "./tests"
}
mock_provider "aws" {}

variables {
  region       = "BHS5"
  state_bucket = "guaranine-disposable-state"
}

run "seed_mock_state" {
  command = apply
}

run "refuse_vm_replacement" {
  command = plan
  plan_options {
    replace = [openstack_compute_instance_v2.controller]
  }
}
