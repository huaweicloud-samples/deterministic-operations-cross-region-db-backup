#!/usr/bin/env python3
"""只读发现脚本：用 AK/SK（来自环境变量）列出账号下所有 RDS 实例。
不写入任何文件，仅打印 JSON。用于为 terraform.tfvars 获取真实 instance_ids。
"""
import os
import sys
import re
import json

from huaweicloudsdkcore.exceptions import exceptions
from huaweicloudsdkcore.auth.credentials import BasicCredentials
from huaweicloudsdkcore.http.http_config import HttpConfig
from huaweicloudsdkiam.v3 import IamClient, KeystoneListProjectsRequest
from huaweicloudsdkrds.v3 import RdsClient, ListInstancesRequest

HTTP_CFG = HttpConfig(timeout=15)

REGION_CN = {
    "cn-north-4": "北京四", "cn-east-3": "上海一", "cn-south-1": "广州",
    "cn-south-2": "深圳", "cn-southwest-2": "贵阳一", "cn-north-9": "乌兰察布一",
    "cn-east-2": "上海二", "cn-north-1": "北京一", "cn-north-2": "北京二",
}


def main():
    ak = os.environ.get("HW_ACCESS_KEY")
    sk = os.environ.get("HW_SECRET_KEY")
    if not ak or not sk:
        print("ERROR: HW_ACCESS_KEY / HW_SECRET_KEY not set in env", file=sys.stderr)
        sys.exit(2)

    iam = (IamClient.new_builder()
           .with_credentials(BasicCredentials(ak, sk))
           .with_endpoint("https://iam.myhuaweicloud.com")
           .with_http_config(HTTP_CFG)
           .build())

    try:
        projects = iam.keystone_list_projects(KeystoneListProjectsRequest()).projects
    except Exception as e:  # noqa
        print(f"ERROR listing projects: {e}", file=sys.stderr)
        sys.exit(3)

    # region -> project_id (first project per region)
    region_pid = {}
    for p in projects:
        name = (p.name or "").strip()
        if name and name not in region_pid:
            region_pid[name] = p.id

    results = []
    for region, pid in sorted(region_pid.items()):
        # 只查真实区域（形如 cn-north-4），跳过 mos 等全局/特殊项目名
        if not re.match(r"^[a-z]{2}-[a-z]+-[0-9]+$", region):
            continue
        try:
            rds = (RdsClient.new_builder()
                   .with_credentials(BasicCredentials(ak, sk, pid))
                   .with_endpoint(f"https://rds.{region}.myhuaweicloud.com")
                   .with_http_config(HTTP_CFG)
                   .build())
        except Exception:  # noqa
            continue
        insts = []
        for attempt in range(2):
            try:
                insts = rds.list_instances(ListInstancesRequest()).instances or []
                break
            except Exception:  # noqa - 瞬时 TLS 抖动
                if attempt == 0:
                    import time
                    time.sleep(1)
                else:
                    insts = []
        for i in insts:
            results.append({
                "region": region,
                "region_cn": REGION_CN.get(region, region),
                "project_id": pid,
                "id": i.id,
                "name": i.name,
                "engine": i.engine,
                "status": i.status,
                "type": getattr(i, "type", None),  # “Single”/“Ha” 等
            })

    print(json.dumps({"count": len(results), "instances": results},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
