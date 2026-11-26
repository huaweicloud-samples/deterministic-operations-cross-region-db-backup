# ============================================================================
# 变量定义
# 与规格选择直接挂钩的项显式写死，禁止用 data.xxx.ids[N] 选值。
# ============================================================================

variable "region" {
  description = "华为云源区域（如 cn-north-4 北京四、cn-east-3 上海一）"
  type        = string
  default     = "cn-north-4"
}

variable "project_name" {
  description = "项目名称，作为资源命名前缀与手动备份名称标记前缀"
  type        = string
  default     = "rds-dr-automation"
}

# ---- 既有 RDS 实例（本模板不创建 RDS 实例，仅管理其备份）----
variable "instance_ids" {
  description = "源区域中需要纳入跨区域 DR 的既有 RDS 实例 ID 列表（逗号/列表均可）。当 create_demo_rds=true 时本变量被忽略，改用新建的演示实例。"
  type        = list(string)
  default     = []
}

# ---- 演示用 RDS 实例（用户要求新建一个便宜的 RDS 以便演示本方案；默认开启）----
variable "create_demo_rds" {
  description = "true 时在本模板内新建一个单节点 MySQL 演示实例（含 VPC/子网/安全组），并把它的 ID 注入 instance_ids。false 时仅管理既有实例（var.instance_ids）。"
  type        = bool
  default     = true
}

variable "rds_password" {
  description = "演示 RDS 实例的 root 密码（8-32 位，含大小写/数字/特殊字符）。敏感变量，仅经 -var 会话注入，禁止写入 tfvars。"
  type        = string
  sensitive   = true
}

variable "secondary_region" {
  description = "目的区域（跨区域 DR 目标区域）。cn-north-4 的合法配对为 cn-south-1 / cn-east-3 / cn-southwest-2（官方跨区域备份配对表）。"
  type        = string
  default     = "cn-south-1"
}

variable "secondary_project_id" {
  description = "目的区域的 project ID（每个区域独立的项目 ID，用于跨区域删除/传输）。默认 cn-south-1 的项目 ID。"
  type        = string
  default     = "019f25fce3767ca39922b0a7e8d0de47"
}

# ---- 原生跨区域备份策略总开关（需先在控制台提交工单开通跨区域备份功能）----
variable "enable_cross_region_backup" {
  description = "是否创建 huaweicloud_rds_cross_region_backup_strategy（原生自动备份跨区域副本）。前提：已提交工单开通跨区域备份功能。默认 false，先交付源区域内自动化；开通后改为 true 再 apply 一次即可启用。"
  type        = bool
  default     = false
}

# ---- 备份范围过滤 ----
variable "include_databases" {
  description = "仅备份该列表中的实例 ID；为空表示备份全部 instance_ids"
  type        = list(string)
  default     = []
}

variable "exclude_databases" {
  description = "排除该列表中的实例 ID（仅当 include_databases 为空时生效）"
  type        = list(string)
  default     = []
}

# ---- 调度与保留 ----
variable "snapshot_frequency" {
  description = "创建手动备份的定时触发器 Cron（UTC，6 段：秒 分 时 日 月 周）。默认每日 02:00 UTC"
  type        = string
  default     = "0 0 2 * * *"
}

variable "snapshot_delete_frequency" {
  description = "清理过期手动备份的定时触发器 Cron（UTC，6 段）。默认每日 03:00 UTC"
  type        = string
  default     = "0 0 3 * * *"
}

variable "snapshot_retention_in_days" {
  description = "手动备份保留天数（超过则清理）"
  type        = number
  default     = 7
}

variable "retry_count" {
  description = "创建备份失败时的重试次数"
  type        = number
  default     = 3
}

variable "seconds_between_retries" {
  description = "两次重试之间的间隔（秒）"
  type        = number
  default     = 60
}

# ---- 原生跨区域备份策略 ----
variable "offsite_backup_keep_days" {
  description = "原生跨区域备份策略保留天数（针对自动备份的跨区域副本）"
  type        = number
  default     = 7
}

# ---- 告警 ----
variable "alert_email" {
  description = "告警接收邮箱（用于订阅 SMN 主题；本套件函数当前不主动发告警，预留）"
  type        = string
  default     = "2440926961@qq.com"
}

# ---- 函数运行时（影响依赖打包目标）----
variable "function_runtime" {
  description = "FunctionGraph 运行时；SDK 依赖已按 Python3.9 兼容打包"
  type        = string
  default     = "Python3.9"
}

variable "function_memory" {
  description = "函数内存（MB）"
  type        = number
  default     = 256
}

variable "function_timeout" {
  description = "函数超时（秒）"
  type        = number
  default     = 300
}
