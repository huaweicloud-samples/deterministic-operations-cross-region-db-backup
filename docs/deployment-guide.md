# 部署指导书（华为云）

本指导书说明如何使用 Terraform + `huaweicloud` Provider 部署「云数据库 跨区域备份容灾自动化」实践。

## 1. 前置条件

- 已注册华为云并完成实名认证，账户余额充足。
- 本地安装 Terraform ≥ 1.5（`terraform version` 验证）。
- 在「我的凭证 → 访问密钥」创建 AK/SK，并通过环境变量注入（**不要写入代码/配置**）：
```bash
export HW_ACCESS_KEY="<HW_ACCESS_KEY>"
export HW_SECRET_KEY="<HW_SECRET_KEY>"
# 临时凭证（推荐）额外导出：
export HW_SECURITY_TOKEN="<TOKEN>"
```
- 目标区域（默认 `cn-north-4` 北京四）与目的区域（如 `cn-east-3` 上海一）均已开通 云数据库 服务。
- **目的区域需先在控制台开通「备份跨区域传输」能力**（源实例所属区域授权向目的区域传输）
否则 `transfer_backup` 会报未授权（见第 10 节）。
- 已在源区域存在需要纳入 DR 的**既有 云数据库 实例**（本模板不创建实例）。

## 2. 配置变量

编辑 `infra/terraform.tfvars`（示例，占位符需替换为真实值；**不要写入真实 AK/SK**）：

```hcl
region = "cn-north-4"
project_name = "rds-dr-automation"

# 纳入跨区域 DR 的既有 RDS 实例 ID（必填，可填多个）
instance_ids = ["<your-rds-instance-id-1>", "<your-rds-instance-id-2>"]
secondary_region = "cn-east-3"
secondary_project_id = "<your-secondary-region-project-id>"

# 备份范围过滤：include 非空时仅备份该列表；否则排除 exclude 列表
include_databases = []
exclude_databases = []

# 调度（UTC，6 段 cron：秒 分 时 日 月 周）
snapshot_frequency = "0 0 2 * * *" # 每日 02:00 UTC 创建手动备份
snapshot_delete_frequency = "0 0 3 * * *" # 每日 03:00 UTC 清理过期备份
snapshot_retention_in_days = 7
retry_count = 3
seconds_between_retries = 60

offsite_backup_keep_days = 7 # 原生跨区域备份策略（自动备份副本）保留天数

alert_email = "your-email@example.com" # SMN 邮件订阅收件人（预留告警）

function_runtime = "Python3.9"
function_memory = 256
function_timeout = 300
```

> 获取 `secondary_project_id`：在目的区域控制台「我的凭证 → 项目」查看对应区域的 project ID。

## 3. 初始化与校验

```bash
cd infra
terraform init # 拉取 huaweicloud provider
terraform validate # 语法/参数校验
terraform plan # 预览资源变更
```
> `init/validate/plan` 均不会创建云资源。

## 4. 部署

```bash
terraform apply
```
按提示输入 `yes`。首次执行会创建 OBS 桶、IAM 委托、函数、触发器、SMN 主题与原生跨区域备份策略
（详见第 6 节）。

## 5. 函数代码说明

`src/functions/` 下三个 Python 文件需先打包为 zip（函数运行环境不含 `huaweicloudsdkrds`
故 zip 自带 SDK 依赖）：

```bash
cd src
python build_functions.py # 生成 snapshot_*/snapshot_*.zip
```

- `snapshot_create/index.py`：定时创建手动备份（TIMER 触发）。
- `snapshot_delete/index.py`：定时清理过期手动备份（TIMER 触发，跨源/目的两区域）。
- `snapshot_copy/index.py`：SMN 触发，将刚创建的手动备份跨区域传输。

函数运行期凭证由 `functiongraph` 委托经 `context` 注入（不进环境变量），**代码中无真实凭据**。
部署包存放于**私有** OBS 桶，仅经桶策略授权 FunctionGraph 委托读取（见第 10 节）。

## 6. 资源清单（apply 前必读）

本实践会在源区域创建以下资源：

| 资源 | 规格（显式写死） | 用途 |
|------|------------------|------|
| OBS 桶 | 标准存储，私有，约 20 MB（3 个 zip 包） | 存放函数部署包 |
| FunctionGraph ×3 | 256 MB / 300 s | 三个编排函数 |
| SMN 主题 + 订阅 | 邮件协议 | 承接云数据库 手动备份创建事件 |
| IAM 委托 / 触发器 | — | 函数委托、调度触发器 |
| **原生跨区域备份策略** | 自动备份副本存于目的区域 | 自动备份跨区域副本 |

