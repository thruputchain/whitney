variable "region" {
  type = string
}

variable "availability_zone" {
  type = string
}

variable "ami_id" {
  type = string
}

variable "ssh_key_name" {
  type = string
}

variable "operator_cidr" {
  type = string
}

variable "n_exec" {
  type = number

  validation {
    condition     = var.n_exec >= 1
    error_message = "n_exec must be at least 1."
  }
}

variable "storage" {
  type        = string
  default     = "tmpfs"
  description = "tmpfs (RAM), ebs (an EBS data volume on db, event, indexer), or nvme (instance store, x8aedz)."

  validation {
    condition     = contains(["tmpfs", "ebs", "nvme"], var.storage)
    error_message = "storage must be \"tmpfs\", \"ebs\", or \"nvme\"."
  }
}

# The data volume of db, event, and indexer when storage = "ebs". The ranges
# and ratio limits match whitney/config.py. The validations refer to other
# variables, which needs OpenTofu >= 1.9.
variable "ebs_volume_type" {
  type    = string
  default = "gp3"

  validation {
    condition     = contains(["gp3", "io2"], var.ebs_volume_type)
    error_message = "ebs_volume_type must be \"gp3\" or \"io2\"."
  }
}

variable "ebs_volume_size_gib" {
  type    = number
  default = 200

  validation {
    condition     = floor(var.ebs_volume_size_gib) == var.ebs_volume_size_gib && var.ebs_volume_size_gib >= (var.ebs_volume_type == "io2" ? 4 : 1) && var.ebs_volume_size_gib <= 65536
    error_message = "ebs_volume_size_gib must be a whole number, 1-65536 for gp3 or 4-65536 for io2."
  }
}

variable "ebs_iops" {
  type    = number
  default = 16000

  validation {
    condition     = floor(var.ebs_iops) == var.ebs_iops && (var.ebs_volume_type == "io2" ? var.ebs_iops >= 100 && var.ebs_iops <= 256000 : var.ebs_iops >= 3000 && var.ebs_iops <= 80000)
    error_message = "ebs_iops must be a whole number, 3000-80000 for gp3 or 100-256000 for io2."
  }

  validation {
    condition     = var.ebs_volume_type != "gp3" || var.ebs_iops <= max(3000, 500 * var.ebs_volume_size_gib)
    error_message = "ebs_iops for gp3 must be <= max(3000, 500 x ebs_volume_size_gib)."
  }

  validation {
    condition     = var.ebs_volume_type != "io2" || var.ebs_iops <= 1000 * var.ebs_volume_size_gib
    error_message = "ebs_iops for io2 must be <= 1000 x ebs_volume_size_gib."
  }
}

variable "ebs_throughput_mibps" {
  type        = number
  default     = 1000
  description = "gp3 only; io2 ignores it."

  validation {
    condition     = var.ebs_volume_type != "gp3" || (floor(var.ebs_throughput_mibps) == var.ebs_throughput_mibps && var.ebs_throughput_mibps >= 125 && var.ebs_throughput_mibps <= 2000)
    error_message = "ebs_throughput_mibps must be a whole number, 125-2000, for gp3."
  }

  validation {
    condition     = var.ebs_volume_type != "gp3" || var.ebs_throughput_mibps * 4 <= var.ebs_iops
    error_message = "ebs_throughput_mibps for gp3 must be <= ebs_iops / 4 (0.25 MiB/s per IOPS)."
  }
}

variable "indexer" {
  type    = bool
  default = false
}

variable "single_host" {
  type    = bool
  default = false
}

variable "instance_types" {
  type = map(string)

  validation {
    condition     = alltrue([for k in ["runner", "exec", "db", "event", "indexer", "single"] : contains(keys(var.instance_types), k)])
    error_message = "instance_types needs keys runner, exec, db, event, indexer, single."
  }
}

variable "archive_url" {
  type = string
}

variable "archive_sha256" {
  type = string
}

variable "cluster_name" {
  type = string
}

variable "max_lifetime_minutes" {
  type        = number
  description = "Each host powers off (and terminates) this many minutes after boot."

  validation {
    condition     = var.max_lifetime_minutes > 0 && floor(var.max_lifetime_minutes) == var.max_lifetime_minutes
    error_message = "max_lifetime_minutes must be a whole number > 0."
  }
}

variable "instance_create_timeout" {
  type        = string
  default     = "6m"
  description = "aws_instance create timeout (for example 6m, 1h30m): bounds only the wait for an instance to go from pending to running. It does not bound RunInstances retries."

  validation {
    condition     = can(regex("^([0-9]+(\\.[0-9]+)?[hms])+$", var.instance_create_timeout))
    error_message = "instance_create_timeout must be a duration like 6m or 1h30m."
  }
}

variable "max_retries" {
  type        = number
  default     = 5
  description = "AWS SDK retries per API call (provider max_retries)."

  validation {
    condition     = var.max_retries >= 0 && floor(var.max_retries) == var.max_retries
    error_message = "max_retries must be a whole number >= 0."
  }
}
