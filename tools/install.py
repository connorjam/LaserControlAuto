from pathlib import Path

import json
import re
import shutil
import sys


# interface.json 是 JSONC（带注释）。优先用 json-with-comments，
# 没装就退回内置的最小实现 —— 免得为了打包还得先装个包。
try:
    import jsonc
except ModuleNotFoundError:
    class _Jsonc:
        """够用就行的 JSONC 读写：去掉 // 和 /* */ 注释。"""

        @staticmethod
        def _strip(text: str) -> str:
            out, i, state = [], 0, 0
            while i < len(text):
                c = text[i]
                if state == 0:
                    if c == '"':
                        out.append(c)
                        state = 1
                        i += 1
                    elif text[i : i + 2] == "//":
                        while i < len(text) and text[i] != "\n":
                            i += 1
                    elif text[i : i + 2] == "/*":
                        i += 2
                        while i + 1 < len(text) and text[i : i + 2] != "*/":
                            i += 1
                        i += 2
                    else:
                        out.append(c)
                        i += 1
                elif state == 1:
                    out.append(c)
                    if c == "\\":
                        state = 2
                    elif c == '"':
                        state = 0
                    i += 1
                else:
                    out.append(c)
                    state = 1
                    i += 1
            return "".join(out)

        @staticmethod
        def load(fp):
            return json.loads(_Jsonc._strip(fp.read()))

        @staticmethod
        def dump(obj, fp, **kwargs):
            kwargs.pop("ensure_ascii", None)
            fp.write(json.dumps(obj, ensure_ascii=False, **kwargs))

    jsonc = _Jsonc()
    print("[install] 没装 json-with-comments，使用内置的 JSONC 读写")

from configure import configure_ocr_model


working_dir = Path(__file__).parent.parent.resolve()
install_path = working_dir / Path("install")
version = len(sys.argv) > 1 and sys.argv[1] or "v0.0.1"

# the first parameter is self name
if sys.argv.__len__() < 4:
    print("Usage: python install.py <version> <os> <arch>")
    print("Example: python install.py v1.0.0 win x86_64")
    sys.exit(1)

os_name = sys.argv[2]
arch = sys.argv[3]


def get_dotnet_platform_tag():
    """自动检测当前平台并返回对应的dotnet平台标签"""
    if os_name == "win" and arch == "x86_64":
        platform_tag = "win-x64"
    elif os_name == "win" and arch == "aarch64":
        platform_tag = "win-arm64"
    elif os_name == "macos" and arch == "x86_64":
        platform_tag = "osx-x64"
    elif os_name == "macos" and arch == "aarch64":
        platform_tag = "osx-arm64"
    elif os_name == "linux" and arch == "x86_64":
        platform_tag = "linux-x64"
    elif os_name == "linux" and arch == "aarch64":
        platform_tag = "linux-arm64"
    else:
        print("Unsupported OS or architecture.")
        print("available parameters:")
        print("version: e.g., v1.0.0")
        print("os: [win, macos, linux, android]")
        print("arch: [aarch64, x86_64]")
        sys.exit(1)

    return platform_tag


def install_deps():
    if not (working_dir / "deps" / "bin").exists():
        print('Please download the MaaFramework to "deps" first.')
        print('请先下载 MaaFramework 到 "deps"。')
        sys.exit(1)

    if os_name == "android":
        shutil.copytree(
            working_dir / "deps" / "bin",
            install_path,
            dirs_exist_ok=True,
        )
        shutil.copytree(
            working_dir / "deps" / "share" / "MaaAgentBinary",
            install_path / "MaaAgentBinary",
            dirs_exist_ok=True,
        )
    else:
        shutil.copytree(
            working_dir / "deps" / "bin",
            install_path / "runtimes" / get_dotnet_platform_tag() / "native",
            ignore=shutil.ignore_patterns(
                "*MaaDbgControlUnit*",
                "*MaaThriftControlUnit*",
                "*MaaRpc*",
                "*MaaHttp*",
                "plugins",
                "*.node",
                "*MaaPiCli*",
            ),
            dirs_exist_ok=True,
        )
        shutil.copytree(
            working_dir / "deps" / "share" / "MaaAgentBinary",
            install_path / "libs" / "MaaAgentBinary",
            dirs_exist_ok=True,
        )
        shutil.copytree(
            working_dir / "deps" / "bin" / "plugins",
            install_path / "plugins" / get_dotnet_platform_tag(),
            dirs_exist_ok=True,
        )



