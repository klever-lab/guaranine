terraform {
  required_version = "= 1.17.1"

  required_providers {
    ovh = {
      source  = "ovh/ovh"
      version = "= 2.25.0"
    }
  }

  backend "s3" {
    key                         = "guaranine/ovh-storage.tfstate"
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

provider "ovh" {}

variable "project_id" {
  description = "OVH Public Cloud project ID."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.project_id))
    error_message = "Use the 32-character Public Cloud project ID."
  }
}

variable "state_bucket" {
  description = "Existing Guaranine controller state bucket."
  type        = string
}

variable "storage_region" {
  description = "OVH S3 region code."
  type        = string
  default     = "bhs"
}

locals {
  books_bucket = "guaranine-books-${substr(sha256(var.project_id), 0, 12)}"
  books_arn    = "arn:aws:s3:::${ovh_cloud_project_storage.books.name}"
  state_arn    = "arn:aws:s3:::${var.state_bucket}"
  state_key    = "${local.state_arn}/guaranine/ovh.tfstate"
  state_lock   = "${local.state_key}.tflock"

  storage_admin_actions = [
    "s3:CreateBucket",
    "s3:DeleteBucket",
    "s3:ListAllMyBuckets",
    "s3:PutBucketAcl",
    "s3:PutObjectAcl",
  ]
}

resource "ovh_cloud_project_user" "storage" {
  for_each     = toset(["books-owner", "books-app", "guaranine-state"])
  service_name = var.project_id
  description  = each.key
  role_names   = ["objectstore_operator"]
}

resource "ovh_cloud_project_storage" "books" {
  service_name = var.project_id
  region_name  = upper(var.storage_region)
  name         = local.books_bucket
  owner_id     = tonumber(ovh_cloud_project_user.storage["books-owner"].id)
  hide_objects = true
  versioning   = { status = "enabled" }
  encryption   = { sse_algorithm = "AES256" }

  lifecycle {
    prevent_destroy = true
  }
}

resource "ovh_cloud_project_user_s3_policy" "books" {
  service_name = var.project_id
  user_id      = ovh_cloud_project_user.storage["books-app"].id
  policy = jsonencode({
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [local.books_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = ["${local.books_arn}/*"]
      },
      {
        Effect   = "Deny"
        Action   = concat(local.storage_admin_actions, ["s3:DeleteObject"])
        Resource = ["*"]
      },
    ]
  })
}

resource "ovh_cloud_project_user_s3_policy" "state" {
  service_name = var.project_id
  user_id      = ovh_cloud_project_user.storage["guaranine-state"].id
  policy = jsonencode({
    Statement = [
      {
        Effect   = "Allow"
        Resource = [local.state_arn]
        # The controller root manages the bucket, its encryption and its versioning.
        Action = [
          "s3:ListBucket",
          "s3:GetBucketAcl",
          "s3:GetBucketCORS",
          "s3:GetBucketLocation",
          "s3:GetBucketLogging",
          "s3:GetBucketObjectLockConfiguration",
          "s3:GetBucketTagging",
          "s3:GetBucketVersioning",
          "s3:GetBucketWebsite",
          "s3:GetEncryptionConfiguration",
          "s3:GetLifecycleConfiguration",
          "s3:GetReplicationConfiguration",
          "s3:PutBucketVersioning",
          "s3:PutEncryptionConfiguration",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = [local.state_key, local.state_lock]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:DeleteObject"]
        Resource = [local.state_lock]
      },
      {
        Effect   = "Deny"
        Action   = ["s3:DeleteObject"]
        Resource = [local.state_key]
      },
      {
        Effect   = "Deny"
        Action   = local.storage_admin_actions
        Resource = ["*"]
      },
    ]
  })
}

resource "ovh_cloud_project_user_s3_credential" "storage" {
  for_each     = toset(["books-app", "guaranine-state"])
  service_name = var.project_id
  user_id      = ovh_cloud_project_user.storage[each.key].id

  depends_on = [ovh_cloud_project_user_s3_policy.books, ovh_cloud_project_user_s3_policy.state]
}

output "books_credentials" {
  description = "Private JSON inputs for self-hosted Books deployments."
  sensitive   = true
  value = jsonencode({
    bucket     = ovh_cloud_project_storage.books.name
    region     = var.storage_region
    access_key = ovh_cloud_project_user_s3_credential.storage["books-app"].access_key_id
    secret_key = ovh_cloud_project_user_s3_credential.storage["books-app"].secret_access_key
  })
}

output "state_credentials" {
  description = "Private AWS shared credentials for the OVH controller Terraform backend."
  sensitive   = true
  value       = <<-EOT
    [default]
    aws_access_key_id = ${ovh_cloud_project_user_s3_credential.storage["guaranine-state"].access_key_id}
    aws_secret_access_key = ${ovh_cloud_project_user_s3_credential.storage["guaranine-state"].secret_access_key}
  EOT
}
