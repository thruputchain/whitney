mock_provider "aws" {}

variables {
  region               = "us-east-2"
  availability_zone    = "us-east-2a"
  ami_id               = "ami-0123456789abcdef0"
  ssh_key_name         = "whitney"
  operator_cidr        = "203.0.113.7/32"
  n_exec               = 3
  indexer              = false
  single_host          = false
  cluster_name         = "whitney-test"
  max_lifetime_minutes = 480
  archive_url          = "https://example.invalid/whitney.tar.gz"
  archive_sha256       = "0000000000000000000000000000000000000000000000000000000000000000"
  instance_types = {
    runner  = "r8a.16xlarge"
    exec    = "r8a.16xlarge"
    db      = "r8a.16xlarge"
    event   = "r8a.16xlarge"
    indexer = "r8a.16xlarge"
    single  = "m8a.48xlarge"
  }
}

run "default_n3" {
  command = apply

  assert {
    condition     = length(aws_instance.host) == 6
    error_message = "expected 6 instances for n_exec=3"
  }
  assert {
    condition     = alltrue([for r in concat([output.roles.runner, output.roles.db, output.roles.event], output.roles.execs) : r.instance_type == "r8a.16xlarge"])
    error_message = "every role must be r8a.16xlarge"
  }
  assert {
    condition     = output.roles.indexer == null
    error_message = "indexer must be null when off"
  }
  assert {
    condition     = length(output.roles.execs) == 3
    error_message = "expected 3 execs"
  }
}

run "indexer_on" {
  command = apply
  variables {
    indexer = true
  }

  assert {
    condition     = length(aws_instance.host) == 7
    error_message = "expected 7 instances with indexer"
  }
  assert {
    condition     = output.roles.indexer.instance_type == "r8a.16xlarge"
    error_message = "indexer type"
  }
}

run "default_storage_is_tmpfs" {
  command = apply

  assert {
    condition     = var.storage == "tmpfs"
    error_message = "storage must default to tmpfs"
  }
  assert {
    condition     = alltrue([for h in aws_instance.host : length(h.ebs_block_device) == 0])
    error_message = "tmpfs mode must attach no data volume"
  }
}

run "nvme_x8aedz" {
  command = apply
  variables {
    storage = "nvme"
    indexer = true
    instance_types = {
      runner  = "r8a.16xlarge"
      exec    = "r8a.16xlarge"
      db      = "x8aedz.12xlarge"
      event   = "x8aedz.12xlarge"
      indexer = "x8aedz.12xlarge"
      single  = "m8a.48xlarge"
    }
  }

  assert {
    condition     = output.roles.db.instance_type == "x8aedz.12xlarge" && output.roles.event.instance_type == "x8aedz.12xlarge" && output.roles.indexer.instance_type == "x8aedz.12xlarge"
    error_message = "db, event, and indexer must be x8aedz.12xlarge"
  }
  assert {
    condition     = alltrue([for h in aws_instance.host : length(h.ebs_block_device) == 0])
    error_message = "nvme mode must attach no EBS data volume"
  }
}

run "ebs_default_volume" {
  command = apply
  variables {
    storage = "ebs"
  }

  assert {
    condition = alltrue([for name in ["db", "event"] : length(aws_instance.host[name].ebs_block_device) == 1 && alltrue([
      for d in aws_instance.host[name].ebs_block_device :
      d.device_name == "/dev/sdf" && d.volume_type == "gp3" && d.volume_size == 200 && d.iops == 16000 && d.throughput == 1000 && d.encrypted && d.delete_on_termination
    ])])
    error_message = "db and event must get one encrypted gp3 200 GiB, 16000 IOPS, 1000 MiB/s volume at /dev/sdf, deleted on termination"
  }
  assert {
    condition     = alltrue([for name in ["runner", "exec0", "exec1", "exec2"] : length(aws_instance.host[name].ebs_block_device) == 0])
    error_message = "runner and executors must get no data volume"
  }
  assert {
    condition     = alltrue([for u in local.user_data : strcontains(u, "WHITNEY_EBS_VOLUME_TYPE='gp3'")])
    error_message = "the volume type must be rendered into user_data"
  }
}

