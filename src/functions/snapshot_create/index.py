# -*- coding: utf-8 -*-
# [v9] 生产版 - 修复备份名称长度问题

import json
import os
import time
from datetime import datetime, timezone


def get_credentials(context):
    from huaweicloudsdkcore.auth.credentials import BasicCredentials
    ak = sk = token = None
    if context is not None:
        try:
            ak = context.getSecurityAccessKey()
            sk = context.getSecuritySecretKey()
            token = context.getSecurityToken()
        except Exception as e:
            print(f"context 凭证获取失败: {e}", flush=True)
    if not ak or not sk:
        ak = os.environ.get("HWC_ACCESS_KEY")
        sk = os.environ.get("HWC_SECRET_ACCESS_KEY")
        token = os.environ.get("HWC_SECURITY_TOKEN") or None
    project_id = os.environ.get("HW_PROJECT_ID")
    if not ak or not sk:
        raise RuntimeError("缺少访问凭证")
    return BasicCredentials(ak, sk, project_id).with_security_token(token)


def build_client(credentials, region):
    from huaweicloudsdkrds.v3 import RdsClient
    from huaweicloudsdkrds.v3.region.rds_region import RdsRegion
    return RdsClient.new_builder().with_credentials(credentials).with_region(RdsRegion.value_of(region)).build()


def split_list(value):
    if not value:
        return []
    return [x.strip() for x in value.split(",") if x.strip()]


def resolve_targets(base, include, exclude):
    targets = base
    if include:
        targets = [i for i in base if i in include]
    elif exclude:
        targets = [i for i in targets if i not in exclude]
    return targets


def handler(event, context):
    print("=== HANDLER ENTRY ===", flush=True)

    try:
        # Step 1: 凭证
        print("=== STEP 1: get_credentials ===", flush=True)
        credentials = get_credentials(context)
        print(f"  OK: ak={credentials.ak[:8]}... project={credentials.project_id}", flush=True)

        # Step 2: 客户端
        print("=== STEP 2: build_client ===", flush=True)
        region = os.environ.get("HW_REGION", "cn-north-4")
        client = build_client(credentials, region)
        print(f"  OK: region={region}", flush=True)

        # Step 3: 解析配置
        prefix = os.environ.get("SNAPSHOT_PROCESS_NAME", "rds-dr")
        base = split_list(os.environ.get("INSTANCE_IDS", ""))
        include = split_list(os.environ.get("INCLUDE_DATABASES", ""))
        exclude = split_list(os.environ.get("EXCLUDE_DATABASES", ""))
        retry_count = int(os.environ.get("RETRY_COUNT", "3"))
        retry_sleep = int(os.environ.get("SECONDS_BETWEEN_RETRIES", "60"))

        targets = resolve_targets(base, include, exclude)
        print(f"待备份实例: {targets}; 区域: {region}", flush=True)

        if not targets:
            return {"statusCode": 200, "body": {"message": "未配置任何目标实例，跳过", "successCount": 0, "failureCount": 0}}

        # Step 4: API 类型导入
        from huaweicloudsdkrds.v3 import CreateManualBackupRequest, CreateManualBackupRequestBody
        from huaweicloudsdkcore.exceptions import exceptions

        # Step 5: 创建备份
        result = {"successCount": 0, "failureCount": 0, "details": {}}
        ts = datetime.now(timezone.utc).strftime("%m%d%H%M")  # 缩短时间戳

        for inst in targets:
            # 截取实例 ID 前 8 位 + 时间戳，确保总长 ≤ 50 字符
            inst_short = inst[:8] if len(inst) > 8 else inst
            name = f"{prefix}-{inst_short}-{ts}"
            ok = False
            last_err = None

            print(f"  创建备份: {name} (实例 {inst})", flush=True)

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
                    resp = client.create_manual_backup(req)
                    job_id = getattr(resp, 'job_id', str(resp))
                    print(f"  ✅ 已创建手动备份 {name} job_id={job_id}", flush=True)
                    ok = True
                    break
                except exceptions.ClientRequestException as e:
                    last_err = f"{e.error_code}: {e.error_msg}"
                    print(f"  ❌ 第{attempt}次失败 ({last_err})", flush=True)
                except Exception as e:
                    last_err = str(e)
                    print(f"  ❌ 第{attempt}次异常: {e}", flush=True)

            if ok:
                result["successCount"] += 1
                result["details"][name] = "Succeeded"
            else:
                result["failureCount"] += 1
                result["details"][name] = f"Failed: {last_err}"

        if result["failureCount"] and result["successCount"] == 0:
            raise RuntimeError(f"所有实例备份均失败（已重试 {retry_count} 次）")

        return {"statusCode": 200, "body": result}

    except Exception as e:
        print(f"=== HANDLER EXCEPTION ===", flush=True)
        print(f"  type: {type(e).__name__}", flush=True)
        print(f"  msg:  {e}", flush=True)
        import traceback
        traceback.print_exc()
        return {"statusCode": 500, "body": {"error": str(e), "error_type": type(e).__name__}}
