#!/usr/bin/env python3
"""把 install/ 打成可以直接发给用户的 zip。

产物：dist/LaserControlAuto-<os>-<arch>-<version>.zip
zip 里**不带 install/ 前缀** —— 用户解压出来直接就是 MFAAvalonia.exe 那一层。

版本号默认读 install/interface.json 里的 version（tools/install.py 写进去的那个），
所以正常流程是：

    python tools/install.py v1.0.1 win x86_64
    python tools/pack_zip.py

用法：

    python tools/pack_zip.py                      # win x86_64，版本号自动读
    python tools/pack_zip.py v1.0.1               # 指定版本号
    python tools/pack_zip.py v1.0.1 macos aarch64 # 指定平台
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

working_dir = Path(__file__).parent.parent.resolve()
install_path = working_dir / "install"
dist_path = working_dir / "dist"

# 运行时自己生成的目录，不进分发包。
# （config 里会存用户的窗口标题、任务勾选状态；logs/debug 是日志；output 是扫描结果）
EXCLUDE_DIRS = {"config", "logs", "debug", "temp", "backup", "output"}

# 这些前缀的目录也是运行时产物（比如用户导出的 log_20260922_100738）
EXCLUDE_PREFIXES = ("log_",)

# 字节码缓存，没必要带
EXCLUDE_ANY = {"__pycache__"}


def should_skip(relative: Path) -> bool:
    parts = relative.parts
    if any(part in EXCLUDE_ANY for part in parts):
        return True
    for part in parts:
        if part in EXCLUDE_DIRS or part.startswith(EXCLUDE_PREFIXES):
            return True
    return False


def read_version() -> str:
    interface = install_path / "interface.json"
    if not interface.exists():
        print("[pack] 找不到 install/interface.json，先跑 tools/install.py")
        sys.exit(1)
    with open(interface, "r", encoding="utf-8") as f:
        return json.load(f).get("version") or "v0.0.0"


def main() -> int:
    version = sys.argv[1] if len(sys.argv) > 1 else read_version()
    os_name = sys.argv[2] if len(sys.argv) > 2 else "win"
    arch = sys.argv[3] if len(sys.argv) > 3 else "x86_64"

    if not install_path.exists():
        print("[pack] install/ 不存在，先跑 tools/install.py")
        return 1

    dist_path.mkdir(parents=True, exist_ok=True)
    zip_path = dist_path / f"LaserControlAuto-{os_name}-{arch}-{version}.zip"
    if zip_path.exists():
        print(f"[pack] 覆盖已存在的 {zip_path.name}")

    files = 0
    raw_bytes = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(install_path.rglob("*")):
            relative = path.relative_to(install_path)
            if should_skip(relative):
                continue
            if path.is_file():
                archive.write(path, relative.as_posix())
                files += 1
                raw_bytes += path.stat().st_size
            elif path.is_dir():
                # 保留空目录（zip 里以 / 结尾）
                if not any(path.iterdir()):
                    archive.writestr(relative.as_posix() + "/", "")

    size_mb = zip_path.stat().st_size / 1024 / 1024
    print(
        f"[pack] 已生成 {zip_path}\n"
        f"       版本 {version} · {os_name}/{arch} · {files} 个文件 · "
        f"压缩前 {raw_bytes / 1024 / 1024:.1f} MB → 压缩后 {size_mb:.1f} MB"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
