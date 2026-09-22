# 打包发布

本文说明怎么把项目打包成**能直接拷到别的电脑上运行**的成品。

---

## 目录

- [打包出来是什么](#打包出来是什么)
- [目标电脑需要什么](#目标电脑需要什么)
- [方式一：CI 自动打包（推荐）](#方式一ci-自动打包推荐)
- [方式二：本地打包](#方式二本地打包)
- [为什么必须带一份 Python](#为什么必须带一份-python)
- [常见问题](#常见问题)

---

## 打包出来是什么

跑完打包后得到的是一个 `install/` 目录（CI 会把它压成 zip 发到 Release）：

```tree
install/
├── MFAAvalonia.exe          ← 用户双击这个（通用 UI）
├── interface.json           ← 项目配置；打包时 agent 路径会被自动改写
├── resource/                ← 流水线 + 图片 + OCR 模型
│   ├── pipeline/my_task.json
│   ├── image/
│   └── model/ocr/
├── agent/                   ← Python 自定义识别 / 动作
│   ├── main.py
│   └── laser_sweep.py
├── python/                  ← 便携版 Python（免安装，见下文）
│   ├── python.exe
│   └── Lib/site-packages/{maa,numpy,...}
├── runtimes/                ← MaaFramework 原生库
├── libs/MaaAgentBinary/     ← AgentServer 通信用的二进制
├── plugins/                 ← MaaFramework 插件
├── debug/                   ← 运行日志（首次运行后生成）
├── output/                  ← 扫描结果 CSV 默认落在这里
├── README.md
└── LICENSE
```

用户解压后双击 `MFAAvalonia.exe` 即可，**不需要装 Python**。

---

## 目标电脑需要什么

| 需要 | 说明 |
| --- | --- |
| Windows 10/11 **64 位** | 便携版 Python 用的是 `embed-amd64` |
| [VC++ 运行库](https://aka.ms/vs/17/release/vc_redist.x64.exe) | 缺了会报「应用程序无法正常启动」 |
| [.NET Desktop Runtime](https://dotnet.microsoft.com/download/dotnet) | MFAAvalonia 是 .NET 应用；如果启动提示缺少 .NET 就装它 |
| A 软件 + B 软件 | 目标程序本身要装好、能正常打开 |

**不需要**：Python、pip、任何 Python 依赖 —— 这些都在包里。

---

## 方式一：CI 自动打包（推荐）

GitHub Actions 已经配好了，打一个 tag 就会自动为多平台打包并发 Release。

### 一次性准备

1. 把代码推到 GitHub 仓库（`connorjam/LaserControlAuto`）
2. 进仓库 **Settings → Actions → General → Workflow permissions**，
   选 **Read and write permissions** → Save
   （不设置的话 CI 建 Release 会被拒）

### 每次发版

```bash
git add .
git commit -m "v1.0.0 发布"
git tag v1.0.0          # 版本号自己定，必须以 v 开头
git push origin HEAD -u
git push origin v1.0.0
```

推上去之后：

1. 打开仓库的 **Actions** 页面，能看到 `install` 工作流在跑
2. 跑完（约 10~20 分钟）到 **Releases** 页面下载
3. 文件名形如 `MaaXXX-win-x86_64-v1.0.0.zip`

CI 会自动完成这些事：

| 步骤 | 谁做的 |
| --- | --- |
| 拉取子模块 `assets/MaaCommonAssets`（OCR 模型来源） | `actions/checkout` 的 `submodules: true` |
| 下载 MaaFramework 原生库到 `deps/` | `robinraju/release-downloader` |
| 下载 MFAAvalonia 到 `MFA/` | 同上 |
| 把 MFA 复制进 `install/` | workflow 里的 rsync |
| **准备便携版 Python** | `tools/prepare_embedded_python.py`（仅 win x86_64） |
| 复制资源 / agent / 改写 interface.json | `tools/install.py` |
| 打包成 zip 并发 Release | `softprops/action-gh-release` |

> **注意**：`.github/workflows/install.yml` 里的 `MAAFW_VERSION` / `MFAA_VERSION`
> 两个环境变量留空表示取最新版；想锁定版本就填具体版本号。

---

## 方式二：本地打包

CI 要联网 + 要 GitHub 权限，嫌麻烦也可以在本机打。

### 准备工作

| 需要 | 怎么弄 |
| --- | --- |
| Python 3.11+ | 系统里能跑 `python` 即可（打包脚本本身不需要额外依赖） |
| **`deps/`** 里的 MaaFramework | 跑 `python tools/fetch_binaries.py` 自动下 |
| **`MFA/`** 里的 MFAAvalonia | 同上，一次下好两个 |
| OCR 模型 | 仓库已自带（`assets/resource/model/ocr/`） |
| Node.js（可选） | 只有跑 `maa-tools check` 时才需要 |

### 步骤

**1. 下载两个外部二进制包**

```powershell
python tools\fetch_binaries.py
```

会自动查最新 release、挑 Windows x64 的资产、下载并解压：

- `MAA-win-x86_64-*.zip` → `deps/`（要能看到 `deps/bin` 和 `deps/share/MaaAgentBinary`）
- `MFAAvalonia-*-win-x64.zip` → `MFA/`

> 想锁定版本：`python tools\fetch_binaries.py --maa-tag v5.13.1 --mfa-tag v2.16.1`
>
> 脚本会校验下载完整性（大小 + zip 结构）。网络断了会保留 `.part` 文件，
> **重跑即从断点续传**，不会白下。

**2. 准备便携版 Python**

```powershell
python tools\prepare_embedded_python.py
```

下载 Python 官方的 embeddable 包解压到 `deps/python/`，再往里装
`maafw` / `numpy` / `MaaAgentBinary` / `StrEnum`（约 130 MB）。

> 这一步**不需要 pip**：脚本直接从 PyPI 取预编译 wheel 解压进去，
> 所以在 Linux 上也能给 Windows 目标装包（CI 就是这么干的）。
>
> 想指定 Python 版本：`python tools\prepare_embedded_python.py 3.13.7`

**3. 组装**

```powershell
mkdir install -Force
Copy-Item -Recurse -Force MFA\* install\
python tools\install.py v1.0.0 win x86_64
```

跑完 `install/` 就是成品。日志里会出现：

```
[install] 已打入便携版 Python：...\install\python  (XXX MB)
[install] agent 将使用随包附带的便携版 Python: ./python/python.exe
Install to ...\install successfully.
```

**4. 自测**

```powershell
cd install
.\MFAAvalonia.exe
```

**5. 发给别人**

把整个 `install/` 目录压成 zip 即可。

### 本地打包时各文件的作用

| 文件 | 作用 |
| --- | --- |
| `tools/fetch_binaries.py` | 下载 MaaFramework + MFAAvalonia 并解压到位 |
| `tools/prepare_embedded_python.py` | 准备便携版 Python（产物在 `deps/python/`） |
| `tools/install.py` | 组装 `install/`：拷原生库、资源、agent、便携版 Python，并改写 `interface.json` |
| `tools/configure.py` | 检查 OCR 模型（已有则跳过） |
| `.github/workflows/install.yml` | CI 版的全套流程，本地打包可以对照着看 |

---

## 为什么必须带一份 Python

AgentServer 是一个**独立的 Python 子进程**：`interface.json` 里的

```jsonc
"agent": {
    "child_exec": "python",          // 开发时用系统 PATH 里的 python
    "child_args": ["../agent/main.py"]
}
```

通用 UI 启动任务时会去拉起这个子进程，自定义识别 / 自定义动作（也就是
`agent/laser_sweep.py` 里那些）全都跑在它里面。

如果目标电脑没有 Python，或者有但没有 `maafw`，子进程会立刻退出，
表现就是**任务看起来启动了但什么都不做**。所以发版时必须：

1. 把解释器 + 依赖打进包（`tools/prepare_embedded_python.py`）
2. 把 `child_exec` 指向它（`tools/install.py` 自动改写成 `./python/python.exe`）

> 参考：MaaFramework 官方文档 `docs/zh_cn/develop/agent.md` 的「打包」一节。

### 版本要对齐

AgentServer 和主框架是**跨进程通信**，两边的框架版本必须一致，
否则握手时会直接报：

```
Protocol version mismatch client: ["v5.12.2"] [kProtocolVersion=7]
                            server: [resp.version=v5.13.1] [resp.protocol=8]
```

也就是说 `deps/` 里的 MaaFramework 版本，必须和
`tools/prepare_embedded_python.py` 装进便携版 Python 的 `maafw` 版本一致。

两者都用「最新版」时通常没问题；要锁版本就两边一起锁：

```powershell
# 锁定 maafw 版本
python tools\prepare_embedded_python.py
deps\python\python.exe -m pip install "maafw==5.13.1"

# 对应地把 CI 里的 MAAFW_VERSION 也设成 v5.13.1
```

---

## 常见问题

### 打包时提示 `Please download the MaaFramework to "deps" first`

`deps/bin` 不存在。按上面第 1 步把 MaaFramework 解压到 `deps/`。

### 打包时提示 `File Not Found: .../assets/MaaCommonAssets/OCR`

子模块没拉。跑 `git submodule update --init --recursive`。
（如果你已经自己把 OCR 模型放进 `assets/resource/model/ocr/` 了，
可以临时把 `tools/install.py` 里 `configure_ocr_model()` 那一行注释掉。）

### 用户那边任务启动了但什么都不做

八成是 agent 子进程没起来。让用户看：

- `debug/maafw.log` 里有没有 agent 相关报错
- 通用 UI 的运行日志面板里有没有 `[laser_sweep]` 开头的打印

如果有 `Protocol version mismatch`，就是上面说的版本没对齐。

### 用户那边点开始没反应

1. 确认 `install/interface.json` 里的 `window_regex` 和目标软件窗口标题匹配
2. 确认 `child_exec` 是 `./python/python.exe` 且 `install/python/python.exe` 真的在
3. 让用户手动验证一下便携版 Python：

   ```powershell
   cd install
   .\python\python.exe -c "import maa, numpy; print('ok')"
   ```

   打印 `ok` 就说明解释器和依赖都没问题。

### 打包后打开软件，「任务列表」一片空白、点 + 也没任务可加

2026-09-22 遇到过一次。日志面板里是一行红字：

```
[ERR] [cfg=Default] 加载界面资源定义失败：file=interface.json,
      reason=Failed to load interface file：...\install\interface.json
```

但**真正的异常在更上面几行**（同一个日志文件的开头部分）：

```
Newtonsoft.Json.JsonSerializationException: welcome announcement entries must be objects.
   at MFAAvalonia.Helper.Converters.MaaWelcomeConverter.ReadJson(...)
```

**原因**：`welcome` 被写成了字符串数组。

```jsonc
"welcome": ["第一条公告", "第二条公告"]   // ❌ MFAAvalonia v2.16.1 直接报错
```

ProjectInterface V2 协议从 v2.10.2 起确实允许数组写法，`tools/validate_schema.py`
的 schema 校验也放行（schema 里 `welcome` 就是 `string | string[]`），
但 MFAAvalonia v2.16.1 的 `MaaWelcomeConverter` 只认两种：

- 一个字符串：`"welcome": "公告正文"`
- 对象数组：`"welcome": [{"label": "标题", "content": "正文"}]`

它抛出的异常会让**整个 interface.json 加载失败**，于是任务、控制器、扫描设置
在界面上全部消失 —— 看上去就像「这软件没有任务」，其实只是公告写法不对。

**处理**：

```jsonc
// ✅ 正确写法；要多条公告就都塞进这一个字符串，用 --- 分隔
"welcome": "第一条公告\n\n---\n\n第二条公告"
```

`tools/install.py` 现在会在打包时自检：发现字符串数组会自动合并成一条并打印告警
（`⚠️ welcome 写成了字符串数组…`），所以重跑一次就行：

```powershell
python tools\install.py v1.0.0 win x86_64
```

> **怎么确认 interface.json 真的被读进去了**：看日志里有没有
> `[Interface] 预加载完成，耗时 xx ms`。没有这行 = 加载失败，
> 界面上一定缺任务。

### 换电脑后坐标对不上怎么办

不需要重调 `roi`——本项目的定位用的是「配对法」（找离标签最近的按钮 / 数值），
界面上的同名干扰项会自动排除，分辨率变了偏移量也按比例缩放。

真正需要改的只有窗口标题和标签文字，见
[README 的「换成别的软件」一节](../../../README.md#换成别的软件改配置)。
