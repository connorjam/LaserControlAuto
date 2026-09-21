<!-- markdownlint-disable MD033 MD041 -->
<p align="center">
  <img alt="LOGO" src="https://cdn.jsdelivr.net/gh/MaaAssistantArknights/design@main/v1/icons/maa-logo_512x512.png" width="160" height="160" />
</p>

<div align="center">

# 激光控制自动化 · LaserControlAuto

在激光驱动调试软件里自动逐点设置参数，等稳定后到测量软件读取数值，全部记录到 CSV。

由 **[MaaFramework](https://github.com/MaaXYZ/MaaFramework)** 驱动

</div>

---

## 它做什么

一句话：**改一个参数 → 等一会儿 → 读一个数 → 记一行 → 换下一个参数**，循环到扫完为止。

具体流程：

| 步骤 | 动作 |
| --- | --- |
| 1 | 在 **A 软件** 里找到目标参数（默认是 `Temperature`），把输入框清空并填入本轮数值，再点它旁边的 `Setting` 按钮 |
| 2 | 等待一段时间（默认 10 秒），让设备稳定下来 |
| 3 | 到 **B 软件** 里找指定标签（默认是 `Wavelength`）**最近的**那个数值，读出来 |
| 4 | 把「序号 / 设定参数 / 测量值 / 时间」追加写入 CSV |
| 5 | 换下一个参数，回到第 1 步 |

扫描区间、步长、等待时间和结果保存位置都可以在界面上直接填，不用改代码。

---

## 怎么用（最终用户）

### 运行前准备

1. **Windows 10/11 64 位**电脑
2. 装上 [VC++ 运行库](https://aka.ms/vs/17/release/vc_redist.x64.exe)
   （缺了会报「应用程序无法正常启动」）
3. （若提示缺少 .NET）安装 [.NET Desktop Runtime](https://dotnet.microsoft.com/download/dotnet)
4. **打开 A 软件和 B 软件**，让它们都停在工作界面上

> Python 不需要自己装——发布包里已经带了一份便携版解释器。

### 操作步骤

1. 解压发布包，双击 `MFAAvalonia.exe`
2. 控制器选 **「桌面端」**，资源选 **「默认」**
3. 在 **「扫描设置」** 里填：
   - **起始值 / 结束值**：扫描区间，例如 20 到 30
   - **步长**：每一步加多少，例如 1（也可以填 0.5）
   - **每轮等待秒数**：设好参数后等多久再读数，例如 10
   - **输出文件夹**：结果存哪儿，例如 `output`（相对程序目录）或 `D:\数据`
4. 勾选任务 **「激光参数扫描」**，点开始
5. 跑完后到输出文件夹里取 `laser_sweep_result.csv`

> ⚠️ 任务运行期间**不要用手去动鼠标键盘**，程序会自己操作 A 软件。
> 想中途停止，按界面上的停止按钮即可。

### 结果长什么样

`laser_sweep_result.csv`（UTF-8 BOM 编码，Excel 双击不乱码）：

| 序号 | 设定参数 | B软件测量值 | 时间 |
| --- | --- | --- | --- |
| 1 | 20 | 1061 | 2026-09-21 16:40:12 |
| 2 | 21 | 1062 | 2026-09-21 16:40:35 |

某一轮读数失败时会写一行空值占位，保证行号和参数一一对应，不会错位。

---

## 出问题了怎么办

| 现象 | 可能原因 / 处理 |
| --- | --- |
| 点了开始没反应，A 软件没被操作 | 检查 A 软件是否已打开、窗口标题是否和配置一致 |
| 提示找不到窗口 | 窗口标题变了，见下面「换成别的软件」 |
| 数值一直读不到 | B 软件窗口没开，或者标签文字不对 |
| CSV 里测量值全是空的 | 读数失败，看日志里的 OCR 结果 |
| 报「应用程序无法正常启动」 | 装 VC++ 运行库 |

日志位置：`debug/maafw.log`，以及通用 UI 的运行日志面板。

---

## 换成别的软件（改配置）

### 换 B 软件（要读数的那个）—— 改 2 处

编辑 `resource/pipeline/my_task.json`，找到 `LaserSweepReadValueB`：

```jsonc
"window_regex": "^你的B软件窗口标题$",   // ① 窗口标题（用 ^...$ 锚定）
"anchor_text": "要读的那个量的标签",      // ② 例如 "Output Power"
```

> `window_regex` 要加 `^...$`。比如标题叫 `GaussianBeam` 时，
> 不加锚点会连「GaussianBeam - 文件资源管理器」一起匹配上。

### 换 A 软件（要操作的那个）—— 改 3 处

| 改哪儿 | 改成什么 |
| --- | --- |
| `interface.json` → `controller[].win32.window_regex` | A 软件的窗口标题 |
| `my_task.json` → `LaserSweepFindTemperature` 的 `anchor_text` | 参数的标签文字 |
| `my_task.json` → `LaserSweepClickValueBox` 的 `dx_ratio` | 标签到输入框的距离 ÷ 图像宽度 |

**不需要量任何 `roi` 坐标**——界面上的同名干扰项由「配对法」自动排除，分辨率变了偏移会按比例缩放。

---

## 给开发者：怎么打包

见 [`docs/zh_cn/develop/packaging.md`](./docs/zh_cn/develop/packaging.md)。

两条路：

- **本地打包**：准备 `deps/`（MaaFramework）+ `MFA/`（MFAAvalonia），
  跑 `python tools/prepare_embedded_python.py`，再跑 `python tools/install.py`
- **CI 自动打包**：打一个 `v1.0.0` 标签推上去，
  GitHub Actions 会自动为多个平台打包并发 Release

---

## 目录结构

```
LaserControlAuto/
├── assets/
│   ├── interface.json              ← 界面配置：窗口、任务、可填参数
│   └── resource/
│       ├── pipeline/my_task.json   ← 主流程（改这里调流程）
│       ├── image/                  ← 找图用的模板图
│       └── model/ocr/              ← OCR 模型
├── agent/
│   ├── main.py                     ← AgentServer 入口
│   └── laser_sweep.py              ← 自定义识别 / 动作（核心逻辑）
├── tools/
│   ├── install.py                  ← 打包脚本
│   ├── prepare_embedded_python.py  ← 准备便携版 Python
│   └── configure.py                ← OCR 模型配置
├── docs/                           ← 文档
└── .github/workflows/              ← CI：检查 + 自动发版
```

---

## 鸣谢

由 **[MaaFramework](https://github.com/MaaXYZ/MaaFramework)** 强力驱动，
基于 [MaaPracticeBoilerplate](https://github.com/MaaXYZ/MaaPracticeBoilerplate) 模板起步。

## 许可证

MIT，见 [LICENSE](./LICENSE)。
