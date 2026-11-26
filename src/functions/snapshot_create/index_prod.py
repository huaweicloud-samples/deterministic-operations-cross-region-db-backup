# -*- coding: utf-8 -*-
"""
RDS 手动备份创建函数

定时触发：为配置的 RDS 实例创建带统一前缀标记的手动备份（手动快照），
并带重试以应对「实例非 Available」或「上一次备份进行中」等瞬时故障。

设计要点（详见 docs/architecture.md）：
  - 华为云 RDS 手动备份创建 API（create_manual_backup）不支持 tags 字段，
    因此用「备份名称前缀」(SNAPSHOT_PROCESS_NAME-) 作为「本自动化管理」的标记。
  - 凭证仅经 FunctionGraph 委托（agency）经 context 注入，代码中不出现任何真实 AK/SK。
"""

import json
import os
import time
from datetime import datetime, timezone

from huaweicloudsdkcore.auth.credentials import BasicCredentials
from huaweicloudsdkcore.exceptions import exceptions

from huaweicloudsdkrds.v3 import (
    RdsClient,
    CreateManualBackupRequest,
    CreateManualBackupRequestBody,
)
from huaweicloudsdkrds.v3.region.rds_region import RdsRegion


def get_credentials(context):
    """从 FunctionGraph 委托注入的 context 获取临时凭证；context 不可用时回退到 HWC_* 环境变量。"""
    ak = sk = token = None
    if context is not None:
        try:
            ak = context.getSecurityAccessKey()
            sk = context.getSecuritySecretKey()
            token = context.getSecurityToken()
        except Exception as e:
            print(f"通过 context 获取凭证失败，尝试环境变量回退: {e}")

    if not ak or not sk:
        ak = os.environ.get("HWC_ACCESS_KEY")
        sk = os.environ.get("HWC_SECRET_ACCESS_KEY")
        token = os.environ.get("HWC_SECURITY_TOKEN") or None

    project_id = os.environ.get("HW_PROJECT_ID")
    if not ak or not sk:
        raise RuntimeError(
            "缺少访问凭证：请确认函数已绑定 FunctionGraph 委托（建议同时开启“函数执行委托”），"
            "否则 context 无法提供临时 AK/SK/Token。"
        )
    # 临时凭证必须携带 security_token
    return BasicCredentials(ak, sk, project_id).with_security_token(token)


def build_client(credentials, region):
    return (
        RdsClient.new_builder()
        .with_credentials(credentials)
        .with_region(RdsRegion.value_of(region))
        .build()
    )


def split_list(value):
    if not value:
        return []
    return [x.strip() for x in value.split(",") if x.strip()]


def resolve_targets(base, include, exclude):
    """include 非空时只备份 include 列表；exclude 仅在未提供 include 时生效。"""
    targets = base
    if include:
        targets = [i for i in base if i in include]
    elif exclude:
        targets = [i for i in targets if i not in exclude]
    return targets


def handler(event, context):
    credentials = get_credentials(context)
    region = os.environ.get("HW_REGION", "cn-north-4")
    client = build_client(credentials, region)

    prefix = os.environ.get("SNAPSHOT_PROCESS_NAME", "rds-dr")
    base = split_list(os.environ.get("INSTANCE_IDS", ""))
    include = split_list(os.environ.get("INCLUDE_DATABASES", ""))
    exclude = split_list(os.environ.get("EXCLUDE_DATABASES", ""))
    retry_count = int(os.environ.get("RETRY_COUNT", "3"))
    retry_sleep = int(os.environ.get("SECONDS_BETWEEN_RETRIES", "60"))

    targets = resolve_targets(base, include, exclude)
    print(f"待备份实例: {targets}; 区域: {region}")

    if not targets:
        return {"statusCode": 200, "body": {"message": "未配置任何目标实例，跳过", "successCount": 0, "failureCount": 0}}

    result = {"successCount": 0, "failureCount": 0, "details": {}}
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

    for inst in targets:
        name = f"{prefix}-{inst}-{ts}"
        ok = False
        last_err = None
        for attempt in range(1, retry_count + 1):
            try:
                if attempt > 1:
                    time.sleep(retry_sleep)
                body = CreateManualBackupRequestBody(
                    instance_id=inst,
                    name=name,
                    description="Automated cross-region DR manual backup",
                )
                req = CreateManualBackupRequest()
                req.body = body
                client.create_manual_backup(req)
                print(f"已创建手动备份 {name} (实例 {inst})")
                ok = True
                break
            except exceptions.ClientRequestException as e:
                # 实例非 Available / 上一次备份进行中 等瞬时故障 -> 重试
                last_err = f"{e.error_code}: {e.error_msg}"
                print(f"实例 {inst} 第 {attempt}/{retry_count} 次创建失败（{last_err}），稍后重试...")
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                print(f"实例 {inst} 第 {attempt}/{retry_count} 次创建异常: {e}")
        if ok:
            result["successCount"] += 1
            result["details"][name] = "Succeeded"
        else:
            result["failureCount"] += 1
            result["details"][name] = f"Failed: {last_err}"

    if result["failureCount"] and result["successCount"] == 0:
        raise RuntimeError(f"所有实例备份均失败（已重试 {retry_count} 次）")

    return {"statusCode": 200, "body": result}
