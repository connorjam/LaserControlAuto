#!/usr/bin/env python3
"""准备便携版 Python —— 把 Python 解释器和 agent 依赖一起打进发布包。

## 为什么需要这一步

AgentServer 是一个 **Python 子进程**（agent/main.py）。别人的电脑上不一定装了
Python，更不会装 `maafw`。所以发版时必须把一份小巧的 Python 解释器也带上，
否则用户双击程序后 agent 起不来，任务看起来启动了却什么都不做。

（MaaFramework 官方文档 docs/zh_cn/develop/agent.md 的「打包」一节也是这么建议的。）

## 做法

用的是 Python 官方的 **Embeddable Package**（免安装绿色版，约 10MB）：

  1. 下载 python-<版本>-embed-amd64.zip 并解压到 deps/python/
  2. 改 pythonXY._pth，把 Lib\\site-packages 加进搜索路径并打开 import site
     （嵌入式版本默认不带 pip、也不认 site-packages）
  3. 从 PyPI 下载依赖的 wheel 并**直接解压**进 deps/python/Lib/site-packages

第 3 步刻意**不用 pip**，原因有三：
  · 不依赖 pip（嵌入式 Python 本来就没有 pip，得先装，多一层失败点）
  · 不用编译，直接从 PyPI 取预编译 wheel
  · 跨平台一致 —— 在 Linux 上也能给 Windows 目标装包（CI 就是这么干的）

wheel 文件名里带着「适用哪个 Python / 哪个平台」的标签，脚本据此筛选，
并顺着 `requires_dist` 递归解析依赖。

## 用法

    python tools/prepare_embedded_python.py             # 自动挑一个版本
    python tools/prepare_embedded_python.py 3.13.7      # 指定版本

## 产物

    deps/python/python.exe
    deps/python/Lib/site-packages/{maa,numpy,...}

之后 tools/install.py 会把整个 deps/python 复制到 install/python，
并把 interface.json 里的 agent.child_exec 改写成 ./python/python.exe。

## 注意

- 只支持 **Windows x64**（embed-amd64）。别的平台要另想办法。
- 需要联网。
- 只装**二进制 wheel**。万一某个依赖只有源码包，脚本会明确报错而不是静默装错。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import urllib.request
import zipfile
from pathlib import Path

from _net import download, open_url

WORKING_DIR = Path(__file__).parent.parent.resolve()
DEPS_DIR = WORKING_DIR / "deps"
TARGET_DIR = DEPS_DIR / "python"
SITE_PACKAGES = TARGET_DIR / "Lib" / "site-packages"

PYTHON_ORG = "https://www.python.org/ftp/python"
PYPI_JSON = "https://pypi.org/pypi/{name}/json"

# agent 直接需要的顶层依赖；其余靠 requires_dist 递归解析
ROOT_REQUIREMENTS = ["maafw", "numpy"]

# 目标环境（发布包是 Windows x64 的）
TARGET_PLATFORM_TAG = "win_amd64"
TARGET_SYS_PLATFORM = "win32"
TARGET_PLATFORM_SYSTEM = "Windows"
TARGET_MACHINE = "AMD64"

# 找不到指定版本时的兜底列表（从新到旧）
FALLBACK_VERSIONS = ["3.13.7", "3.13.5", "3.13.1", "3.12.8", "3.11.9"]

UA = {"User-Agent": "dsh-prepare-python"}


def log(msg: str) -> None:
    print(f"[prepare-python] {msg}", flush=True)


def http_json(url: str) -> dict:
    with open_url(url, timeout=120, log=log) as resp:
        return json.loads(resp.read().decode("utf-8"))


# --------------------------------------------------------------------------- #
# 1. 下载并解压嵌入式 Python
# --------------------------------------------------------------------------- #
def pick_python_version(explicit: str | None) -> str:
    for version in ([explicit] if explicit else FALLBACK_VERSIONS):
        url = f"{PYTHON_ORG}/{version}/python-{version}-embed-amd64.zip"
        try:
            with open_url(url, method="HEAD", timeout=30, log=log):
                log(f"选中 Python {version}")
                return version
        except Exception as exc:  # noqa: BLE001
            log(f"  {version} 不可用（{exc}），试下一个")
    raise SystemExit(
        "找不到可用的 Python 版本。请手动指定，例如：\n"
        "  python tools/prepare_embedded_python.py 3.13.7"
    )


def extract_embedded(version: str) -> Path:
    zip_path = DEPS_DIR / f"python-{version}-embed-amd64.zip"
    download(
        f"{PYTHON_ORG}/{version}/python-{version}-embed-amd64.zip",
        zip_path,
        expect_zip=True,
        log=log,
    )

    if TARGET_DIR.exists():
        log(f"清掉旧的 {TARGET_DIR}")
        shutil.rmtree(TARGET_DIR, ignore_errors=True)
    TARGET_DIR.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(TARGET_DIR)

    exe = TARGET_DIR / "python.exe"
    if not exe.exists():
        raise SystemExit(f"解压后没看到 python.exe：{list(TARGET_DIR.iterdir())}")
    log(f"已解压到 {TARGET_DIR}")
    return exe


def enable_site_packages() -> None:
    """嵌入式 Python 默认无视 site-packages，必须改 _pth 才能用第三方库。"""
    candidates = list(TARGET_DIR.glob("python*._pth"))
    if not candidates:
        raise SystemExit(f"找不到 ._pth 文件：{list(TARGET_DIR.iterdir())}")
    pth = candidates[0]

    lines = pth.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    has_site = has_site_packages = False
    for line in lines:
        stripped = line.strip()
        if stripped == "#import site":
            out.append("import site")
            has_site = True
            continue
        if stripped == "import site":
            has_site = True
        if "site-packages" in stripped:
            has_site_packages = True
        out.append(line)
    if not has_site:
        out.append("import site")
    if not has_site_packages:
        out.append("Lib\\site-packages")
    pth.write_text("\n".join(out) + "\n", encoding="utf-8")
    log(f"改 {pth.name} -> {out}")


# --------------------------------------------------------------------------- #
# 2. 解析并下载 wheel（不用 pip）
# --------------------------------------------------------------------------- #
def wheel_tags(filename: str) -> tuple[str, str, str] | None:
    """拆出 wheel 的 (python标签, abi标签, 平台标签)。"""
    if not filename.endswith(".whl"):
        return None
    parts = filename[:-4].split("-")
    if len(parts) < 5:
        return None
    return parts[-3], parts[-2], parts[-1]


def wheel_ok(filename: str, py_short: str) -> bool:
    tags = wheel_tags(filename)
    if tags is None:
        return False
    py_tag, abi_tag, plat_tag = tags
    if plat_tag not in (TARGET_PLATFORM_TAG, "any"):
        return False
    cp = "cp" + py_short.replace(".", "")
    py_ok = any(t == "py3" or t.startswith(cp) for t in py_tag.split("."))
    abi_ok = abi_tag in ("none", "abi3") or abi_tag.startswith(cp)
    return py_ok and abi_ok


def wheel_rank(filename: str, py_short: str) -> tuple[int, int]:
    """越小越优先：先要平台专用的，再要 Python 版本专用的。"""
    tags = wheel_tags(filename)
    plat_tag = tags[2] if tags else "any"
    cp = "cp" + py_short.replace(".", "")
    return (0 if plat_tag == TARGET_PLATFORM_TAG else 1, 0 if cp in filename else 1)


def marker_ok(marker: str | None, py_short: str) -> bool:
    """粗略判断环境标记是否适用（判不了就当作适用，宁可多装）。"""
    if not marker:
        return True
    expr = marker
    # ★ extras 全部当作「没请求」：我们只装运行时依赖，
    #   否则会顺着 extra == 'docs' / 'test' / 'dev' 把 sphinx、pytest、
    #   pylint 甚至只有源码包的 transifex-client 全拽进来。
    expr = re.sub(r"\bextras?\b", '""', expr)
    expr = re.sub(r"python_full_version", f'"{py_short}.0"', expr)
    expr = re.sub(r"python_version", f'"{py_short}"', expr)
    expr = re.sub(r"sys_platform", f'"{TARGET_SYS_PLATFORM}"', expr)
    expr = re.sub(r"platform_system", f'"{TARGET_PLATFORM_SYSTEM}"', expr)
    expr = re.sub(r"platform_machine", f'"{TARGET_MACHINE}"', expr)
    expr = re.sub(r"implementation_name", '"cpython"', expr)
    expr = re.sub(r"os_name", '"nt"', expr)
    # 把 "3.13" 这类版本字符串变成 (3,13)，否则字符串比较会出错（"3.13" < "3.10"）
    expr = re.sub(r'"(\d+(?:\.\d+)*)"', lambda m: "(" + ",".join(m.group(1).split(".")) + ")", expr)
    try:
        return bool(eval(expr, {"__builtins__": {}}, {}))
    except Exception:  # noqa: BLE001
        log(f"  环境标记看不懂，按适用处理: {marker}")
        return True


def parse_requirement(req: str) -> tuple[str, str | None]:
    """'numpy (>=1.0) ; python_version < "3.11"' -> ('numpy', 'python_version < "3.11"')"""
    if ";" in req:
        body, marker = req.split(";", 1)
        head = body.strip().split("[")[0].split("(")[0]
        name = re.split(r"[<>=!~ ]", head)[0].strip()
        return name, marker.strip()
    head = req.strip().split("[")[0].split("(")[0]
    return re.split(r"[<>=!~ ]", head)[0].strip(), None


def resolve(py_short: str) -> list[dict]:
    """广度优先解析依赖闭包，返回一串可直接下载的 wheel 信息。"""
    todo = list(ROOT_REQUIREMENTS)
    seen: set[str] = set()
    picked: list[dict] = []

    while todo:
        raw = todo.pop(0)
        name, marker = parse_requirement(raw)
        key = name.lower().replace("_", "-")
        if not name or key in seen:
            continue
        seen.add(key)

        if not marker_ok(marker, py_short):
            log(f"跳过 {name}（环境标记不适用：{marker}）")
            continue

        try:
            data = http_json(PYPI_JSON.format(name=name))
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"查不到包 {name}：{exc}")

        files = [f for f in data["urls"] if wheel_ok(f["filename"], py_short)]
        if not files:
            avail = [f["filename"] for f in data["urls"]]
            raise SystemExit(
                f"{name} 没有适用于 Python {py_short} / {TARGET_PLATFORM_TAG} 的预编译 wheel。\n"
                f"  可用文件：{avail}\n"
                f"  （本项目刻意只装二进制包，不在打包时编译东西）"
            )
        files.sort(key=lambda f: wheel_rank(f["filename"], py_short))
        chosen = files[0]
        log(f"选中 {chosen['filename']}  ({chosen['size'] / 1024 / 1024:.1f} MB)")
        picked.append(
            {
                "name": name,
                "version": data["info"]["version"],
                "filename": chosen["filename"],
                "url": chosen["url"],
            }
        )

        for dep in data["info"].get("requires_dist") or []:
            dep_name, _ = parse_requirement(dep)
            if dep_name and dep_name.lower().replace("_", "-") not in seen:
                todo.append(dep)

    return picked


def install_wheels(specs: list[dict]) -> None:
    SITE_PACKAGES.mkdir(parents=True, exist_ok=True)
    cache = DEPS_DIR / "wheels"
    cache.mkdir(parents=True, exist_ok=True)

    for spec in specs:
        whl = cache / spec["filename"]
        download(spec["url"], whl, expect_zip=True, log=log, label=f"{spec['name']} {spec['version']}")
        log(f"解压 {whl.name}")
        with zipfile.ZipFile(whl) as zf:
            zf.extractall(SITE_PACKAGES)
        # wheel 里可能带 *.data/{purelib,platlib}，把内容挪到根上
        for data_dir in list(SITE_PACKAGES.glob("*.data")):
            for sub in ("purelib", "platlib"):
                src = data_dir / sub
                if not src.is_dir():
                    continue
                for item in src.iterdir():
                    dst = SITE_PACKAGES / item.name
                    if dst.exists():
                        if dst.is_dir():
                            shutil.rmtree(dst, ignore_errors=True)
                        else:
                            dst.unlink()
                    shutil.move(str(item), str(dst))
            shutil.rmtree(data_dir, ignore_errors=True)


def verify() -> None:
    """可能在 Linux 上交叉打包，跑不了 python.exe，所以查目录结构。"""
    log("检查 site-packages")
    need = ["maa", "numpy"]
    missing = [n for n in need if not (SITE_PACKAGES / n).exists()]
    if missing:
        raise SystemExit(
            f"site-packages 里缺少：{missing}\n"
            f"内容：{sorted(p.name for p in SITE_PACKAGES.iterdir())}"
        )
    dist_infos = sorted(p.name for p in SITE_PACKAGES.glob("*.dist-info"))
    log(f"  已装 {len(dist_infos)} 个发行包：")
    for d in dist_infos:
        log(f"    {d}")
    log("✅ 便携版 Python 准备好了")


def main() -> None:
    parser = argparse.ArgumentParser(description="准备便携版 Python 供打包使用")
    parser.add_argument("version", nargs="?", help="Python 版本，如 3.13.7；不填则自动挑")
    args = parser.parse_args()

    version = pick_python_version(args.version)
    py_short = ".".join(version.split(".")[:2])

    extract_embedded(version)
    enable_site_packages()

    log(f"解析依赖（目标：Python {py_short} / {TARGET_PLATFORM_TAG}）")
    specs = resolve(py_short)
    install_wheels(specs)
    verify()

    size = sum(f.stat().st_size for f in TARGET_DIR.rglob("*") if f.is_file())
    log("")
    log(f"目录：{TARGET_DIR}")
    log(f"大小：{size / 1024 / 1024:.1f} MB")
    log("接下来跑： python tools/install.py <版本> win x86_64")


if __name__ == "__main__":
    main()
