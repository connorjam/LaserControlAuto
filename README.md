<!-- markdownlint-disable MD033 MD041 -->
<p align="center">
  <img alt="LOGO" src="docs/zh_cn/develop/maalaser-logo_512.png" width="160" height="160" />
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
| 1 | 在 **A 软件** 里**现场 OCR 定位**目标参数那一行（默认标签 `Temperature`；有同名标签时用 `near_text` 指定要哪个），清空输入框、填入本轮数值，再点同一行的 `Setting` |
| 2 | 等待一段时间（默认 10 秒），让设备稳定下来 |
| 3 | 到 **B 软件** 里读指定标签**旁边**的数值（默认标签 `功率CH1`；标签右边同一行、或标签正下方都认） |
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
| 1 | 20 | -79.72 | 2026-09-22 16:51:25 |
| 2 | 25 | -79.94 | 2026-09-22 16:51:32 |
| 3 | 30 | -80.11 | 2026-09-22 16:51:58 |

（数值随设定参数变化，这里只是示意；GaussianBeam 那类 B 软件读到的会是 `1061` 这样的波长值。）

某一轮读数失败时会写一行空值占位，保证行号和参数一一对应，不会错位。

---

## 出问题了怎么办

| 现象 | 可能原因 / 处理 |
| --- | --- |
| 打开后「任务列表」是空的，点 + 也没任务可加 | `interface.json` 没加载成功：日志里会有「文件 interface.json 加载失败！」。常见原因是 `welcome` 写成了字符串数组，见[打包文档的常见问题](docs/zh_cn/develop/packaging.md) |
| 点了开始没反应，A 软件没被操作 | 检查 A 软件是否已打开、窗口标题是否和配置一致 |
| 提示找不到窗口 | 窗口标题变了，见下面「换成别的软件」 |
| 数值一直读不到 | B 软件窗口没开，或者标签文字不对 |
| CSV 里测量值全是空的 | 读数失败，看日志里的 OCR 结果 |
| A 软件的参数没被改，或点到别处去了 | 看日志面板里 `[laser_sweep]` 那几行：`锚点候选` / `按钮候选` / `点击 (x, y)` 会说明它选了谁、点在哪；选错就用 `near_text` 指定标签 |
| 日志里一条 `[laser_sweep]` 都没有 | 用的是 v1.0.4 之前的老包（stdout 被块缓冲吃掉，打印刷不出来），换新版即可 |
| 报「应用程序无法正常启动」 | 装 VC++ 运行库 |

日志位置：通用 UI 的运行日志面板（agent 的调试打印都以 `[laser_sweep]` 开头），
以及 `debug/maafw.log`（框架层细节）。

---

## 换成别的软件（改配置）

### 换 B 软件（要读数的那个）—— 改 2 处

编辑 `resource/pipeline/my_task.json`，找到 `LaserSweepReadValueB`：

```jsonc
// ① 窗口标题（用 ^...$ 锚定；多套软件可以用 | 一次写全）
"window_regex": "^(GaussianBeam|光器件耦合系统.*)$",
// ② 要读的那个量的标签，可以写数组（任一命中即可）
"anchor_text": ["功率CH1", "Wavelength"],
```

数值是靠**位置**认的，程序自己会挑：

1. 先找「标签**右边、同一行**」的数值 —— 光器件耦合系统那种 `功率CH1 │ -79.720dBm` 的排法；
2. 再找「标签**正下方**」的数值 —— GaussianBeam 那种 `Wavelength` 换行接 `1061 nm` 的排法；
3. 两条都不成立，才退回「离标签最近」并打警告（结果未必准）。

> ⚠️ 别把 `anchor_text` 写成 `"CH1"` 这种既当标签、自身又含数字的词。
> 宽松正则在标签自己身上就能匹配出 `1` —— 换电脑后实测踩过这个坑：测量值恒为 1。
> 现在有「候选不许和标签重叠」这条护栏兜着，但标签写得越明确越稳。