def rewrite_agent_paths(interface: dict) -> None:
    """把 interface.json 里 agent 的路径从「开发布局」改写成「打包布局」。

    子进程的 CWD 是 interface.json 所在目录，两个布局下 agent 的位置不一样：

        开发时：  assets/interface.json        -> 仓库根/agent      要写 ../agent/main.py
        打包后：  install/interface.json       -> install/agent     要写 ./agent/main.py

    所以开发用的 assets/interface.json 里填的是 ../agent/main.py，
    打包时必须在这里改回来，否则最终用户那边会找不到 agent/main.py。

    顺带把 child_exec 也换掉：如果 deps/python 存在（跑过
    tools/prepare_embedded_python.py），就改成随包附带的便携版解释器，
    这样用户电脑上没装 Python 也能跑。
    """
    agent = interface.get("agent")
    if not agent:
        return

    fixed = []
    for arg in agent.get("child_args", []):
        normalized = str(arg).replace("\\", "/")
        if normalized.endswith("agent/main.py"):
            fixed.append("./agent/main.py")
        else:
            fixed.append(arg)
    agent["child_args"] = fixed

    bundled = install_path / "python" / "python.exe"
    if bundled.exists():
        agent["child_exec"] = "./python/python.exe"
        print("[install] agent 将使用随包附带的便携版 Python: ./python/python.exe")
    else:
        print(
            "[install] 没找到 deps/python，agent 仍使用系统 PATH 里的 python。\n"
            "          想让用户免装 Python，请先跑：\n"
            "            python tools/prepare_embedded_python.py"
        )


def install_embedded_python():
    """把 deps/python（便携版解释器 + 依赖）复制进 install/python。"""
    src = working_dir / "deps" / "python"
    if not (src / "python.exe").exists():
        print("[install] 跳过便携版 Python（deps/python/python.exe 不存在）")
        return

    dst = install_path / "python"
    shutil.copytree(src, dst, dirs_exist_ok=True)
    size = sum(f.stat().st_size for f in dst.rglob("*") if f.is_file())
    print(f"[install] 已打入便携版 Python：{dst}  ({size // 1024 // 1024} MB)")


def install_resource():

    configure_ocr_model()

    shutil.copytree(
        working_dir / "assets" / "resource",
        install_path / "resource",
        dirs_exist_ok=True,
    )
    shutil.copy2(
        working_dir / "assets" / "interface.json",
        install_path,
    )

    with open(install_path / "interface.json", "r", encoding="utf-8") as f:
        interface = jsonc.load(f)

    interface["version"] = version
    rewrite_agent_paths(interface)

    with open(install_path / "interface.json", "w", encoding="utf-8") as f:
        jsonc.dump(interface, f, ensure_ascii=False, indent=4)


def install_chores():
    shutil.copy2(
        working_dir / "README.md",
        install_path,
    )
    shutil.copy2(
        working_dir / "LICENSE",
        install_path,
    )


def install_agent():
    shutil.copytree(
        working_dir / "agent",
        install_path / "agent",
        dirs_exist_ok=True,
    )


if __name__ == "__main__":
    install_deps()
    # 便携版 Python 要在 install_resource() 之前拷好：
    # rewrite_agent_paths() 靠它是否存在来决定 child_exec 写什么
    install_embedded_python()
    install_resource()
    install_chores()
    install_agent()

    print(f"Install to {install_path} successfully.")
