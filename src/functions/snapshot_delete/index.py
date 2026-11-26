# -*- coding: utf-8 -*-
"""
RDS 手动备份过期清理函数

定时触发：在【源区域】与【目的区域】分别清理早于保留期、且由本自动化创建的手动备份。
标记方式：备份名称以 SNAPSHOT_PROCESS_NAME 前缀开头即视为本自动化管理。

设计要点（详见 docs/architecture.md）：
  - 华为云手动备份创建 API 不支持 tags 字段，改用「名称前缀」标记，
    删除时按前缀 + begin_time 过滤。
  - 跨区域删除：需对目的区域单独构建 RdsClient（region + 目的 project_id 不同）；
    委托需在两个区域都授予 RDS FullAccess（XA 项目级角色）。
  - 凭证仅经 FunctionGraph 委托经 context 注入。

重要：SDK 采用懒加载（lazy import），全部 import 放在真正使用的函数内部。
原因：FunctionGraph 只从 handler 入口开始采集日志，模块级 import 阶段的
崩溃/输出无法被捕获，导致"执行失败+日志全空"的静默死锁。
"""

import os
from datetime import datetime, timedelta, timezone


def get_credentials(context):
    """从 FunctionGraph 委托注入的 context 获取临时凭证；context 不可用时回退到 HWC_* 环境变量。
    仅返回原始值，BasicCredentials 构造推迟到 build_client（避免在本函数内 import SDK）。"""
    ak = sk = token = None
    if context is not None:
        try:
            ak = context.getSecurityAccessKey()
            sk = context.getSecuritySecretKey()
            token = context.getSecurityToken()
        except Exception as e:
            print(f"通过 context 获取凭证失败，尝试环境变量回退: {e}", flush=True)
    if not ak or not sk:
        ak = os.environ.get("HWC_ACCESS_KEY")
        sk = os.environ.get("HWC_SECRET_ACCESS_KEY")
        token = os.environ.get("HWC_SECURITY_TOKEN") or None
    project_id = os.environ.get("HW_PROJECT_ID")
    if not ak or not sk:
        raise RuntimeError("缺少凭证：请为函数绑定 FunctionGraph 委托（函数执行委托）。")
    return ak, sk, token, project_id


def build_client(ak, sk, token, project_id, region):
    from huaweicloudsdkcore.auth.credentials import BasicCredentials
    from huaweicloudsdkrds.v3 import RdsClient
    from huaweicloudsdkrds.v3.region.rds_region import RdsRegion
    creds = BasicCredentials(ak, sk, project_id).with_security_token(token)
    return (
        RdsClient.new_builder()
        .with_credentials(creds)
        .with_region(RdsRegion.value_of(region))
        .build()
    )


def split_list(value):
    if not value:
        return []
    return [x.strip() for x in value.split(",") if x.strip()]


def _parse_ts(value):
    """兼容 SDK 反序列化的字符串或 datetime；解析失败返回最小时间。"""
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    s = str(value).replace("Z", "+00:00")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def list_managed_backups(client, instance_id, prefix, cutoff):
    """列出该实例下、名称带前缀、状态已完成、且早于 cutoff 的手动备份 ID。"""
    from huaweicloudsdkrds.v3 import ListBackupsRequest
    from huaweicloudsdkcore.exceptions import exceptions
    out = []
    offset = 0
    limit = 100
    while True:
        req = ListBackupsRequest()
        req.instance_id = instance_id
        req.backup_type = "manual"
        req.limit = limit
        req.offset = offset
        try:
            resp = client.list_backups(req)
        except exceptions.ClientRequestException as e:
            print(f"列出备份失败 instance={instance_id}: {e.error_code} {e.error_msg}", flush=True)
            break
        items = getattr(resp, "backups", []) or []
        if not items:
            break
        for b in items:
            name = getattr(b, "name", "") or ""
            if not name.startswith(prefix + "-"):
                continue
            if getattr(b, "status", None) != "COMPLETED":
                continue
            if _parse_ts(getattr(b, "begin_time", None)) < cutoff:
                out.append(getattr(b, "id"))
        if len(items) < limit:
            break
        offset += limit
    return out


def handler(event, context):
    print("=== HANDLER ENTRY ===", flush=True)
    try:
        ak, sk, token, primary_project = get_credentials(context)
        primary = os.environ.get("HW_REGION", "cn-north-4")
        secondary = os.environ.get("SECONDARY_REGION")
        secondary_project = os.environ.get("SECONDARY_PROJECT_ID")
        prefix = os.environ.get("SNAPSHOT_PROCESS_NAME", "rds-dr")
        retention = int(os.environ.get("RETENTION_IN_DAYS", "7"))
        instances = split_list(os.environ.get("INSTANCE_IDS", ""))
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention)

        print(f"配置: 区域={primary} 跨区={secondary} 前缀={prefix} 保留={retention}天 实例={instances}", flush=True)

        deleted = 0
        failed = 0
        # (区域, 该区域所用 project_id)
        regions = [(primary, primary_project)]
        if secondary:
            regions.append((secondary, secondary_project or primary_project))

        from huaweicloudsdkrds.v3 import DeleteManualBackupRequest
        from huaweicloudsdkcore.exceptions import exceptions

        for region, proj in regions:
            if not region:
                continue
            client = build_client(ak, sk, token, proj, region)
            for inst in instances:
                ids = list_managed_backups(client, inst, prefix, cutoff)
                for bid in ids:
                    try:
                        req = DeleteManualBackupRequest()
                        req.backup_id = bid
                        client.delete_manual_backup(req)
                        deleted += 1
                        print(f"已删除备份 {bid} (实例 {inst}, 区域 {region})", flush=True)
                    except exceptions.ClientRequestException as e:
                        failed += 1
                        print(f"删除失败 {bid}: {e.error_code} {e.error_msg}", flush=True)

        return {"statusCode": 200, "body": {"deleted": deleted, "failed": failed}}

    except Exception as e:
        print(f"=== HANDLER EXCEPTION ===", flush=True)
        print(f"  type: {type(e).__name__}", flush=True)
        print(f"  msg:  {e}", flush=True)
        import traceback
        traceback.print_exc()
        return {"statusCode": 500, "body": {"error": str(e), "error_type": type(e).__name__}}
