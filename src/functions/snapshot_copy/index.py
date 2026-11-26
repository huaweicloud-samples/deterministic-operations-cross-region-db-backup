# -*- coding: utf-8 -*-
"""
RDS 手动备份跨区域传输函数

由 RDS 事件订阅（「手动备份创建成功」事件）经 SMN 主题触发：将刚创建的手动备份
传输（transfer_backup）到目的区域，实现跨区域 DR。

设计要点（详见 docs/architecture.md）：
  - 华为云 RDS 提供 transfer_backup（跨区域备份传输）接口，把备份传输到目的区域。
  - 华为云「复制备份」(replicate) API 仅支持同区域，跨区域必须走 transfer_backup / 跨区域备份策略。
  - 目的区域加密与参数组在「恢复」时决定；本实践不显式传入相关参数。
  - 凭证仅经 FunctionGraph 委托经 context 注入。

前置条件（控制台手动开启，Terraform 无对应资源）：
  - 在源实例所属区域开通「备份传输策略」，授权向 SECONDARY_REGION 传输；
    否则 transfer_backup 会报未授权（见 deployment-guide.md）。

重要：SDK 采用懒加载（lazy import），全部 import 放在真正使用的函数内部。
原因：FunctionGraph 只从 handler 入口开始采集日志，模块级 import 阶段的
崩溃/输出无法被捕获，导致"执行失败+日志全空"的静默死锁。
"""

import json
import os
from datetime import datetime, timezone


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
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    s = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def parse_event(event, instances):
    """尽力从 SMN 触发事件中提取 (instance_id, backup_id)。解析失败返回 (None, None)。"""
    try:
        smn = event.get("smn", {})
        raw = smn.get("message") or smn.get("Message") or ""
        msg = json.loads(raw) if raw else {}
        # 云数据库 事件消息常见结构：含 instance_id / backup_id / resource_id 等字段
        inst = msg.get("instance_id") or msg.get("resource_id") or msg.get("instanceId")
        bid = msg.get("backup_id") or msg.get("backupId") or msg.get("id")
        if inst and bid:
            return inst, bid
    except Exception as e:  # noqa: BLE001
        print(f"解析 SMN 消息失败，将回退到最新备份定位: {e}", flush=True)
    return None, None


def latest_managed_backup(client, instances, prefix):
    """回退策略：在配置实例中找名称带前缀、状态 COMPLETED 的最新一条手动备份。"""
    from huaweicloudsdkrds.v3 import ListBackupsRequest
    from huaweicloudsdkcore.exceptions import exceptions
    best = None  # (begin_time, instance_id, backup_id)
    for inst in instances:
        offset = 0
        while True:
            req = ListBackupsRequest()
            req.instance_id = inst
            req.backup_type = "manual"
            req.limit = 100
            req.offset = offset
            try:
                resp = client.list_backups(req)
            except exceptions.ClientRequestException as e:
                print(f"列出备份失败 {inst}: {e.error_code} {e.error_msg}", flush=True)
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
                bt = _parse_ts(getattr(b, "begin_time", None))
                bid = getattr(b, "id")
                if best is None or bt > best[0]:
                    best = (bt, inst, bid)
            if len(items) < 100:
                break
            offset += 100
    return (best[1], best[2]) if best else (None, None)


def handler(event, context):
    print("=== HANDLER ENTRY ===", flush=True)
    try:
        ak, sk, token, primary_project = get_credentials(context)
        primary = os.environ.get("HW_REGION", "cn-north-4")
        secondary = os.environ.get("SECONDARY_REGION")
        prefix = os.environ.get("SNAPSHOT_PROCESS_NAME", "rds-dr")
        instances = split_list(os.environ.get("INSTANCE_IDS", ""))

        if not secondary:
            return {"statusCode": 200, "body": {"transferred": False, "reason": "未配置 SECONDARY_REGION"}}

        client = build_client(ak, sk, token, primary_project, primary)

        instance_id, backup_id = parse_event(event, instances)
        if not backup_id:
            instance_id, backup_id = latest_managed_backup(client, instances, prefix)
        if not backup_id:
            print("未定位到待传输的手动备份，跳过", flush=True)
            return {"statusCode": 200, "body": {"transferred": False, "reason": "no backup identified"}}

        from huaweicloudsdkrds.v3 import TransferBackupRequest, TransferBackupRequestBody
        from huaweicloudsdkcore.exceptions import exceptions

        body = TransferBackupRequestBody()
        body.backups = [backup_id]
        body.destination = secondary
        body.instance_id = instance_id
        req = TransferBackupRequest()
        req.instance_id = instance_id
        req.body = body
        resp = client.transfer_backup(req)
        print(f"已提交跨区域传输：备份 {backup_id} 实例 {instance_id} -> {secondary}; 响应 {getattr(resp,'results',None)}", flush=True)
        return {
            "statusCode": 200,
            "body": {"transferred": True, "backup_id": backup_id, "instance_id": instance_id, "destination": secondary},
        }

    except exceptions.ClientRequestException as e:
        # 常见原因：源区域未开通向目的区域的备份传输策略 / 未授权。请在控制台配置后重试。
        print(f"=== CLIENT ERROR === code={e.error_code} msg={e.error_msg}", flush=True)
        return {"statusCode": 500, "body": {"error": f"{e.error_code}: {e.error_msg}"}}
    except Exception as e:
        print(f"=== HANDLER EXCEPTION ===", flush=True)
        print(f"  type: {type(e).__name__}", flush=True)
        print(f"  msg:  {e}", flush=True)
        import traceback
        traceback.print_exc()
        return {"statusCode": 500, "body": {"error": str(e), "error_type": type(e).__name__}}
