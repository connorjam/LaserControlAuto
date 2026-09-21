"""AgentServer 入口。

通用 UI 会以「interface.json 所在目录」为 CWD 启动这个脚本，例如：

    开发时：  python ../agent/main.py <socket_id>          （CWD = assets/）
    打包后：  ./python/python.exe ./agent/main.py <socket_id>   （CWD = install/）

脚本参数里的 socket_id 由通用 UI 生成，用来建立通信套接字。
"""

import os
import sys

# ★ 必须放在导入本地模块之前。
#
# 便携版 Python（官方 embed 包）带一个 pythonXXX._pth 文件，
# 一旦存在这个文件，解释器就**不再自动把「脚本所在目录」加进 sys.path**，
# 只用 ._pth 里列的路径。于是 `import laser_sweep` 会报
# ModuleNotFoundError —— 而开发时用系统 Python 完全正常，
# 这个坑只会在打包后暴露。
#
# 这里显式把自己的目录插到最前面，两种解释器都能正常工作。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from maa.agent.agent_server import AgentServer  # noqa: E402
from maa.toolkit import Toolkit  # noqa: E402

import laser_sweep  # noqa: E402,F401  （导入即注册自定义识别/动作）
import my_action  # noqa: E402,F401
import my_reco  # noqa: E402,F401


def main():
    Toolkit.init_option("./")

    if len(sys.argv) < 2:
        print("Usage: python main.py <socket_id>")
        print("socket_id is provided by AgentIdentifier.")
        sys.exit(1)

    socket_id = sys.argv[-1]

    AgentServer.start_up(socket_id)
    AgentServer.join()
    AgentServer.shut_down()


if __name__ == "__main__":
    main()
