# deterministic-operations-cross-region-db-backup

<img src="https://img.shields.io/badge/License-MIT--0-blue.svg" alt="License">
![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)
![Huawei Cloud](https://img.shields.io/badge/Huawei%20Cloud-RDS%20%7C%20FunctionGraph%20%7C%20SMN-orange.svg)
![Build](https://img.shields.io/badge/terraform%20validate-passing-brightgreen.svg)

A native Huawei Cloud service automation solution for **manual backup creation + cross-region transfer + expiry cleanup** of cloud databases, implementing "backup-and-restore" cross-region disaster recovery (Cross-Region DR). It suits scenarios that require redundant cross-region backups of critical cloud database instances with automatic cleanup of expired backups per a retention period. This solution **does not create cloud database instances**; it only manages backups of existing instances.

## 1. Overview

A native Huawei Cloud service automation solution for **manual backup creation + cross-region transfer + expiry cleanup** of cloud databases, implementing "backup-and-restore" cross-region disaster recovery (Cross-Region DR). It suits scenarios that require redundant cross-region backups of critical cloud database instances with automatic cleanup of expired backups per a retention period. This solution **does not create cloud database instances**; it only manages backups of existing instances.

## 2. Table of Contents

- [1. Overview](#1-overview)
- [2. Table of Contents](#2-table-of-contents)
- [3. Architecture Diagram](#3-architecture-diagram)
- [4. Highlights](#4-highlights)
- [5. Cloud Services Used](#5-cloud-services-used)
- [6. Prerequisites](#6-prerequisites)
- [7. Quick Start](#7-quick-start)
- [8. Step-by-Step Deployment](#8-step-by-step-deployment)
- [9. Usage / Verification](#9-usage--verification)
- [10. Cleanup](#10-cleanup)
- [11. Detailed Explanation](#11-detailed-explanation)
- [12. Dependencies](#12-dependencies)
- [13. FAQ / Troubleshooting](#13-faq--troubleshooting)
- [14. Contributing](#14-contributing)
- [15. License](#15-license)
- [16. Contact / Maintainers](#16-contact--maintainers)

## 3. Architecture Diagram

```mermaid
flowchart TB
RDS_SRC[RDS instance (existing)] -->|snapshot-create TIMER 02:00| CREATE[Create manual backup]
RDS_SRC -->|manual backup creation event| SMN[SMN topic]
SMN -->|trigger| COPY[snapshot-copy]
COPY -->|transfer_backup| RDS_DST[Destination-region backup copy]
DELETE[snapshot-delete TIMER 03:00] -->|clean up expired| RDS_SRC
DELETE -->|cross-region cleanup| RDS_DST
STRAT[Native cross-region backup policy] -.automatic backup copy.-> RDS_DST
OBS[OBS bucket (private)] -->|bucket policy grant| CREATE & DELETE & COPY
```

See [docs/architecture.md](docs/architecture.md).

## 4. Highlights

- **Serverless automation**: Three FunctionGraph functions handle "creation / cross-region transfer / cleanup" respectively, triggered on demand.
- **Dual-channel cross-region DR**: Manual backups are transferred cross-region via `transfer_backup`, while automatic backups are backed up by the native cross-region policy — complete coverage.
- **No credentials on disk**: Runtime credentials for the functions are injected through the FunctionGraph agency via `context`; the code and documents contain no real AK/SK.
- **Minimal exposure surface**: The function deployment packages are stored in a **private** OBS bucket, readable only by the agency via a bucket policy, with no internet exposure.
- **One-command IaC**: All resources are defined uniformly by Terraform + the huaweicloud Provider; `terraform validate` has passed.

## 5. Cloud Services Used

This practice uses the following Huawei Cloud services (see `docs/deployment-guide.md` for the resource list):

| Service | Purpose |
|---|---|
| RDS (cloud database) | Source/destination region database instances and backups |
| FunctionGraph (FGS) | Three orchestration functions (create / transfer / cleanup) |
| SMN (message notification) | RDS event subscription triggers the copy function |
| OBS (object storage) | Private bucket for function deployment packages |
| IAM (Agency) | FunctionGraph agency |

## 6. Prerequisites

- Huawei Cloud real-name verification completed, with an adequate account balance.
- **Terraform >= 1.5** installed locally (verify with `terraform version`).
- Create AK/SK and provide them via environment variables (do not write them into code/config):
```bash
export HW_ACCESS_KEY="<HW_ACCESS_KEY>"
export HW_SECRET_KEY="<HW_SECRET_KEY>"
# Temporary credentials (recommended): export HW_SECURITY_TOKEN="<TOKEN>"
```
- Cloud database enabled in both source and destination regions; "backup cross-region transfer" authorization enabled in the destination region console.
- An **existing cloud database instance** to be covered by DR already present in the source region (this solution does not create instances).

## 7. Quick Start

```bash
# 1. Package the functions (bundled with huaweicloud SDK dependencies)
cd src && python build_functions.py && cd ..

# 2. Deploy the infrastructure
cd infra
terraform init
terraform validate
terraform plan
terraform apply
```

Complete steps and cleanup: see [docs/deployment-guide.md](docs/deployment-guide.md).

## 8. Step-by-Step Deployment

1. **Package the functions**: `cd src && python build_functions.py`, which generates `snapshot_*/snapshot_*.zip`.
2. **Configure variables**: edit `infra/terraform.tfvars` (see Section 2 of the deployment guide), filling in `instance_ids`, `secondary_region`, `secondary_project_id`, etc.
3. **Initialize and validate**: `terraform init` → `terraform validate` → `terraform plan`.
4. **Deploy**: `terraform apply`.
5. **Manual step**: in the source instance region console, create a "cloud database event subscription" pointing to the output `smn_topic_urn` (this resource is not managed by Terraform).

## 9. Usage / Verification

After a successful deployment:

1. In the FunctionGraph console confirm the 3 functions are enabled, with triggers 2×TIMER (ACTIVE) + 1×SMN (ACTIVE).
2. Manually or via the timer trigger `snapshot-create`, confirm in the cloud database console that `rds-dr-automation-<instance>-<ts>` appears with status `COMPLETED`.
3. After configuring the cloud database event subscription, creating a new manual backup should automatically trigger `snapshot-copy` via SMN to transfer it to the destination region.
4. Confirm the SMN email subscription confirmation link has been clicked.

Detailed verification and known pitfalls: see Sections 7 and 10 of [docs/deployment-guide.md](docs/deployment-guide.md).

After deployment, follow these steps to verify that the business functionality works before considering the delivery complete.

## 10. Cleanup

```bash
cd infra && terraform destroy
```

> Before `destroy`, confirm there are no manual/cross-region backups you still need to keep; the OBS bucket has `force_destroy=true` and will be deleted along with its contents.
> If a versioned OBS bucket reports `MalformedXML` on `destroy`, empty all versions and delete markers before deleting the bucket.

## 11. Detailed Explanation

- **Three actions**: creation (TIMER daily 02:00 UTC) / cross-region transfer (SMN-triggered) / cleanup (TIMER daily 03:00 UTC).
- **Tagging strategy**: the manual backup creation API does not support tags; the **name prefix** `SNAPSHOT_PROCESS_NAME-` is used to mark ownership, and cleanup filters by prefix + creation time to avoid deleting other backups (see [docs/architecture.md](docs/architecture.md) §5).
- **Cross-region capability boundary**: Huawei Cloud `copy backup` is same-region only; manual snapshots use `transfer_backup` for cross-region, while automatic backups use the native cross-region backup policy — the two complement each other.
- **Permission agency**: the `op_svc_cff` agency, with `OBS Administrator` (AX) and cloud database/SMN FullAccess (XA, in both source and destination regions).
- **Trigger stability**: all three triggers use `ignore_changes=[event_data,status]` to avoid PUT rejection while ACTIVE.

Configuration item descriptions are in `infra/variables.tf`; constraint satisfaction status.

## 12. Dependencies

- **Runtime dependencies**: `huaweicloudsdkrds` / `huaweicloudsdkcore` (bundled into the function zip).
- **IaC**: Terraform + `huaweicloud` Provider.
- The architecture pattern references official Huawei Cloud cloud database / FunctionGraph / SMN best practices.

## 13. FAQ / Troubleshooting

- **Function reports a missing SDK package**: check whether the zip contains the `huaweicloudsdkrds` directory; this solution bundles dependencies.
- **Cross-region transfer reports unauthorized**: the source region must enable "backup cross-region transfer" in the console and authorize the destination region.
- **SMN email not received**: after subscribing you must click the confirmation email to complete the subscription.
- **Function does not re-pull the package after a code change**: `terraform apply -replace=huaweicloud_fgs_function.<name>` to force recreation.
- **Call still returns 403 after changing agency permissions**: the warm instance caches old credentials; use the console "clear instances" or recreate the function for a cold start.

## 14. Contributing

Issues are welcome for reporting problems or improvement suggestions. Please submit feedback through the project Issue channel.

## 15. License

This practice is licensed under **MIT No Attribution**; see the repository root [`LICENSE`](LICENSE) file.

This project is open-sourced under the **Apache-2.0** license. See [LICENSE](LICENSE) (if not separately provided, this statement prevails).

## 16. Contact / Maintainers
| Item | Content |
|---|---|
| **Creator** | [dgc-git](https://github.com/dgc-git) |
| **Maintainer** | [dgc-git](https://github.com/dgc-git) |
| **Contact** | GitHub Issues / PR (see [Section 14](#14-contributing)) |
