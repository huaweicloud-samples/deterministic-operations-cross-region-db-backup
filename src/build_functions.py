# -*- coding: utf-8 -*-
"""
构建 FunctionGraph 部署包（zip）。

华为云 FunctionGraph Python3.9 运行环境不含 huaweicloudsdk*，需把 index.py 连同
SDK 依赖一起打成 zip 上传 OBS，函数以 code_type="obs" 引用。

华为云 Python SDK（huaweicloudsdkcore / huaweicloudsdkrds）及其依赖（requests、
urllib3、certifi、idna、charset_normalizer、python_dateutil、six）均为纯 Python
的 py3-none-any 通用轮子，可在 Python3.9 运行期直接导入，无需针对平台下载轮子。
本脚本直接从当前 Python 环境的 site-packages 复制这些纯 Python 包（跳过 cryptography
等含原生扩展的包——SDK 的 RDS 调用走 HMAC 签名，不依赖 cryptography），避免联网。

⚠️ 重要：必须使用安装了 huaweicloudsdk* 的 Python 环境运行本脚本！
   如果当前 Python 没有 SDK，产物将只有 index.py（~3KB），部署后函数会
   在 handler 入口前崩溃导致「执行失败+日志全空」。
   推荐用法：
     <venv>/Scripts/python.exe build_functions.py snapshot_create

用法：
    python build_functions.py                # 构建全部 3 个函数
    python build_functions.py snapshot_create  # 仅构建指定函数
"""
import glob
import os
import shutil
import site
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
FUNCTIONS = ["snapshot_create", "snapshot_delete", "snapshot_copy"]

# 需要随包携带的纯 Python 包（import 名）。six 是单文件模块。
PKGS = [
    "huaweicloudsdkcore",
    "huaweicloudsdkrds",
    "requests",
    "urllib3",
    "certifi",
    "idna",
    "charset_normalizer",
    "dateutil",   # python_dateutil 的 import 名
    "six",
    "defusedxml", # huaweicloudsdkcore 的隐藏依赖（xml_utils 用到），必须随包携带
]


def site_packages():
    # Windows venv 的纯 Python 包位于 <prefix>/Lib/site-packages
    sp = os.path.join(sys.prefix, "Lib", "site-packages")
    if os.path.isdir(sp):
        return sp
    sps = site.getsitepackages()
    if sps:
        return sps[0]
    return os.path.join(os.path.dirname(sys.executable), "Lib", "site-packages")


def copy_pure_pkg(sp, lib, name):
    """复制包（目录或单文件）到 lib；仅保留运行时必需的纯 Python 文件。

    跳过：
      - 原生扩展（.pyd/.so）
      - __pycache__ / *.pyc（本地编译缓存，目标运行时版本不同会冲突）
      - tests/ / test_*.py（测试代码，运行时不需要）
      - *.dist-info / *.egg-info（元数据，不影响 import）
      - py.typed（类型存根标记文件）
    """
    src_dir = os.path.join(sp, name)
    src_file = os.path.join(sp, name + ".py")
    target_base = os.path.join(lib, name)

    # 需要跳过的目录名和文件模式
    SKIP_DIRS = {"__pycache__", "tests", ".git"}
    SKIP_SUFFIXES = (".pyd", ".so", ".pyc", ".dist-info", ".egg-info", "py.typed")

    if os.path.isdir(src_dir):
        if os.path.exists(target_base):
            return
        os.makedirs(target_base, exist_ok=True)
        for root, dirs, files in os.walk(src_dir):
            # 过滤掉要跳过的目录（原地修改 dirs 阻止 os.walk 进入）
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            rel = os.path.relpath(root, src_dir)
            dest_dir = os.path.join(target_base, rel) if rel != "." else target_base
            os.makedirs(dest_dir, exist_ok=True)
            for f in files:
                # 跳过原生扩展和元数据文件
                if any(f.endswith(s) for s in SKIP_SUFFIXES):
                    continue
                # 跳过测试文件
                if f.startswith("test_") and f.endswith(".py"):
                    continue
                shutil.copy2(os.path.join(root, f), os.path.join(dest_dir, f))
    elif os.path.isfile(src_file):
        shutil.copy2(src_file, os.path.join(lib, name + ".py"))
    else:
        print(f"  [警告] 在 site-packages 未找到 {name}，跳过")
        return


def build(name):
    src_py = os.path.join(HERE, "functions", name, "index.py")
    if not os.path.isfile(src_py):
        raise SystemExit(f"找不到函数源码: {src_py}")

    sp = site_packages()

    # 前置检查：确认关键 SDK 包存在，避免产出空包（~3KB）导致 FGS 静默崩溃
    missing = [p for p in ["huaweicloudsdkcore", "huaweicloudsdkrds"]
               if not (os.path.isdir(os.path.join(sp, p)) or
                       os.path.isfile(os.path.join(sp, p + ".py")))]
    if missing:
        raise SystemExit(
            f"❌ 当前 Python 环境缺少关键依赖: {', '.join(missing)}\n"
            f"   site-packages: {sp}\n"
            f"   请使用安装了 huaweicloudsdk 的 venv 运行本脚本，例如:\n"
            f"     <venv>\\Scripts\\python.exe build_functions.py {name}"
        )

    stage = tempfile.mkdtemp(prefix=f"build_{name}_")
    lib = os.path.join(stage, "lib")
    os.makedirs(lib, exist_ok=True)

    print(f"从 {sp} 复制纯 Python 依赖 ...")
    for pkg in PKGS:
        copy_pure_pkg(sp, lib, pkg)

    zip_path = os.path.join(HERE, "functions", name, f"{name}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(src_py, "index.py")
        file_count = 1
        for root, _dirs, files in os.walk(lib):
            for f in files:
                fp = os.path.join(root, f)
                z.write(fp, os.path.relpath(fp, lib))
                file_count += 1
    size = os.path.getsize(zip_path)
    size_mb = size / (1024 * 1024)
    # 体积护栏：仅 index.py 约 3KB；带完整 SDK 依赖应 > 2MB。
    # 若 < 200KB 说明 SDK 依赖没打进去（多为用了无 SDK 的 Python 运行脚本），
    # 直接报错退出，避免产出空包 → FGS 部署后 handler 入口前静默崩溃（日志全空）。
    if size < 200 * 1024:
        raise SystemExit(
            f"❌ 产物过小 ({size // 1024} KB)，疑似空包（SDK 依赖未打入）。\n"
            f"   请使用安装了 huaweicloudsdk* 的 venv 运行本脚本：\n"
            f"     <venv>\\Scripts\\python.exe build_functions.py {name}"
        )
    print(f"[OK] 已构建 {zip_path} ({size_mb:.1f} MB, {file_count} 个文件)")


if __name__ == "__main__":
    targets = sys.argv[1:] or FUNCTIONS
    for t in targets:
        if t not in FUNCTIONS:
            raise SystemExit(f"未知函数: {t}（可选: {', '.join(FUNCTIONS)}）")
        build(t)
