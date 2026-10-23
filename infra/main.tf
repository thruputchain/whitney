locals {
  exec_names = [for i in range(var.n_exec) : "exec${i}"]

  # Multi-host layout: one host per role.
  multi_hosts = merge(
    {
      runner = var.instance_types["runner"]
      event  = var.instance_types["event"]
      db     = var.instance_types["db"]
    },
    var.indexer ? { indexer = var.instance_types["indexer"] } : {},
    { for n in local.exec_names : n => var.instance_types["exec"] },
  )

  # Single-host work roles, in the same order as Task 8's single_host_roles.
  single_roles = concat(["event", "db"], var.indexer ? ["indexer"] : [], local.exec_names)

  # Daemon metrics ports, the same as whitney/metrics.py (roles.json). Each
  # host's Vector scrapes the roles on it. Multi-host: every exec has its own
  # host and port 9104. Single host: execK on 9110 + K.
  metrics_ports = {
    event   = 9101
    db      = 9102
    indexer = 9103
  }
  exec_metrics_port        = 9104
  single_exec_metrics_base = 9110

  # Vector, pinned like the archive: host setup checks the SHA-256.
  vector = {
    version = "0.43.0"
    url     = "https://github.com/vectordotdev/vector/releases/download/v0.43.0/vector-0.43.0-x86_64-unknown-linux-musl.tar.gz"
    sha256  = "aa3229c525a933e1af0a63a0cc6f6e5ce126b60c0ebacc17a7f553ba55b0bb52"
  }

  # Roles that keep data. In ebs mode only these get the data volume.
  data_roles = ["db", "event", "indexer"]
  # Only gp3 takes a throughput setting.
  ebs_throughput = var.ebs_volume_type == "gp3" ? var.ebs_throughput_mibps : null

  hosts = var.single_host ? {
    single = {
      type          = var.instance_types["single"]
      control_ports = [for i in range(length(local.single_roles)) : 11111 + i]
      work_roles    = local.single_roles
      data_volume   = false
      metrics_ports = { for r in local.single_roles : r => try(local.metrics_ports[r], local.single_exec_metrics_base + index(local.exec_names, r)) }
    }
    } : {
    for name, type in local.multi_hosts : name => {
      type          = type
      control_ports = [11111]
      work_roles    = [name]
      data_volume   = var.storage == "ebs" && contains(local.data_roles, name)
      # The runner host runs no daemon.
      metrics_ports = { for r in [name] : r => try(local.metrics_ports[r], local.exec_metrics_port) if r != "runner" }
    }
  }

  tags = { whitney = var.cluster_name }
}

# The host setup script of each host, rendered. Tests read it here; the
# instances get it gzipped.
locals {
  user_data = { for name, h in local.hosts : name => templatefile("${path.module}/host-setup.sh.tftpl", {
    role                 = name
    storage              = var.storage
    ebs_volume_type      = var.ebs_volume_type
    archive_url          = var.archive_url
    archive_sha256       = var.archive_sha256
    control_ports        = h.control_ports
    work_roles           = h.work_roles
    max_lifetime_minutes = var.max_lifetime_minutes
    cluster_name         = var.cluster_name
    instance_type        = h.type
    metrics_ports        = h.metrics_ports
    vector_version       = local.vector.version
    vector_url           = local.vector.url
    vector_sha256        = local.vector.sha256
  }) }
}

resource "aws_vpc" "this" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = merge(local.tags, { Name = "${var.cluster_name}-vpc" })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(local.tags, { Name = "${var.cluster_name}-igw" })
}

resource "aws_subnet" "this" {
  vpc_id                  = aws_vpc.this.id
  cidr_block              = "10.42.1.0/24"
  availability_zone       = var.availability_zone
  map_public_ip_on_launch = true
  tags                    = merge(local.tags, { Name = "${var.cluster_name}-subnet" })
}

resource "aws_route_table" "this" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = merge(local.tags, { Name = "${var.cluster_name}-rt" })
}

resource "aws_route_table_association" "this" {
  subnet_id      = aws_subnet.this.id
  route_table_id = aws_route_table.this.id
}

resource "aws_security_group" "this" {
  name        = "${var.cluster_name}-sg"
  description = "whitney cluster: SSH from operator, all traffic within the group"
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "SSH from operator"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.operator_cidr]
  }

  ingress {
    description = "All traffic within the group"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    self        = true
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.tags, { Name = "${var.cluster_name}-sg" })
}

resource "aws_placement_group" "this" {
  name     = "${var.cluster_name}-pg"
  strategy = "cluster"
  tags     = merge(local.tags, { Name = "${var.cluster_name}-pg" })
}

resource "aws_instance" "host" {
  for_each = local.hosts

  ami                         = var.ami_id
  instance_type               = each.value.type
  key_name                    = var.ssh_key_name
  subnet_id                   = aws_subnet.this.id
  availability_zone           = var.availability_zone
  vpc_security_group_ids      = [aws_security_group.this.id]
  placement_group             = aws_placement_group.this.name
  associate_public_ip_address = true

  # Dead-man switch: host setup schedules a power-off after max_lifetime_minutes,
  # and a power-off from inside the host terminates it.
  instance_initiated_shutdown_behavior = "terminate"

  # gzip: the rendered script is over EC2's 16 KiB user data limit, and
  # cloud-init unpacks gzip user data on its own.
  user_data_base64            = base64gzip(local.user_data[each.key])
  user_data_replace_on_change = true

  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 100
  }

  # storage = "ebs": one data volume on db, event, and indexer. Inline, so
  # instance termination (destroy or the dead-man switch) deletes it too.
  # Host setup finds it as the one unpartitioned EBS NVMe disk besides root.
  dynamic "ebs_block_device" {
    for_each = each.value.data_volume ? [1] : []
    content {
      device_name           = "/dev/sdf"
      volume_type           = var.ebs_volume_type
      volume_size           = var.ebs_volume_size_gib
      iops                  = var.ebs_iops
      throughput            = local.ebs_throughput
      encrypted             = true
      delete_on_termination = true
      tags                  = merge(local.tags, { Name = "${var.cluster_name}-${each.key}-data" })
    }
  }

  tags = merge(local.tags, { Name = "${var.cluster_name}-${each.key}" })

  # Bounds the wait for an instance to reach running after RunInstances
  # worked. It does not bound the SDK retries of InsufficientInstanceCapacity:
  # var.max_retries and whitney's apply deadline (--apply-timeout) do that.
  timeouts {
    create = var.instance_create_timeout
  }

  lifecycle {
    precondition {
      condition     = !var.single_host || var.storage == "tmpfs"
      error_message = "single_host requires storage = \"tmpfs\"."
    }
    # EC2 limits user data to 16 KiB after base64 decoding. Fail at plan
    # time, before anything is created.
    precondition {
      condition     = length(base64gzip(local.user_data[each.key])) * 3 / 4 <= 16384
      error_message = "gzipped user data for ${each.key} is over EC2's 16 KiB limit."
    }
  }
}
