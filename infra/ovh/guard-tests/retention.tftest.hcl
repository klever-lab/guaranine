mock_provider "openstack" {}

variables {
  region = "BHS5"
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
