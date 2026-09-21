#!/usr/bin/env python3
"""下载打包所需的两个外部二进制包。

本地打包需要两样东西，手动去 GitHub 找很容易下错版本或下漏：

  1. **MaaFramework** 的原生库 → 解压进 `deps/`（要能看到 `deps/bin` 和
     `deps/share/MaaAgentBinary`）
  2. **MFAAvalonia**（通用 UI）→ 解压进 `MFA/`

这个脚本自动完成：查最新 release → 挑对应平台的资产 → 下载 → **校验完整性**
→ 解压并合并到目标目录。

## 用法

    python tools/fetch_binaries.py             # 最新版
    python tools/fetch_binaries.py --os win --arch x86_64
    python tools/fetch_binaries.py --maa-tag v5.13.1 --mfa-tag v2.16.1

## 为什么不用 GitHub API

匿名调用 `api.github.com` 很容易撞上 60 次/小时的限流。这里改成解析
release 网页（`/releases/expanded_assets/<tag>`），稳定得多。

## 注意

- 下载后会检查文件大小和 zip 完整性。网络中断导致**截断**的包会被识别出来并重下，
  不会像以前那样解压到一半才报 `BadZipFile`。
- 解压采用「先解到暂存目录再合并」，**不会清空目标目录**——
  `deps/` 里还躺着别的工具产物（比如 `deps/python`），不能整个删掉。
"""

from __future__ import annotations

import argparse
import re
import shutil
import zipfile
from pathlib import Path

from _net import download, open_url

WORKING_DIR = Path(__file__).parent.parent.resolve()
DOWNLOADS = WORKING_DIR / "deps" / "downloads"

UA = {"User-Agent": "dsh-fetch-binaries"}

TARGETS = {
    "maa": {
        "label": "MaaFramework",
        "repo": "MaaXYZ/MaaFramework",
        "dest": WORKING_DIR / "deps",
        "assets": {
            ("win", "x86_64"): re.compile(r"^MAA-win-x86_64-.*\.zip$"),
            ("win", "aarch64"): re.compile(r"^MAA-win-aarch64-.*\.zip$"),
            ("linux", "x86_64"): re.compile(r"^MAA-linux-x86_64-.*\.zip$"),
            ("macos", "x86_64"): re.compile(r"^MAA-macos-x86_64-.*\.zip$"),
            ("macos", "aarch64"): re.compile(r"^MAA-macos-aarch64-.*\.zip$"),
        },
        "must_have": "bin",
        # ⚠️ MaaFramework 的 zip 里自带一个 tools/ 目录（放它的 schema 文件），
        #    而本仓库的 deps/tools/ 是模板同步过来的 schema，是 **git 跟踪的**。
        #    直接合并会把仓库文件覆盖掉（实测还删掉了一个），所以要跳过。
        "skip": {"tools"},
    },
    "mfa": {
        "label": "MFAAvalonia",
        "repo": "MaaXYZ/MFAAvalonia",
        "dest": WORKING_DIR / "MFA",
        "assets": {
            ("win", "x86_64"): re.compile(r"^MFAAvalonia-.*-win-x64\.zip$"),
            ("win", "aarch64"): re.compile(r"^MFAAvalonia-.*-win-arm64\.zip$"),
        },
        "must_have": None,
        "skip": set(),
    },
}


def log(msg: str) -> None:
    print(f"[fetch] {msg}", flush=True)


def latest_tag(repo: str) -> str:
    with open_url(f"https://github.com/{repo}/releases/latest", timeout=60, log=log) as resp:
        return resp.url.rstrip("/").split("/")[-1]


def asset_names(repo: str, tag: str) -> list[str]:
    with open_url(f"https://github.com/{repo}/releases/expanded_assets/{tag}", timeout=60, log=log) as resp:
        html = resp.read().decode("utf-8", "ignore")
    names: list[str] = []
    for path in re.findall(r'href="/([^"]*?/releases/download/[^"]+)"', html):
        name = path.split("/")[-1]
        if name not in names:
            names.append(name)
    return names


def extract_merge(zip_path: Path, dest: Path, skip: set[str] | None = None) -> list[str]:
    """解压到暂存目录，再把顶层条目合并进 dest。

    刻意不整个删 dest —— deps/ 里还有别的产物（例如 deps/python），
    以前就是因为 rmtree(deps) 把刚做好的便携版 Python 一起删掉了。

    skip 里的顶层名字会被忽略（例如 MaaFramework 自带的 tools/，
    会和仓库里 git 跟踪的 deps/tools/ 打架）。
    """
    skip = skip or set()
    staging = dest.parent / f"_{dest.name}_staging"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(staging)

    # 如果解压出来只有一层包装目录，就摊平
    entries = list(staging.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        for item in list(inner.iterdir()):
            shutil.move(str(item), str(staging / item.name))
        inner.rmdir()

    dest.mkdir(parents=True, exist_ok=True)
    merged: list[str] = []
    skipped: list[str] = []
    for item in sorted(staging.iterdir()):
        if item.name in skip:
            skipped.append(item.name)
            continue
        target = dest / item.name
        if target.exists():
            shutil.rmtree(target, ignore_errors=True) if target.is_dir() else target.unlink()
        shutil.move(str(item), str(target))
        merged.append(item.name)

    if skipped:
        log(f"  按规则跳过（不动仓库里的同名目录）：{skipped}")

    shutil.rmtree(staging, ignore_errors=True)
    return merged


def handle(key: str, spec: dict, os_name: str, arch: str, tag: str | None) -> None:
    log(f"\n=== {spec['label']} ===")
    pattern = spec["assets"].get((os_name, arch))
    if pattern is None:
        log(f"  不支持 {os_name}/{arch}，跳过")
        return

    resolved = tag or latest_tag(spec["repo"])
    log(f"版本：{resolved}")

    names = asset_names(spec["repo"], resolved)
    matched = [n for n in names if pattern.match(n)]
    if not matched:
        raise SystemExit(
            f"{spec['label']} 的 {resolved} 里没有匹配 {pattern.pattern} 的资产。\n"
            f"  可用资产：{names}"
        )
    name = matched[0]
    url = f"https://github.com/{spec['repo']}/releases/download/{resolved}/{name}"

    zip_path = DOWNLOADS / name
    download(url, zip_path, expect_zip=True, log=log)

    merged = extract_merge(zip_path, spec["dest"], spec.get("skip"))
    log(f"  已合并到 {spec['dest'].name}/：{merged[:10]}")

    must = spec["must_have"]
    if must and not (spec["dest"] / must).exists():
        raise SystemExit(f"{spec['label']} 解压后没看到 {must}/，结构不对")


def main() -> None:
    parser = argparse.ArgumentParser(description="下载打包所需的外部二进制包")
    parser.add_argument("--os", dest="os_name", default="win", choices=["win", "linux", "macos"])
    parser.add_argument("--arch", default="x86_64", choices=["x86_64", "aarch64"])
    parser.add_argument("--maa-tag", help="锁定 MaaFramework 版本，如 v5.13.1")
    parser.add_argument("--mfa-tag", help="锁定 MFAAvalonia 版本，如 v2.16.1")
    args = parser.parse_args()

    handle("maa", TARGETS["maa"], args.os_name, args.arch, args.maa_tag)
    handle("mfa", TARGETS["mfa"], args.os_name, args.arch, args.mfa_tag)

    log("\n完成。下一步：")
    log("  python tools/prepare_embedded_python.py")
    log("  python tools/install.py v1.0.0 win x86_64")


if __name__ == "__main__":
    main()
