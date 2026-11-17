terraform {
  required_version = "= 1.17.1"

  required_providers {
    openstack = {
      source  = "terraform-provider-openstack/openstack"
      version = "= 3.4.0"
    }
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.73.0"
    }
  }

  backend "s3" {
    key                         = "guaranine/ovh.tfstate"
    use_lockfile                = true
    encrypt                     = true
    use_path_style              = true
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    skip_s3_checksum            = true
  }
}

# Standard clouds.yaml / OS_* authentication; credentials never enter variables.
provider "openstack" {}

variable "state_bucket" {
  description = "Private OVH S3 bucket holding this root's state and lock."
  type        = string
}

variable "storage_region" {
  description = "OVH S3 region code (different from the OpenStack compute region)."
  type        = string
  default     = "bhs"
}

# This provider calls OVH's S3 API, not AWS infrastructure.
provider "aws" {
  region                      = var.storage_region
  skip_credentials_validation = true
  skip_region_validation      = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  s3_use_path_style           = true
  endpoints {
    s3 = "https://s3.${var.storage_region}.io.cloud.ovh.net"
  }
}

resource "aws_s3_bucket" "state" {
  bucket        = var.state_bucket
  force_destroy = false
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

variable "region" {
  description = "Exact region from the project's OpenStack service catalog, such as BHS5."
  type        = string
}

variable "image_path" {
  description = "Protected local Kairos QCOW2 produced by ./guaranine controlplane."
  type        = string

  validation {
    condition     = startswith(var.image_path, "/") && endswith(var.image_path, ".qcow2")
    error_message = "Supply an absolute path to the private QCOW2 outside the checkout."
  }
}

variable "image_sha256" {
  description = "SHA-256 of the exact private image reviewed before deployment."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{64}$", var.image_sha256))
    error_message = "Supply the image's lowercase SHA-256 digest."
  }
}

data "openstack_compute_flavor_v2" "controller" {
  region = var.region
  name   = "d2-2"
}

data "openstack_networking_network_v2" "public" {
  region   = var.region
  name     = "Ext-Net"
  external = true
}

resource "openstack_images_image_v2" "controller" {
  region           = var.region
  name             = "guaranine-controller-${substr(var.image_sha256, 0, 12)}"
  local_file_path  = var.image_path
  container_format = "bare"
  disk_format      = "qcow2"
  visibility       = "private"
  verify_checksum  = true
  min_disk_gb      = 25
  min_ram_mb       = 2000

  properties = {
    architecture     = "x86_64"
    hw_firmware_type = "uefi"
    guaranine_sha256 = var.image_sha256
  }

  lifecycle {
    # The exact same image can be downloaded to a different runner path.
    ignore_changes = [local_file_path]
    precondition {
      condition     = try(filesha256(var.image_path) == var.image_sha256, false)
      error_message = "The private image must exist and match its reviewed SHA-256."
    }
    postcondition {
      condition     = self.checksum == filemd5(var.image_path)
      error_message = "The supplied bytes differ from the stored seed image. Review an explicit image replacement locally; changing its hash metadata does not upload new bytes."
    }
  }
}

# This BHS5 project has no quota for custom security groups or rules, and Ext-Net
# enforces port security by default while disallowing create_port:port_security_enabled.
# Native host nftables owns ingress restrictions and direct private/LAN isolation.
data "openstack_networking_secgroup_v2" "controller" {
  region = var.region
  name   = "default"
}

resource "openstack_networking_port_v2" "controller" {
  region             = var.region
  name               = "guaranine-controller"
  network_id         = data.openstack_networking_network_v2.public.id
  security_group_ids = [data.openstack_networking_secgroup_v2.controller.id]
  admin_state_up     = true
}

# Booting this image does not start the restored K3s server.
resource "openstack_compute_instance_v2" "controller" {
  region              = var.region
  name                = "guaranine-controlplane"
  flavor_id           = data.openstack_compute_flavor_v2.controller.id
  image_id            = openstack_images_image_v2.controller.id
  config_drive        = false
  stop_before_destroy = true

  network {
    port = openstack_networking_port_v2.controller.id
  }

  metadata = {
    project                = "guaranine"
    managed_by             = "terraform"
    "ovh-monthly-instance" = "1"
  }

  lifecycle {
    prevent_destroy = true
    # image_id changes otherwise rebuild the root disk in place, bypassing
    # prevent_destroy. Kairos owns installed OS updates instead.
    ignore_changes = [image_id]
  }

}

output "handoff" {
  description = "Cloud inventory only; discover and verify the Tailscale SSH address separately."
  value = {
    instance_id = openstack_compute_instance_v2.controller.id
    region      = var.region
    flavor      = data.openstack_compute_flavor_v2.controller.name
    image_id    = openstack_compute_instance_v2.controller.image_id
    port_id     = openstack_networking_port_v2.controller.id
    transport   = "tailscale"
  }
}
