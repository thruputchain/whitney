locals {
  view = {
    for name, inst in aws_instance.host : name => {
      id            = inst.id
      private_ip    = inst.private_ip
      public_ip     = inst.public_ip
      instance_type = inst.instance_type
    }
  }

  single = var.single_host ? local.view["single"] : null
}

output "roles" {
  value = {
    runner  = var.single_host ? local.single : local.view["runner"]
    event   = var.single_host ? local.single : local.view["event"]
    db      = var.single_host ? local.single : local.view["db"]
    indexer = var.indexer ? (var.single_host ? local.single : local.view["indexer"]) : null
    execs   = [for n in local.exec_names : var.single_host ? local.single : local.view[n]]
  }
}

output "placement_group" {
  value = aws_placement_group.this.name
}

output "subnet_az" {
  value = aws_subnet.this.availability_zone
}