- 固定基础设施仅 OBS 桶 + 函数 + SMN + 委托，规模小。
- 唯一数据规模相关项：原生跨区域备份策略在目的区域产生的备份存储量，取决于单个实例备份大小 ×
保留天数 × 实例数。**资源规格已显式写死在 `infra/variables.tf`，便于按实际 DB 容量在控制台复核**。

## 7. 部署后验证

1. 进入 FunctionGraph 控制台，确认 3 个函数状态为「已启用」，触发器分别为 2 个 TIMER（ACTIVE）
与 1 个 SMN（ACTIVE）。
2. 进入 云数据库 控制台，手动触发（或等待定时）`snapshot-create`，确认实例下出现
`rds-dr-automation-<实例ID>-*` 的手动备份且状态 `COMPLETED`。
3. 在源实例区域「备份管理 → 跨区域备份」确认原生策略已将自动备份复制到目的区域；
并在 云数据库 控制台手动创建「云数据库 事件订阅」指向 `smn_topic_urn`（Terraform 无对应资源，需控制台操作，见第 10 节）。
4. 确认 SMN 邮件订阅已点击确认链接（alert_email 预留告警用）。

## 8. 清理

```bash
terraform destroy
```
> `destroy` 前请确认已无需要保留的手动/跨区域备份；OBS 桶设了 `force_destroy=true` 会一并删除。

## 9. 常见问题

- **函数运行报缺 SDK 包**：本实践已将 `huaweicloudsdkrds/core` 等依赖打包进 zip，正常情况下无需额外引入；
若 FunctionGraph 运行时报缺包，检查 zip 是否包含 `huaweicloudsdkrds` 目录。
- **SMN 收不到邮件**：检查订阅是否已“确认订阅”（点击确认邮件）。
- **跨区域传输报未授权**：源区域需先在控制台开通「备份跨区域传输」并授权目的区域（见第 10 节）。

## 10. 落地已知坑（terraform + huaweicloud provider）

实跑与官方文档核对后需人工介入或规避的若干点（供复用时参考）：

- **函数部署包必须私有 + 桶策略授权，不能 public-read**：
OBS 数据面**不认 IAM/委托临时凭证**，仅认桶策略。本实践将包设为 `acl = "private"`
并加 `huaweicloud_obs_bucket_policy` 授权 `domain/<domain_id>:agency/<agency名>` 读取
`function-packages/*`（docs/09 坑 4）。若 FunctionGraph 拉取代码报 403，确认桶策略已生效；
个别账号若代码拉取走服务主体，可在桶策略追加 `op_svc_cff` 授权后重试。
- **OBS Administrator 是 AX（domain 级）角色**：agency 用 `domain_roles` 挂 `OBS Administrator`
云数据库/SMN FullAccess 是 XA（项目级），必须用 `project_role { project = ... }` 分别授源/目的两区域
（docs/09 坑 7），否则权限静默不生效。
- **云数据库 事件订阅须在控制台手动创建**：Terraform 无 云数据库 事件订阅资源。部署后需在源实例区域
「事件订阅」指向本实践的 `smn_topic_urn` 输出，否则 `snapshot-copy` 不会被自动触发（仍可手动调用验证）。
- **原生跨区域备份策略前提**：源实例须已开启自动备份；目的区域须支持跨区域备份且已在控制台开通。
- **TIMER 触发器 ACTIVE 后不可 in-place 更新**：对 `huaweicloud_fgs_function_trigger`
加 `lifecycle { ignore_changes = [event_data, status] }`（docs/09 坑 6），变更触发器需先 taint 重建。
- **改 agency 权限后必须冷启动**：热实例缓存旧临时凭证，改完权限后请「清理实例」或
`terraform apply -replace=huaweicloud_fgs_function.<name>` 重建后再测（docs/09 坑 5）。
- **改代码后函数不会自动重拉**：`code_url` 不变时函数不重拉包，须
`terraform apply -replace=huaweicloud_fgs_function.<name>` 强制重建（docs/09 部署效率）。

## 11. KMS 加密（可选，本实践默认不涉及）

手动备份的服务器端加密由 云数据库 服务侧对实例加密状态决定；跨区域传输/原生策略的副本加密在目的区域
由该区域 云数据库 加密配置决定。本自动化不显式传入 KMS 参数（传输接口无对应入参），如业务要求备份密文
请确保源/目的实例已开启磁盘加密，副本随之加密。
