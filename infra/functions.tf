# ============================================================================
# FunctionGraph 函数 + 触发器
#   - snapshot_create ：TIMER 触发，定时为 RDS 实例创建带标记的手动备份（跨区域 DR 第一环）
#   - snapshot_delete ：TIMER 触发，定时清理源/目的区域过期手动备份（跨区域 DR 收尾环）
#   - snapshot_copy   ：SMN 触发，将刚创建的手动备份经 transfer_backup 跨区域传输（跨区域 DR 接力环）
# 运行期凭证由 functiongraph 委托经 context 注入，代码中不出现任何真实 AK/SK。
# ============================================================================

# ---- 部署包上传 OBS（函数运行环境不含 huaweicloudsdk*，故 zip 自带 SDK 依赖）----
# 把 zip 内容的 md5 编入 OBS key：内容一旦变化，key 即变化 → fgs_function 的
# code_url 变化 → 函数被重新部署（否则 key 不变时 terraform 不会重新拉取代码）。
locals {
  pkg_md5 = {
    create = filemd5("${path.module}/../src/functions/snapshot_create/snapshot_create.zip")
    delete = filemd5("${path.module}/../src/functions/snapshot_delete/snapshot_delete.zip")
    copy   = filemd5("${path.module}/../src/functions/snapshot_copy/snapshot_copy.zip")
  }
}

resource "huaweicloud_obs_bucket_object" "pkg_create" {
  bucket = huaweicloud_obs_bucket.packages.bucket
  key    = "function-packages/snapshot_create-${local.pkg_md5.create}.zip"
  source = "${path.module}/../src/functions/snapshot_create/snapshot_create.zip"
  acl    = "private"
  etag   = local.pkg_md5.create
}

resource "huaweicloud_obs_bucket_object" "pkg_delete" {
  bucket = huaweicloud_obs_bucket.packages.bucket
  key    = "function-packages/snapshot_delete-${local.pkg_md5.delete}.zip"
  source = "${path.module}/../src/functions/snapshot_delete/snapshot_delete.zip"
  acl    = "private"
  etag   = local.pkg_md5.delete
}

resource "huaweicloud_obs_bucket_object" "pkg_copy" {
  bucket = huaweicloud_obs_bucket.packages.bucket
  key    = "function-packages/snapshot_copy-${local.pkg_md5.copy}.zip"
  source = "${path.module}/../src/functions/snapshot_copy/snapshot_copy.zip"
  acl    = "private"
  etag   = local.pkg_md5.copy
}

# 公共 user_data 片段：源项目 / 区域 / 实例清单 / 过滤 / 重试
# 注意：INCLUDE_DATABASES 未配置时传空串（而非 "*"）——函数代码 resolve_targets 把
#       空串视为「不过滤=全部」，把 "*" 当成真实实例 ID 匹配会算成空集（已踩坑）。
locals {
  common_user_data = {
    HW_REGION               = var.region
    HW_PROJECT_ID           = data.huaweicloud_identity_projects.current.projects[0].id
    SNAPSHOT_PROCESS_NAME   = var.project_name
    INSTANCE_IDS            = join(",", local.instance_ids)
    INCLUDE_DATABASES       = length(var.include_databases) > 0 ? join(",", var.include_databases) : ""
    EXCLUDE_DATABASES       = length(var.exclude_databases) > 0 ? join(",", var.exclude_databases) : ""
    RETRY_COUNT             = tostring(var.retry_count)
    SECONDS_BETWEEN_RETRIES = tostring(var.seconds_between_retries)
  }
}

# user_data 中不允许出现空字符串值（FunctionGraph 服务端 FSS.1009）。
# 统一在注入前过滤掉 value=="" 的键；函数代码对缺失键均有 os.environ.get(...,"") 兜底。
locals {
  env_filtered = { for k, v in local.common_user_data : k => v if v != "" }
}

# ============================================================================
# 创建函数（TIMER 定时触发）
# ============================================================================
resource "huaweicloud_fgs_function" "snapshot_create" {
  name        = "${var.project_name}-snapshot-create"
  app         = "default"
  agency      = huaweicloud_identity_agency.functiongraph.name
  handler     = "index.handler"
  runtime     = var.function_runtime
  memory_size = var.function_memory
  timeout     = var.function_timeout
  code_type   = "obs"
  code_url    = "https://${huaweicloud_obs_bucket.packages.bucket}.obs.${var.region}.myhuaweicloud.com/${huaweicloud_obs_bucket_object.pkg_create.key}"
  code_filename = "snapshot_create-${local.pkg_md5.create}.zip"
  description = "定时为 RDS 实例创建带标记的手动备份（跨区域 DR 第一环）"

  depends_on = [huaweicloud_obs_bucket_object.pkg_create]

  # 注入环境变量（已过滤空值，规避 FSS.1009）。函数凭此识别目标实例 / 区域；
  # 凭证仍仅经 FunctionGraph 委托 context 注入，不在此出现。
  user_data = jsonencode(local.env_filtered)
}

