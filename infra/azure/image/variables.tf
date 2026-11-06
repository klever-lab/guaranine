variable "subscription_id" {
  type        = string
  description = "Explicit student subscription, selected after credit/quota/cost preflight."
  validation {
    condition     = can(regex("^[0-9a-fA-F-]{36}$", var.subscription_id))
    error_message = "Use an explicit subscription UUID."
  }
}

variable "location" {
  type    = string
  default = "eastus"
}

variable "image_name" {
  type        = string
  description = "Unique artifact identity; use a new name/state for new private image contents."
  validation {
    condition     = can(regex("^guaranine-talos-[a-z0-9-]+$", var.image_name)) && length(var.image_name) <= 48
    error_message = "Use guaranine-talos-<version>-<node>-<hash>, never a shared/latest image name."
  }
}

variable "vhd_size_bytes" {
  type        = number
  description = "Exact uncompressed fixed VHD file size, including its 512-byte footer."
  validation {
    condition     = var.vhd_size_bytes > 1073741824 && floor(var.vhd_size_bytes) == var.vhd_size_bytes && var.vhd_size_bytes % 512 == 0
    error_message = "Use the actual fixed VHD length in bytes (more than 1 GiB, aligned to 512 bytes)."
  }
}

variable "vhd_sha256" {
  type        = string
  description = "SHA-256 of the private uncompressed VHD; no file contents or credentials."
  validation {
    condition     = can(regex("^[0-9a-f]{64}$", var.vhd_sha256))
    error_message = "Use the VHD's lowercase SHA-256 digest."
  }
}

variable "image_ready" {
  type        = bool
  default     = false
  description = "Explicitly set true only after VHD upload completes and its write SAS is revoked."
}