run "ebs_indexer_io2" {
  command = apply
  variables {
    storage             = "ebs"
    indexer             = true
    ebs_volume_type     = "io2"
    ebs_volume_size_gib = 500
    ebs_iops            = 64000
  }

  assert {
    condition = alltrue([for name in ["db", "event", "indexer"] : length(aws_instance.host[name].ebs_block_device) == 1 && alltrue([
      for d in aws_instance.host[name].ebs_block_device :
      d.volume_type == "io2" && d.volume_size == 500 && d.iops == 64000 && d.encrypted && d.delete_on_termination
    ])])
    error_message = "db, event, and indexer must get one encrypted io2 500 GiB, 64000 IOPS volume"
  }
  assert {
    condition     = strcontains(local.user_data["indexer"], "WHITNEY_EBS_VOLUME_TYPE='io2'")
    error_message = "the volume type must be rendered into user_data"
  }
}

run "ebs_io2_has_no_throughput" {
  command = plan
  variables {
    storage         = "ebs"
    ebs_volume_type = "io2"
    ebs_iops        = 64000
  }

  assert {
    condition     = local.ebs_throughput == null
    error_message = "io2 takes no throughput"
  }
  assert {
    condition     = alltrue([for d in aws_instance.host["db"].ebs_block_device : d.volume_type == "io2"])
    error_message = "db must get an io2 volume"
  }
}

run "ebs_small_gp3" {
  command = apply
  variables {
    storage              = "ebs"
    ebs_volume_size_gib  = 20
    ebs_iops             = 3000
    ebs_throughput_mibps = 125
  }

  assert {
    condition = alltrue([
      for d in aws_instance.host["event"].ebs_block_device :
      d.volume_type == "gp3" && d.volume_size == 20 && d.iops == 3000 && d.throughput == 125
    ])
    error_message = "the volume must follow the ebs_* variables"
  }
}

run "bad_ebs_volume_type" {
  command = plan
  variables {
    ebs_volume_type = "gp2"
  }
  expect_failures = [var.ebs_volume_type]
}

run "bad_ebs_volume_size" {
  command = plan
  variables {
    ebs_volume_size_gib = 0
  }
  expect_failures = [var.ebs_volume_size_gib]
}

run "bad_ebs_iops" {
  command = plan
  variables {
    ebs_iops = 2999
  }
  expect_failures = [var.ebs_iops]
}

run "bad_ebs_throughput" {
  command = plan
  variables {
    ebs_throughput_mibps = 2001
  }
  expect_failures = [var.ebs_throughput_mibps]
}

run "bad_io2_volume_size" {
  command = plan
  variables {
    ebs_volume_type     = "io2"
    ebs_volume_size_gib = 3
  }
  expect_failures = [var.ebs_volume_size_gib]
}

run "bad_io2_iops_per_gib" {
  command = plan
  variables {
    ebs_volume_type     = "io2"
    ebs_volume_size_gib = 10
  }
  expect_failures = [var.ebs_iops]
}

run "bad_gp3_iops_per_gib" {
  command = plan
  variables {
    ebs_volume_size_gib = 20
  }
  expect_failures = [var.ebs_iops]
}

run "bad_gp3_throughput_per_iops" {
  command = plan
  variables {
    ebs_iops = 3000
  }
  expect_failures = [var.ebs_throughput_mibps]
}

run "gp3_baseline_on_a_small_volume" {
  command = plan
  variables {
    storage              = "ebs"
    ebs_volume_size_gib  = 1
    ebs_iops             = 3000
    ebs_throughput_mibps = 750
  }

  assert {
    condition     = alltrue([for d in aws_instance.host["db"].ebs_block_device : d.volume_size == 1 && d.iops == 3000])
    error_message = "3000 IOPS must be allowed on any gp3 size"
  }
}

run "single_host_ebs_rejected" {
  command = plan
  variables {
    storage     = "ebs"
    single_host = true
  }
  expect_failures = [aws_instance.host]
}

run "single_host" {
  command = apply
  variables {
    storage     = "tmpfs"
    single_host = true
  }

  assert {
    condition     = length(aws_instance.host) == 1
    error_message = "expected 1 instance"
  }
  assert {
    condition     = aws_instance.host["single"].instance_type == "m8a.48xlarge"
    error_message = "single host type"
  }
  assert {
    condition = alltrue([
      for r in concat([output.roles.runner, output.roles.event, output.roles.db], output.roles.execs) :
      r.private_ip == output.roles.runner.private_ip
    ])
    error_message = "all roles must share one private_ip"
  }
}