resource "huaweicloud_fgs_function_trigger" "create_timer" {
  function_urn = huaweicloud_fgs_function.snapshot_create.urn
  type         = "TIMER"
  status       = "ACTIVE"
  event_data   = jsonencode({
    name          = "create-snapshot"
    schedule_type = "Cron"
    schedule      = var.snapshot_frequency
  })

  # TIMER 触发器 ACTIVE 后 API 拒绝元数据更新；忽略 event_data/status 避免 FSS.1153（docs/09 坑 6）
  lifecycle {
    ignore_changes = [event_data, status]
  }
}

# ============================================================================
# 删除函数（TIMER 定时触发，跨源/目的两区域清理）
# ============================================================================
resource "huaweicloud_fgs_function" "snapshot_delete" {
  name        = "${var.project_name}-snapshot-delete"
  app         = "default"
  agency      = huaweicloud_identity_agency.functiongraph.name
  handler     = "index.handler"
  runtime     = var.function_runtime
  memory_size = var.function_memory
  timeout     = var.function_timeout
  code_type   = "obs"
  code_url    = "https://${huaweicloud_obs_bucket.packages.bucket}.obs.${var.region}.myhuaweicloud.com/${huaweicloud_obs_bucket_object.pkg_delete.key}"
  code_filename = "snapshot_delete-${local.pkg_md5.delete}.zip"
  description = "定时清理源/目的区域中超过保留期的、本自动化创建的手动备份"

  depends_on = [huaweicloud_obs_bucket_object.pkg_delete]

  # 删除函数额外需要目的区域信息（跨源/目的两区域清理）
  user_data = jsonencode({ for k, v in merge(local.env_filtered, {
    SECONDARY_REGION     = var.secondary_region
    SECONDARY_PROJECT_ID = var.secondary_project_id
    RETENTION_IN_DAYS    = tostring(var.snapshot_retention_in_days)
  }) : k => v if v != "" })
}

resource "huaweicloud_fgs_function_trigger" "delete_timer" {
  function_urn = huaweicloud_fgs_function.snapshot_delete.urn
  type         = "TIMER"
  status       = "ACTIVE"
  event_data   = jsonencode({
    name          = "delete-snapshot"
    schedule_type = "Cron"
    schedule      = var.snapshot_delete_frequency
  })

  lifecycle {
    ignore_changes = [event_data, status]
  }
}

# ============================================================================
# 复制函数（SMN 触发：RDS 手动备份创建成功事件 → 跨区域传输）
# ============================================================================
resource "huaweicloud_fgs_function" "snapshot_copy" {
  name        = "${var.project_name}-snapshot-copy"
  app         = "default"
  agency      = huaweicloud_identity_agency.functiongraph.name
  handler     = "index.handler"
  runtime     = var.function_runtime
  memory_size = var.function_memory
  timeout     = var.function_timeout
  code_type   = "obs"
  code_url    = "https://${huaweicloud_obs_bucket.packages.bucket}.obs.${var.region}.myhuaweicloud.com/${huaweicloud_obs_bucket_object.pkg_copy.key}"
  code_filename = "snapshot_copy-${local.pkg_md5.copy}.zip"
  description = "将刚创建的手动备份跨区域传输到目的区域（transfer_backup）"

  depends_on = [huaweicloud_obs_bucket_object.pkg_copy]

  # 复制函数需要目的区域（transfer_backup 的目标）
  user_data = jsonencode({ for k, v in merge(local.env_filtered, {
    SECONDARY_REGION = var.secondary_region
  }) : k => v if v != "" })
}

resource "huaweicloud_fgs_function_trigger" "copy_smn" {
  function_urn = huaweicloud_fgs_function.snapshot_copy.urn
  type         = "SMN"
  status       = "ACTIVE"
  event_data   = jsonencode({
    topic_urn = huaweicloud_smn_topic.rds_events.topic_urn
  })

  lifecycle {
    ignore_changes = [event_data, status]
  }
}

# ============================================================================
# 输出
# ============================================================================
output "function_create_urn" {
  description = "创建函数 URN"
  value       = huaweicloud_fgs_function.snapshot_create.urn
}

output "function_delete_urn" {
  description = "删除函数 URN"
  value       = huaweicloud_fgs_function.snapshot_delete.urn
}

output "function_copy_urn" {
  description = "复制函数 URN"
  value       = huaweicloud_fgs_function.snapshot_copy.urn
}
