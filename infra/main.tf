# ============================================================================
# 华为云 RDS 跨区域灾备（备份-恢复型 DR）核心基础设施
#   组件对应：FunctionGraph 编排 + OBS 函数包 + IAM 委托（Agency）+ RDS 跨区域备份策略 + SMN 主题
#   本文件负责：源项目信息、函数包 OBS 桶、FunctionGraph 委托、原生跨区域备份策略、SMN 主题
# ============================================================================

# 源区域项目信息（用于 user_data 注入 HW_PROJECT_ID；函数运行期按区域构造客户端）
data "huaweicloud_identity_projects" "current" {
  name = var.region
}

# 账户 domain id：用于 OBS 桶策略 Principal（格式 domain/<domain_id>:agency/<name>）
# 注意：huaweicloud_account.current.id 实为 domain_id（非 project_id），详见 docs/09 坑 7
data "huaweicloud_account" "current" {}

# 目标实例清单：create_demo_rds=true 时取新建演示实例 ID；否则用 var.instance_ids。
# 用 [*] 兼容 count=0（不新建时返回空列表，不产生索引越界）。
locals {
  instance_ids = var.create_demo_rds ? huaweicloud_rds_instance.demo[*].id : var.instance_ids
}

# ============================================================================
# 1. OBS 桶：存放 FunctionGraph 部署包（zip）
#    桶名全局唯一且须 DNS 合规（不含下划线），拼接项目 ID 保证唯一
# ============================================================================
resource "huaweicloud_obs_bucket" "packages" {
  bucket        = "${replace(var.project_name, "_", "-")}-func-pkgs-${data.huaweicloud_identity_projects.current.projects[0].id}"
  acl           = "private"
  force_destroy = true

  tags = {
    Project = var.project_name
  }
}

# ============================================================================
# 1b. OBS 桶策略：函数部署包仅允许 FunctionGraph 委托读取（私有，杜绝公网暴露）
#     OBS 数据面不认 IAM/委托临时凭证，必须经桶策略授权（docs/09 坑 4）。
#     Principal = domain/<domain_id>:agency/<agency名>；仅放行 function-packages/* 的 GetObject。
# ============================================================================
# TODO: OBS 桶策略 Principal 格式待修正（"Policy has invalid principal"）
#       当前函数包为 private 但 FunctionGraph 通过 agency 仍可读取（OBS 数据面特殊行为）
# resource "huaweicloud_obs_bucket_policy" "packages" {
#   bucket = huaweicloud_obs_bucket.packages.bucket
#   policy = jsonencode({
#     Version = "2012-10-17"
#     Statement = [
#       {
#         Sid       = "AllowFgsAgencyReadPackages"
#         Effect    = "Allow"
#         Principal = "domain/${data.huaweicloud_account.current.id}:agency/${huaweicloud_identity_agency.functiongraph.name}"
#         Action    = ["GetObject"]
#         Resource  = ["${huaweicloud_obs_bucket.packages.bucket}/function-packages/*"]
#       }
#     ]
#   })
# }

# ============================================================================
# 2. IAM 委托：FunctionGraph 以此委托的临时凭证调用 RDS / SMN
#    - delegated_service_name 必须为 op_svc_cff（FunctionGraph 的委托服务名）
#    - RDS FullAccess / SMN FullAccess 为 XA（项目级）角色，须按区域分别授权；
#      源区域与目的区域都要授权，函数才能在两个区域都操作 RDS（见 docs/09 坑 7）
# ============================================================================
resource "huaweicloud_identity_agency" "functiongraph" {
  name                  = "${var.project_name}-fgs-agency"
  description           = "Agency for FunctionGraph to call RDS (cross-region) and SMN"
  duration              = "FOREVER"
  delegated_service_name = "op_svc_cff"

  # OBS Administrator 为 AX（domain 级）角色：OBS 数据面不认 IAM/委托临时凭证，
  # 必须配合下方桶策略才能经委托读取函数部署包（docs/09 坑 4 / 坑 7）。
  domain_roles = ["OBS Administrator"]

  project_role {
    project = var.region
    roles   = ["RDS FullAccess", "SMN FullAccess"]
  }

  project_role {
    project = var.secondary_region
    roles   = ["RDS FullAccess", "SMN FullAccess"]
  }
}

# ============================================================================
# 3. 原生跨区域备份策略：将源实例的【自动备份】自动复制到目的区域，作为跨区域 DR 的底层保障。
#    每个纳入管理的源实例各建一条策略（for_each）。
#    前提：源实例已开启自动备份；目的区域支持跨区域备份（控制台先开通）。
# ============================================================================
resource "huaweicloud_rds_cross_region_backup_strategy" "this" {
  for_each               = var.enable_cross_region_backup ? toset(local.instance_ids) : toset([])
  instance_id            = each.key
  backup_type            = "auto"
  destination_region     = var.secondary_region
  destination_project_id = var.secondary_project_id
  keep_days              = var.offsite_backup_keep_days
}

# ============================================================================
# 4. SMN 主题：承接 RDS「手动备份创建成功」事件订阅 → 触发 copy 函数
#    （RDS 事件订阅需在控制台手动创建，指向本主题；Terraform 无对应资源）
# ============================================================================
resource "huaweicloud_smn_topic" "rds_events" {
  name         = "${replace(var.project_name, "_", "-")}-rds-events"
  display_name = "${var.project_name} rds events"
}

resource "huaweicloud_smn_subscription" "alert_email" {
  topic_urn = huaweicloud_smn_topic.rds_events.topic_urn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ============================================================================
# 输出
# ============================================================================
output "obs_bucket_name" {
  description = "函数部署包 OBS 桶名"
  value       = huaweicloud_obs_bucket.packages.bucket
}

output "functiongraph_agency_name" {
  description = "FunctionGraph 委托名"
  value       = huaweicloud_identity_agency.functiongraph.name
}

output "smn_topic_urn" {
  description = "RDS 事件 SMN 主题 URN（用于在控制台创建 RDS 事件订阅）"
  value       = huaweicloud_smn_topic.rds_events.topic_urn
}

output "offsite_policy_instance_ids" {
  description = "已开启原生跨区域备份策略的实例 ID"
  value       = [for k in keys(huaweicloud_rds_cross_region_backup_strategy.this) : k]
}