run "bad_storage" {
  command = plan
  variables {
    storage = "disk"
  }
  expect_failures = [var.storage]
}

run "user_data_multi_host" {
  command = apply

  assert {
    condition     = strcontains(local.user_data["db"], "WHITNEY_WORK_ROLES=\"db\"") && strcontains(local.user_data["db"], "WHITNEY_CONTROL_PORTS=\"11111\"")
    error_message = "db host user_data must carry one port and the db role"
  }
  assert {
    condition     = strcontains(local.user_data["exec2"], "WHITNEY_WORK_ROLES=\"exec2\"") && strcontains(local.user_data["runner"], "WHITNEY_WORK_ROLES=\"runner\"")
    error_message = "exec2 and runner user_data must carry their own role"
  }
  assert {
    condition     = strcontains(local.user_data["db"], "WHITNEY_STORAGE='tmpfs'")
    error_message = "storage mode must be rendered"
  }
}

run "user_data_over_limit" {
  command = plan
  variables {
    archive_url = join("", [for i in range(600) : sha256(tostring(i))])
  }
  expect_failures = [aws_instance.host]
}

run "user_data_single_host" {
  command = apply
  variables {
    storage     = "tmpfs"
    single_host = true
  }

  assert {
    condition     = strcontains(local.user_data["single"], "WHITNEY_WORK_ROLES=\"event db exec0 exec1 exec2\"") && strcontains(local.user_data["single"], "WHITNEY_CONTROL_PORTS=\"11111 11112 11113 11114 11115\"")
    error_message = "single host must list event,db,exec0.. with one port each"
  }
}

run "user_data_single_host_indexer" {
  command = apply
  variables {
    storage     = "tmpfs"
    single_host = true
    indexer     = true
  }

  assert {
    condition     = strcontains(local.user_data["single"], "WHITNEY_WORK_ROLES=\"event db indexer exec0 exec1 exec2\"") && strcontains(local.user_data["single"], "WHITNEY_CONTROL_PORTS=\"11111 11112 11113 11114 11115 11116\"")
    error_message = "single host with indexer must list event,db,indexer,exec0.."
  }
}

run "single_host_nvme_rejected" {
  command = plan
  variables {
    storage     = "nvme"
    single_host = true
  }
  expect_failures = [aws_instance.host]
}

run "dead_man_switch_multi_host" {
  command = apply

  assert {
    condition     = alltrue([for h in aws_instance.host : h.instance_initiated_shutdown_behavior == "terminate"])
    error_message = "every host must terminate when it shuts itself down"
  }
  assert {
    condition     = alltrue([for u in local.user_data : strcontains(u, "run shutdown -h +480 ")])
    error_message = "every host's user_data must schedule shutdown -h +480"
  }
  assert {
    condition     = alltrue([for u in local.user_data : strcontains(u, "+ 480 * 60))\" >\"$ETC/expires-at\"")])
    error_message = "every host's user_data must write an expiry 480 minutes ahead"
  }
  assert {
    condition     = alltrue([for u in local.user_data : strcontains(u, "run systemctl enable --now whitney-deadman.timer")])
    error_message = "every host's user_data must enable the dead-man timer"
  }
}

run "dead_man_switch_single_host" {
  command = apply
  variables {
    storage              = "tmpfs"
    single_host          = true
    max_lifetime_minutes = 90
  }

  assert {
    condition     = aws_instance.host["single"].instance_initiated_shutdown_behavior == "terminate"
    error_message = "the single host must terminate when it shuts itself down"
  }
  assert {
    condition     = strcontains(local.user_data["single"], "run shutdown -h +90 ")
    error_message = "single host user_data must schedule shutdown -h +90"
  }
}

run "bad_max_lifetime" {
  command = plan
  variables {
    max_lifetime_minutes = 0
  }
  expect_failures = [var.max_lifetime_minutes]
}

run "fractional_max_lifetime" {
  command = plan
  variables {
    max_lifetime_minutes = 1.5
  }
  expect_failures = [var.max_lifetime_minutes]
}

run "instance_create_timeout_default" {
  command = plan

  assert {
    condition     = var.instance_create_timeout == "6m"
    error_message = "instance_create_timeout must default to 6m"
  }
  assert {
    condition     = alltrue([for h in aws_instance.host : h.timeouts.create == "6m"])
    error_message = "every host must fail its create after instance_create_timeout"
  }
}

