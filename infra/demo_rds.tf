# ============================================================================
# 演示用 RDS 实例（用户要求新建一个便宜的 RDS 以便演示本跨区域备份方案）
#   - create_demo_rds=true（默认）时新建：VPC + 子网 + 安全组 + 单节点 MySQL
#   - 规格：单机 2 vCPUs | 4 GB MySQL 8.0（SSD 云盘 40GB）
#   - 新建实例 ID 经 main.tf 的 local.instance_ids 自动注入备份自动化。
#   - 销毁：terraform destroy 即可连同 VPC/子网/安全组一并回收。
# ============================================================================

resource "huaweicloud_vpc" "demo" {
  count = var.create_demo_rds ? 1 : 0
  name  = "${replace(var.project_name, "_", "-")}-demo-vpc"
  cidr  = "192.168.100.0/24"
}

resource "huaweicloud_vpc_subnet" "demo" {
  count      = var.create_demo_rds ? 1 : 0
  name       = "${replace(var.project_name, "_", "-")}-demo-subnet"
  cidr       = "192.168.100.0/24"
  gateway_ip = "192.168.100.1"
  vpc_id     = huaweicloud_vpc.demo[0].id
}

resource "huaweicloud_networking_secgroup" "demo" {
  count = var.create_demo_rds ? 1 : 0
  name  = "${replace(var.project_name, "_", "-")}-demo-sg"
}

resource "huaweicloud_rds_instance" "demo" {
  count = var.create_demo_rds ? 1 : 0
  name  = "${replace(var.project_name, "_", "-")}-demo-rds"

  # 单机 2 vCPUs | 4 GB MySQL 8.0（spec_code，非 UUID）
  flavor = "rds.mysql.large.arm2.single"

  vpc_id            = huaweicloud_vpc.demo[0].id
  subnet_id         = huaweicloud_vpc_subnet.demo[0].id
  security_group_id = huaweicloud_networking_secgroup.demo[0].id

  # 单节点（单机版）：db.type="Single" + 不配置 ha 块；engine 在此资源中固定为 MySQL
  availability_zone = ["${var.region}a"]

  db {
    password = var.rds_password
    version  = "8.0"
    type     = "MySQL"
    port     = 3306
  }

  volume {
    type = "CLOUDSSD"
    size = 40
  }

  backup_strategy {
    start_time = "03:00-04:00"
    keep_days  = var.snapshot_retention_in_days
  }

  timeouts {
    create = "30m"
    delete = "30m"
  }
}

output "demo_rds_id" {
  description = "新建演示 RDS 实例 ID（已自动注入备份自动化）"
  value       = var.create_demo_rds ? huaweicloud_rds_instance.demo[0].id : null
}

output "demo_rds_address" {
  description = "新建演示 RDS 实例内网地址"
  value       = var.create_demo_rds ? huaweicloud_rds_instance.demo[0].private_ips : null
}
