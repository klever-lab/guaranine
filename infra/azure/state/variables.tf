variable "subscription_id" {
  description = "Explicit existing Azure for Students subscription UUID."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", var.subscription_id))
    error_message = "Supply an explicit Azure subscription UUID."
  }
}

variable "location" {
  description = "Region verified in preflight. The state service is independent of the coordinator."
  type        = string
  default     = "eastus"
}

variable "operator_object_id" {
  description = "Stable Entra object ID of the existing operator user; never inferred from whoever happens to plan."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", var.operator_object_id))
    error_message = "Supply the existing operator user's Entra object UUID."
  }
}

variable "storage_account_name" {
  description = "Globally unique stable name, selected once; no random names on reruns."
  type        = string

  validation {
    condition     = can(regex("^guaranine[a-z0-9]{0,15}$", var.storage_account_name))
    error_message = "Use guaranine plus up to 15 lowercase letters/digits (maximum 24 characters)."
  }
}

variable "operator_ipv4_cidrs" {
  description = "Operator's public IPv4 egress ranges for the state data plane; /24 to /30 or /32 only."
  type        = set(string)

  validation {
    condition = length(var.operator_ipv4_cidrs) > 0 && alltrue([
      for cidr in var.operator_ipv4_cidrs : can(cidrnetmask(cidr)) &&
      can(regex("/(2[4-9]|30|32)$", cidr))
    ])
    error_message = "Supply at least one narrow IPv4 CIDR (/24 through /30, or a single /32); never 0.0.0.0/0."
  }
}
