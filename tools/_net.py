#!/usr/bin/env python3
"""打包工具共用的网络辅助。

单独拎出来是因为「分块 + 断点续传 + 内容校验」这套逻辑有点绕，
而 fetch_binaries.py 和 prepare_embedded_python.py 都要用，
复制两份迟早会改歪。

## 为什么要分块

实测这台机器上单条 HTTPS 连接下到 40~47 MB 就会被 CDN 掐断
（表现为 `IncompleteRead` / `SSL: UNEXPECTED_EOF`）。一次性
`copyfileobj` 拿大文件必然失败。

## 为什么要校验内容

只对「文件大小」是不够的：实测遇到过一次下载**大小正好对**、
但中间有段数据损坏，直到解压时才报 `zlib.error: invalid block type`。
所以 zip 会用 `ZipFile.testzip()` 逐条校验 CRC，坏了就整个重下。
"""

from __future__ import annotations

import re
import time
import urllib.request
import zipfile
import zlib
from pathlib import Path

UA = {"User-Agent": "dsh-packager"}
CHUNK = 4 * 1024 * 1024


class CorruptDownload(Exception):
    """下完了但内容校验不通过。"""


def open_url(url: str, *, method: str = "GET", timeout: int = 120, retries: int = 4,
             headers: dict | None = None, log=print):
    merged = dict(UA)
    if headers:
        merged.update(headers)
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return urllib.request.urlopen(
                urllib.request.Request(url, method=method, headers=merged), timeout=timeout
            )
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                wait = 2**attempt
                log(f"  第 {attempt} 次失败（{type(exc).__name__}: {exc}），{wait} 秒后重试")
                time.sleep(wait)
    raise last  # type: ignore[misc]


def content_length(url: str, log=print) -> int:
    """拿文件总大小；HEAD 不行就发个 Range 请求读 Content-Range。"""
    try:
        with open_url(url, method="HEAD", timeout=60, log=log) as resp:
            size = int(resp.headers.get("Content-Length") or 0)
            if size:
                return size
    except Exception as exc:  # noqa: BLE001
        log(f"  HEAD 失败（{exc}），改用 Range 探测大小")
    try:
        with open_url(url, headers={"Range": "bytes=0-0"}, timeout=60, log=log) as resp:
            m = re.search(r"/(\d+)$", resp.headers.get("Content-Range") or "")
            if m:
                return int(m.group(1))
            return int(resp.headers.get("Content-Length") or 0)
    except Exception as exc:  # noqa: BLE001
        log(f"  也算不出总大小：{exc}")
        return 0


def verify_zip(path: Path) -> None:
    """内容校验：zip 要能通过逐条 CRC 检查。

    注意 testzip() 有两种失败方式：返回第一个坏条目的名字，
    或者（压缩流本身乱掉时）直接抛 zlib.error / BadZipFile。两种都要当成损坏。
    """
    if not zipfile.is_zipfile(path):
        raise CorruptDownload("不是完整的 zip（中央目录缺失）")
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
    except (zipfile.BadZipFile, zlib.error, OSError) as exc:
        raise CorruptDownload(f"解压校验时出错：{type(exc).__name__}: {exc}") from exc
    if bad is not None:
        raise CorruptDownload(f"zip 内条目校验失败：{bad}")


def _transfer(url: str, dest: Path, chunk: int, log) -> None:
    """分块下载到 dest。

    如果已经存在 .part，会**从断点继续**（网络差的时候这是救命的：
    下到一半被掐断了，重跑不用从头再来）。
    """
    part = dest.with_suffix(dest.suffix + ".part")
    if part.exists() and part.stat().st_size > 0:
        log(f"  发现断点文件，从 {part.stat().st_size / 1024 / 1024:.1f} MB 继续")

    total = content_length(url, log=log)
    log(f"下载 {dest.name}" + (f"（{total / 1024 / 1024:.1f} MB）" if total else ""))

    stall = 0
    loud = False
    while True:
        done = part.stat().st_size if part.exists() else 0
        if total and done >= total:
            break
        if not total and stall >= 15:
            break

        # ★ 每一块都带 Range（包括第一块），否则第一条请求还是拉整包
        headers = {"Range": f"bytes={done}-{done + chunk - 1}"}
        written = 0
        failed = False
        try:
            with open_url(url, headers=headers, timeout=300, log=log) as resp:
                status = getattr(resp, "status", 200)
                if status == 200 and done and not loud:
                    loud = True
                    log("  服务器不支持断点续传，本块是整包")
                mode = "ab" if (status == 206 or not done) else "wb"
                with part.open(mode) as f:
                    while True:
                        buf = resp.read(256 * 1024)
                        if not buf:
                            break
                        f.write(buf)
                        written += len(buf)
        except Exception as exc:  # noqa: BLE001
            failed = True
            log(f"  连接中断（{type(exc).__name__}），本块拿到 {written / 1024 / 1024:.1f} MB，续传")

        now = part.stat().st_size if part.exists() else 0
        if now <= done:
            stall += 1
            if stall >= 15:
                log("  连续很多次都没拿到新数据，先停下")
                break
        else:
            stall = 0

        # 被 CDN 限流时段请求会一直失败，慢一点更容易恢复
        time.sleep(15 if failed else 1)

        pct = f"{now / total * 100:.0f}%" if total else f"{now / 1024 / 1024:.1f} MB"
        log(f"  已下 {now / 1024 / 1024:.1f} MB  ({pct})")

    got = part.stat().st_size if part.exists() else 0
    if total and got < total:
        log(f"  ⚠️ 只拿到 {got}/{total} 字节，保留 .part 供下次续传")
        raise SystemExit("下载不完整。网络恢复后重跑即可从断点继续。")

    part.replace(dest)


def download(url: str, dest: Path, *, expect_zip: bool = False, chunk: int = CHUNK,
             log=print, label: str | None = None, attempts: int = 3) -> Path:
    """分块下载 + 断点续传 + 内容校验。

    · 已经下好且校验通过的，直接复用
    · zip 会做逐条 CRC 校验；坏了就清掉重下（最多 attempts 次）
    """
    name = label or dest.name

    if dest.exists():
        try:
            if expect_zip:
                verify_zip(dest)
            log(f"  命中缓存 {name}（已校验）")
            return dest
        except CorruptDownload as exc:
            log(f"  缓存里的 {name} 损坏（{exc}），重下")
            dest.unlink(missing_ok=True)

    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            log(f"  第 {attempt}/{attempts} 次重试（整个文件重下）")
        try:
            _transfer(url, dest, chunk, log)
            if expect_zip:
                verify_zip(dest)
            log(f"  ✅ {name}  {dest.stat().st_size / 1024 / 1024:.1f} MB")
            return dest
        except CorruptDownload as exc:
            last = exc
            log(f"  ⚠️ {name} 内容校验不过：{exc}")
            dest.unlink(missing_ok=True)
            dest.with_suffix(dest.suffix + ".part").unlink(missing_ok=True)

    raise SystemExit(
        f"{name} 连续 {attempts} 次都校验不通过（最后错误：{last}）。\n"
        "  网络质量太差，换个网络环境或稍后再试。"
    )
