mock_provider "openstack" {
  source = "./tests"
  mock_data "openstack_compute_flavor_v2" {
    defaults = {
      id    = "14c5fa3f-fdad-45c4-9cd1-14dd99c341ee"
      name  = "d2-2"
      vcpus = 1
      ram   = 2000
      disk  = 25
    }
  }
  mock_data "openstack_networking_network_v2" {
    defaults = {
      id   = "d7eaf2f8-d9d8-465b-9244-fd4736660570"
      name = "Ext-Net"
    }
  }
  mock_data "openstack_networking_secgroup_v2" {
    defaults = {
      id   = "c2a3a7a8-0b09-468d-85d7-f1a77bd90e3c"
      name = "default"
    }
  }
  mock_resource "openstack_networking_port_v2" {
    defaults = {
      port_security_enabled = true
    }
  }
}

mock_provider "aws" {}

variables {
  region       = "BHS5"
  state_bucket = "guaranine-disposable-state"
}

run "seed_mock_state" {
  command = apply

  assert {
    condition = (
      !aws_s3_bucket.state.force_destroy &&
      aws_s3_bucket_versioning.state.versioning_configuration[0].status == "Enabled" &&
      one(aws_s3_bucket_server_side_encryption_configuration.state.rule).apply_server_side_encryption_by_default[0].sse_algorithm == "AES256"
    )
    error_message = "Keep versioned, encrypted state without automatic object deletion."
  }

  assert {
    condition = (
      openstack_images_image_v2.controller.visibility == "private" &&
      openstack_images_image_v2.controller.verify_checksum &&
      openstack_images_image_v2.controller.disk_format == "qcow2" &&
      openstack_images_image_v2.controller.properties.hw_firmware_type == "uefi" &&
      openstack_images_image_v2.controller.min_ram_mb == 2000 &&
      openstack_images_image_v2.controller.min_disk_gb == 25 &&
      openstack_compute_instance_v2.controller.user_data == null &&
      openstack_compute_instance_v2.controller.metadata["ovh-monthly-instance"] == "1" &&
      openstack_compute_instance_v2.controller.flavor_id == data.openstack_compute_flavor_v2.controller.id
    )
    error_message = "Boot the exact d2-2 on monthly billing from a private UEFI image without placing private configuration in user_data."
  }

  assert {
    condition = (
      data.openstack_networking_secgroup_v2.controller.name == "default" &&
      openstack_networking_port_v2.controller.port_security_enabled &&
      openstack_networking_port_v2.controller.security_group_ids == toset([data.openstack_networking_secgroup_v2.controller.id]) &&
      openstack_compute_instance_v2.controller.network[0].port == openstack_networking_port_v2.controller.id
    )
    error_message = "The VM must use its dedicated port and OVH default group; native host rules enforce Tailscale-only access."
  }
}

run "unchanged_plan" {
  command = plan
}

run "same_image_different_path" {
  command = plan
  variables {
    image_path = abspath("same.qcow2")
  }
}

run "replace_image_keep_vm" {
  command = plan
  variables {
    image_path   = abspath("changed.qcow2")
    image_sha256 = filesha256(abspath("changed.qcow2"))
  }
  plan_options {
    replace = [openstack_images_image_v2.controller]
  }
}

run "reject_relabelled_image" {
  command = plan
  variables {
    image_path   = abspath("changed.qcow2")
    image_sha256 = filesha256(abspath("changed.qcow2"))
  }
  expect_failures = [openstack_images_image_v2.controller]
}

run "reject_changed_image_bytes" {
  command = plan
  variables {
    image_sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
  expect_failures = [openstack_images_image_v2.controller]
}
