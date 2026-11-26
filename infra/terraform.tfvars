# ============================================================================
# 示例变量（请勿填入真实 AK/SK；凭据一律经环境变量 HW_ACCESS_KEY/HW_SECRET_KEY 注入）
# 部署前请把下方占位符替换为你的真实资源信息。
# ============================================================================

region               = "cn-north-4"
project_name         = "rds-dr-automation"

# 源区域中需要纳入跨区域 DR 的既有 RDS 实例 ID（从 RDS 控制台获取）
instance_ids         = ["<your-rds-instance-id-1>", "<your-rds-instance-id-2>"]

# 目的区域与目的区域的 project ID（每个区域独立的项目 ID）
secondary_region     = "cn-east-3"
secondary_project_id = "<your-secondary-region-project-id>"

# 备份范围过滤（留空表示管理全部 instance_ids）
include_databases    = []
exclude_databases    = []

# 调度（UTC，6 段 Cron：秒 分 时 日 月 周）
snapshot_frequency         = "0 0 2 * * *"   # 每日 02:00 UTC 创建
snapshot_delete_frequency  = "0 0 3 * * *"   # 每日 03:00 UTC 清理

snapshot_retention_in_days = 7
retry_count                = 3
seconds_between_retries    = 60
offsite_backup_keep_days   = 7

alert_email                = "2440926961@qq.com"