> `window_regex` 要加 `^...$`。比如标题叫 `GaussianBeam` 时，
> 不加锚点会连「GaussianBeam - 文件资源管理器」一起匹配上。

### 换 A 软件（要操作的那个）—— 改 2 处

| 改哪儿 | 改成什么 |
| --- | --- |
| `interface.json` → `controller[].win32.window_regex` | A 软件的窗口标题 |
| `my_task.json` → `LaserSweepFindTemperature` 的 `anchor_text` | 参数的标签文字 |
| （可选）`LaserSweepFindTemperature` 的 `near_text` | 界面上有多个同名标签时，钦定「离它最近」的那一个 |

**不需要量任何坐标、比例或 `roi`**：

- 「标签」和它那一行的 `Setting` 按钮由**配对法**锁定，界面上有同名文字也认不错行；
- 输入框位置是**每次点击前现场 OCR 夹出来的**：取「标签右边界 → 按钮左边界」之间那块文字
  （也就是输入框里的当前值），中间没文字就取两者中点。

所以窗口大小、分辨率、DPI 怎么变都不用改配置。
（v1.0.2 之前是「标签中心 + 窗口宽度 × 比例」，换台电脑窗口一小就点偏了。）

**界面里有多个同名标签时**（比如两个 `Temperature`），告诉程序该用哪一个：

```jsonc
"anchor_text": "Temperature",
"near_text": ["Module Config", "ModuleConfig"],  // 取「离它最近」的那个标签
// 或者干脆数着来：
// "anchor_index": 1,                            // 从上到下第 2 个（0 = 第一个）
```

`near_text` 没命中时日志里会打 ⚠️ 并退回旧逻辑；每次运行还会打印
`锚点候选：...` 和 `按 near_text=... 钦定锚点 ...`，对着日志就能确认选对没有。

> **标签、按钮、数值三者都只认「短文本」**
> （`anchor_max_chars` / `target_max_chars` / `value_max_chars` / `min_digit_ratio`）。
> 界面上的 Tips、日志行动辄几十个字，只要含关键词就会冒充成标签、按钮或数值。
> 2026-09-22 换电脑后连吃两次：`Start Wavelength 1548.000000rm not in range[...]`
> 冒充成 B 软件的标签；而 `Tips：Setting TEC Temperature Successfully!`
> 一行**同时**冒充了 A 软件的标签和 `Setting` 按钮 —— 它俩的关键词都在里面，
> 距离还比真控件近。
>
> 日志里 `锚点候选` / `按钮候选` 会列出过滤后的名单，
> 跑一轮就知道有没有漏网的。

---

## 给开发者：怎么打包

见 [`docs/zh_cn/develop/packaging.md`](./docs/zh_cn/develop/packaging.md)。

两条路：

- **本地打包**：准备 `deps/`（MaaFramework）+ `MFA/`（MFAAvalonia），
  跑 `python tools/prepare_embedded_python.py`，再跑 `python tools/install.py v1.0.5 win x86_64`，
  最后 `python tools/pack_zip.py` 打出 `dist/LaserControlAuto-win-x86_64-v1.0.5.zip`
- **CI 自动打包**：打一个 `v1.0.5` 标签推上去，
  GitHub Actions 会自动打包 **Windows x64 版**并创建 Release

> 只是修了 agent / 流水线的一两个文件时，不必重发整个包：
> 直接让使用者用新文件覆盖 `agent/laser_sweep.py`、`resource/pipeline/my_task.json` 更快
> （记得核对字节数，日志里会打印 agent 文件的指纹）。

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
│   ├── pack_zip.py                 ← 把 install/ 打成 dist/ 里的发布 zip
│   ├── prepare_embedded_python.py  ← 准备便携版 Python
│   ├── check_pickers.py            ← 定位逻辑自检（不连软件，改完跑一下）
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
