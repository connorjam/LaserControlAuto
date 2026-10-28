#!/usr/bin/env python3
"""只把「需要覆盖的那几个文件」打成热修复包。

整包（`tools/pack_zip.py`，170 MB）改一两个文件就重发太浪费 —— README 里也写了：
这种情况直接让使用者用新文件覆盖更快。本脚本就是把那份「要覆盖的文件」装进一个
几十 KB 的 zip：

    dist/LaserControlAuto-<version>-hotfix-files.zip
      agent/laser_sweep.py
      agent/plot_curve.py
      resource/pipeline/my_task.json
      resource/image/maalaser-logo_512.png
      interface.json

zip 里**不带 install/ 前缀**（跟整包一样）—— 用户解压出来直接覆盖到
`MFAAvalonia.exe` 那一层，重启任务即可生效。

⚠️ 为什么 `interface.json` 必须在里面（2026-10-14 踩的坑）：
    UI 上的「扫描设置」那些输入框（起始值 / 步长 / **扫描点数** / …）全都定义在
    `interface.json` 的 `option.*.inputs` 里，agent 和 pipeline 只是**接收方**。
    只覆盖 agent + pipeline 的话，新参数根本没有界面可以填 —— 表现就是
    「另一台电脑的 UI 上没有这个新设置」，而在本机（install/ 已被 install.py
    重写过）看却是好的，非常容易误判成「热修复包没生效」。
    所以：**凡是动了 UI 输入项或 pipeline_override 的改动，必须连 interface.json 一起发。**

    （它同时也是 pipeline_override 的所在地 —— 少发它，新字段就传不进流水线，
      agent 那边只会走默认值，症状更隐蔽。）

用法：

    python tools/pack_hotfix.py                       # 版本号读 install/interface.json
    python tools/pack_hotfix.py v1.0.6                # 指定版本号
    python tools/pack_hotfix.py v1.0.6 agent/main.py  # 只打指定文件（路径相对 install/）
    python tools/pack_hotfix.py --sync                # 先把仓库源码同步进 install/ 再打
    python tools/pack_hotfix.py --force               # install/ 与源码不一致时照样打

⚠️ 打包源是 `install/` 里的副本（和 pack_zip.py 一致）。而 install/ 是
`tools/install.py` 从 `agent/`、`assets/resource/` 拷过去的 —— 所以脚本会先比一遍：
两边不一致就停下并告诉你怎么处理（默认拒绝，免得把「昨天的代码」发出去）。
`interface.json` 不参与这项比对：它本来就该和 `assets/interface.json` 不同
（版本号、agent 路径、welcome 都被 install.py 改写过了）。

每个文件都会打印字节数与 md5。用户拿到包后，对着运行日志里那行
`[laser_sweep] agent 文件：…（75625 字节，改于 …）` 就能确认到底换成功没有。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Optional

working_dir = Path(__file__).parent.parent.resolve()
install_path = working_dir / "install"
dist_path = working_dir / "dist"
interface_path = install_path / "interface.json"

# 默认打进热修复包的文件，路径相对 install/，也就是用户程序目录里的路径。
#
# 这几样分别对应「一层」：
#   agent/laser_sweep.py            逻辑层：定位、读数、扫参序列
#   agent/plot_curve.py             绘图层：CSV → 曲线 PNG（「绘制曲线图」任务）
#   resource/pipeline/my_task.json  流程层：节点、延时、默认参数
#   resource/image/maa...png        图标层：interface.json 的 icon 指向它
#   interface.json                  界面层：UI 输入项 + pipeline_override
#
# ⚠️ interface.json 是 2026-10-14 补进来的：此前只发前两个，导致
#    「扫参点数」这种**新增 UI 输入项**的改动在别的电脑上看不到
#    （输入框定义在 interface.json 里，agent 只是接收方）。
DEFAULT_FILES = [
    "agent/laser_sweep.py",
    "agent/plot_curve.py",
    "resource/pipeline/my_task.json",
    "resource/image/maalaser-logo_512.png",
    "interface.json",
]


def read_version() -> str:
    """版本号沿用 install/interface.json 里的那个（tools/install.py 写进去的）。"""
    if not interface_path.exists():
        print("[hotfix] 找不到 install/interface.json，先跑 tools/install.py")
        sys.exit(1)
    with open(interface_path, "r", encoding="utf-8") as f:
        return json.load(f).get("version") or "v0.0.0"


def md5_of(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def dev_source_of(name: str) -> Optional[Path]:
    """install/ 里的这个文件，源头是仓库里的哪一份（用于比对是否同步）。

    tools/install.py 是整目录拷过去的：
        install/agent/**     ← agent/**
        install/resource/**  ← assets/resource/**

    interface.json 故意不在这里 —— 它会被 install.py 改写（版本号、agent 路径、
    welcome 规整），本来就该和 assets/interface.json 不一样。
    """
    if name.startswith("agent/"):
        return working_dir / name
    if name.startswith("resource/"):
        return working_dir / "assets" / name
    return None


def resolve_sources(names: list[str]) -> list[tuple[str, Path, Optional[Path]]]:
    """解析成 (zip 内路径, install 里的真实文件, 仓库源码)，并挡住往 install/ 外跑的路。"""
    root = install_path.resolve()
    resolved: list[tuple[str, Path, Optional[Path]]] = []
    for name in names:
        target = (install_path / name).resolve()
        if root not in target.parents:
            print(f"[hotfix] {name} 不在 install/ 里面，拒绝打包")
            sys.exit(1)
        if not target.is_file():
            print(f"[hotfix] install/ 里找不到 {name}")
            print("         先跑 tools/install.py，或确认路径是相对 install/ 写的")
            sys.exit(1)
        resolved.append((Path(name).as_posix(), target, dev_source_of(name)))
    return resolved


def ensure_in_sync(sources: list[tuple[str, Path, Optional[Path]]], mode: str) -> None:
    """install/ 里的副本必须和仓库源码一致，否则很可能打出旧代码。"""
    stale: list[tuple[str, Path, Path]] = []

    for arcname, src, dev in sources:
        if dev is None or not dev.is_file() or dev.resolve() == src.resolve():
            continue
        if dev.read_bytes() == src.read_bytes():
            continue

        if mode == "sync":
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dev, src)
            print(f"[hotfix] 已同步 {dev.relative_to(working_dir)} → install/{arcname}")
        elif mode == "force":
            print(
                f"[hotfix] ⚠️ --force：install/{arcname} 与 "
                f"{dev.relative_to(working_dir)} 不一致，照样打包"
            )
        else:
            stale.append((arcname, src, dev))

    if not stale:
        return

    print("[hotfix] ⚠️ install/ 里的副本和仓库源码不一致，已停下（免得发出去的是旧代码）：")
    for arcname, src, dev in stale:
        print(f"         install/{arcname:<28} {src.stat().st_size:>7} 字节  md5 {md5_of(src)}")
        print(
            f"         {str(dev.relative_to(working_dir)):<36} "
            f"{dev.stat().st_size:>7} 字节  md5 {md5_of(dev)}"
        )
    print(
        "         三选一：跑 tools/install.py 重新同步 / "
        "加 --sync 让本脚本同步 / 加 --force 强行按现状打包"
    )
    sys.exit(1)


def main() -> int:
    args = sys.argv[1:]
    if any(arg in ("-h", "--help") for arg in args):
        print(__doc__)
        return 0

    mode = "check"
    for flag, name in (("--sync", "sync"), ("--force", "force")):
        if flag in args:
            args.remove(flag)
            mode = name

    version = args[0] if args else read_version()
    sources = resolve_sources(args[1:] or DEFAULT_FILES)
    ensure_in_sync(sources, mode)

    dist_path.mkdir(parents=True, exist_ok=True)
    zip_path = dist_path / f"LaserControlAuto-{version}-hotfix-files.zip"
    if zip_path.exists():
        print(f"[hotfix] 覆盖已存在的 {zip_path.name}")

    raw_bytes = 0
    print(f"[hotfix] 打包 {version} 热修复文件：")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for arcname, src, _dev in sorted(sources):
            archive.write(src, arcname)  # 条目名统一用正斜杠，跨平台解压不会乱
            raw_bytes += src.stat().st_size
            print(f"         {arcname:<34} {src.stat().st_size:>7} 字节  md5 {md5_of(src)}")

    print(
        f"[hotfix] 已生成 {zip_path}\n"
        f"         {len(sources)} 个文件 · 压缩前 {raw_bytes / 1024:.1f} KB → "
        f"压缩后 {zip_path.stat().st_size / 1024:.1f} KB\n"
        f"         用法：解压后覆盖到 MFAAvalonia.exe 那一层，重启任务即生效"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