run "instance_create_timeout_set" {
  command = plan
  variables {
    instance_create_timeout = "1h30m"
  }

  assert {
    condition     = aws_instance.host["runner"].timeouts.create == "1h30m"
    error_message = "the create timeout must follow the variable"
  }
}

run "bad_instance_create_timeout" {
  command = plan
  variables {
    instance_create_timeout = "six minutes"
  }
  expect_failures = [var.instance_create_timeout]
}

run "max_retries_default" {
  command = plan

  assert {
    condition     = var.max_retries == 5
    error_message = "max_retries must default to 5"
  }
}

run "bad_max_retries" {
  command = plan
  variables {
    max_retries = -1
  }
  expect_failures = [var.max_retries]
}

run "metrics_ports_multi_host" {
  command = apply
  variables {
    indexer = true
  }

  assert {
    condition     = jsonencode(local.hosts["runner"].metrics_ports) == "{}" && jsonencode(local.hosts["event"].metrics_ports) == jsonencode({ event = 9101 }) && jsonencode(local.hosts["db"].metrics_ports) == jsonencode({ db = 9102 }) && jsonencode(local.hosts["indexer"].metrics_ports) == jsonencode({ indexer = 9103 })
    error_message = "runner gets no metrics port; event, db, indexer get 9101, 9102, 9103"
  }
  assert {
    condition     = alltrue([for n in ["exec0", "exec1", "exec2"] : jsonencode(local.hosts[n].metrics_ports) == jsonencode({ (n) = 9104 })])
    error_message = "each exec host gets its own exec on 9104"
  }
  assert {
    condition     = strcontains(local.user_data["db"], "WHITNEY_METRICS_PORTS=\"db=9102\"") && strcontains(local.user_data["runner"], "WHITNEY_METRICS_PORTS=\"\"") && strcontains(local.user_data["exec2"], "WHITNEY_METRICS_PORTS=\"exec2=9104\"")
    error_message = "the role -> port map must be rendered into user_data"
  }
  assert {
    condition     = alltrue([for u in local.user_data : strcontains(u, "WHITNEY_VECTOR_SHA256='aa3229c525a933e1af0a63a0cc6f6e5ce126b60c0ebacc17a7f553ba55b0bb52'") && strcontains(u, "WHITNEY_VECTOR_VERSION='0.43.0'") && strcontains(u, "/v0.43.0/vector-0.43.0-x86_64-unknown-linux-musl.tar.gz")])
    error_message = "every host must get the pinned Vector URL, version and sha256"
  }
  assert {
    condition     = strcontains(local.user_data["db"], "WHITNEY_CLUSTER='whitney-test'") && strcontains(local.user_data["db"], "WHITNEY_INSTANCE_TYPE='r8a.16xlarge'")
    error_message = "cluster name and instance type must be rendered for the metric labels"
  }
}

run "metrics_ports_single_host" {
  command = apply
  variables {
    storage     = "tmpfs"
    single_host = true
    indexer     = true
  }

  assert {
    condition     = jsonencode(local.hosts["single"].metrics_ports) == jsonencode({ event = 9101, db = 9102, indexer = 9103, exec0 = 9110, exec1 = 9111, exec2 = 9112 })
    error_message = "single host: event, db, indexer on 9101-9103 and execK on 9110 + K"
  }
  assert {
    condition     = strcontains(local.user_data["single"], "WHITNEY_METRICS_PORTS=\"db=9102 event=9101 exec0=9110 exec1=9111 exec2=9112 indexer=9103\"")
    error_message = "the single host's role -> port map must be rendered into user_data"
  }
}

run "user_data_is_gzipped_and_fits" {
  command = apply
  variables {
    storage     = "tmpfs"
    single_host = true
    indexer     = true
    n_exec      = 16
  }

  assert {
    condition     = aws_instance.host["single"].user_data_base64 == base64gzip(local.user_data["single"])
    error_message = "the instance must get the rendered host setup script, gzipped"
  }
  assert {
    condition     = length(aws_instance.host["single"].user_data_base64) * 3 / 4 < 16384
    error_message = "gzipped user data must stay under EC2's 16 KiB limit"
  }
  assert {
    condition     = length(local.user_data["single"]) > 16384
    error_message = "the rendered script is over 16 KiB: this test must still prove gzip is needed"
  }
}
