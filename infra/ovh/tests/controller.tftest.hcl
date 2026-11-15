mock_provider "openstack" {
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
}

variables {
  region = "BHS5"
}

run "seed_mock_state" {
  command = apply

  assert {
    condition = (
      openstack_images_image_v2.controller.visibility == "private" &&
      openstack_images_image_v2.controller.verify_checksum &&
      openstack_images_image_v2.controller.disk_format == "qcow2" &&
      openstack_images_image_v2.controller.properties.hw_firmware_type == "uefi" &&
      openstack_images_image_v2.controller.min_ram_mb == 2000 &&
      openstack_images_image_v2.controller.min_disk_gb == 25 &&
      openstack_compute_instance_v2.controller.user_data == null &&
      openstack_compute_instance_v2.controller.flavor_id == data.openstack_compute_flavor_v2.controller.id
    )
    error_message = "Boot the exact d2-2 from a private UEFI image without placing private configuration in user_data."
  }

  assert {
    condition = (
      openstack_networking_secgroup_v2.controller.delete_default_rules &&
      length(openstack_networking_secgroup_rule_v2.tailscale) == 2 &&
      alltrue([for rule in openstack_networking_secgroup_rule_v2.tailscale :
        rule.direction == "ingress" && rule.protocol == "udp" &&
        rule.port_range_min == 41641 && rule.port_range_max == 41641
      ]) &&
      openstack_networking_port_v2.controller.port_security_enabled &&
      openstack_networking_port_v2.controller.security_group_ids == toset([openstack_networking_secgroup_v2.controller.id]) &&
      openstack_compute_instance_v2.controller.network[0].port == openstack_networking_port_v2.controller.id
    )
    error_message = "The VM must use its dedicated port and firewall, exposing only encrypted Tailscale transport."
  }
}

run "unchanged_plan" {
  command = plan
}

run "replace_image_keep_vm" {
  command = plan
  plan_options {
    replace = [openstack_images_image_v2.controller]
  }
}

run "reject_changed_image_bytes" {
  command = plan
  variables {
    image_sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
  expect_failures = [openstack_images_image_v2.controller]
}
