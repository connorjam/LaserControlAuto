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

循环执行下面五步，直到扫完整个区间：

1. 在 A 软件里定位目标参数那一行，清空输入框，填入本轮数值，点旁边的 `Setting`
2. 等待设备稳定（默认 10 秒）
3. 到 B 软件里读取该参数对应的测量值
4. 把「序号 / 设定值 / 测量值 / 时间」追加写入 CSV
5. 换下一个数值

扫描区间、步长或点数、等待时长、结果存放位置都在界面上填，不用改代码。

定位目标控件靠的是实时 OCR，不需要预先量坐标：

- 参数标签（默认 `Temperature`）和同一行的 `Setting` 按钮通过「最近邻配对」锁定，
  界面上有同名文字也不会认错行
- 输入框位置取「标签右边界」到「按钮左边界」之间那块文字，没有文字就取中点
- B 软件的数值先找「标签右边同一行」，再找「标签正下方」

所以换电脑、改窗口大小、改缩放比例都不影响。

---

## 快速开始

### 环境要求

- Windows 10/11 64 位
- [VC++ 运行库](https://aka.ms/vs/17/release/vc_redist.x64.exe)（缺了会提示「应用程序无法正常启动」）
- 如果启动时提示缺少 .NET，装一下 [.NET Desktop Runtime](https://dotnet.microsoft.com/download/dotnet)
- A 软件和 B 软件都打开，停在含目标参数的那一页

发布包里自带便携版 Python，不需要另外安装。

### 操作步骤

1. 解压发布包，双击 `MFAAvalonia.exe`
2. 控制器选「桌面端」，资源选「默认」
3. 在「扫描设置」里填好起始值、结束值、步长或点数等参数
4. 勾选「激光参数扫描」任务，点开始
5. 扫描结束后会自动弹出 output 文件夹，到里面取本次的 CSV

任务运行期间不要手动操作鼠标键盘，程序要自己控制 A 软件。中途想停就按界面上的停止按钮。

### 结果放在哪

所有结果都在**软件所在目录**的 `output/` 里，每次扫描占一个子文件夹：

```tree
MFAAvalonia.exe
output/
├── 20261016_110530_output/                  ← 默认按系统时间命名
│   └── 20261016_110530_20_30_11.csv         ← 时间_起始值_终止值_总点数
└── 20261016_143012_升温测试/                 ← 也可以自己命名
    └── 升温测试结果.csv
```

默认文件名把这次扫的参数写进去，翻 output 目录时不用逐个打开就能认出是哪个扫描：

| 扫描设置 | 默认文件名 |
| --- | --- |
| 20 → 30，步长 1（11 组） | `20261016_110530_20_30_11.csv` |
| 20.5 → 21，2 组 | `20261016_110530_20.5_21_2.csv` |
| -10 → 0，3 组 | `20261016_110530_-10_0_3.csv` |

名字里的总点数是**实际跑了几组**，不是界面上填的那个数，这样文件名和 CSV 行数始终一致。

「结果文件夹名」和「结果文件名」留空就用上面的默认值，也可以自己起名。两者都支持时间占位符，可以和中文混写：

| 写法 | 展开为 |
| --- | --- |
| `%Y%m%d` | `20261016` |
| `{date}` / `{time}` / `{datetime}` | `20261016` / `110530` / `20261016_110530` |
| `升温_%Y%m%d` | `升温_20261016` |

名称里 Windows 不允许的字符（`\ / : * ? " < > |`）会自动换成下划线。

### 打开 output 文件夹

任务列表里有个「打开输出文件夹」任务，勾选后点开始就会弹出 output 目录：

- 扫描跑过之后，打开的是上次实际使用的目录（改过「输出总目录」也认得）
- 还没跑过扫描也能用，会打开默认的 output（不存在就现建一个）

它需要控制器连接成功，这是通用 UI 的前提，但不要求 A/B 软件开着。

> Project Interface V2 的配置项只有输入框、下拉框、勾选框、开关和快捷键五种，
> 没有按钮控件，所以这里用一个「点一下就弹文件夹」的任务来充当按钮。

---

## 扫描设置说明

### 按步长还是按点数

由「扫描点数」这一项决定，两种模式二选一：

| 扫描点数 | 模式 | 例子（起始 20、结束 30） |
| --- | --- | --- |
| `0`（默认）或留空 | 按步长，点数由区间算出来 | 步长 1 → 11 个点；步长 0.5 → 21 个点 |
| `>0` | 按点数，步长自动反算，此时忽略「步长」 | 填 11 → 每步 1；填 6 → 每步 2；填 1 → 只扫起始值 |

按点数时，步长按 `(结束值 - 起始值) ÷ (点数 - 1)` 计算，最后一个点落在结束值上：

```text
起始 20、结束 30、点数 6  →  20, 22, 24, 26, 28, 30
起始 20、结束 30、点数 4  →  20, 23.333333, 26.666667, 30
```

第二种情况除不尽，A 软件的输入框里会出现 `23.333333` 这样的长小数。如果希望每个点都是整数，把区间除以 `点数 - 1` 凑成整数即可。

运行日志里会写明这次用的是哪种模式：

```text
[laser_sweep] 参数序列共 11 组：20 → 30（按步长（1））
[laser_sweep] 参数序列共 6 组：20 → 30（按点数（6 个点，反算步长 2））
```

### 每轮等待秒数

在 A 软件设好参数后等多久再去 B 软件读数。按设备实际稳定时间来填，这一项每减 1 秒，整轮就快 1 秒。

### 输出设置

| 项 | 作用 | 默认 |
| --- | --- | --- |
| 结果文件夹名 | 本次结果放在 `output/` 下的哪个子文件夹 | 留空 = `系统时间_output` |
| 结果文件名 | CSV 叫什么 | 留空 = `时间_起始值_终止值_总点数` |
| 跑完自动打开 | 扫描结束后是否弹出 output 文件夹 | 开 |
| 输出总目录 | 把 `output/` 整体换到别的位置 | `output` |

---

## 结果格式

CSV 用 UTF-8 BOM 编码，Excel 双击打开不乱码：

| 序号 | 设定参数 | B软件测量值 | 时间 |
| --- | --- | --- | --- |
| 1 | 20 | -79.72 | 2026-09-22 16:51:25 |
| 2 | 25 | -79.94 | 2026-09-22 16:51:32 |
| 3 | 30 | -80.11 | 2026-09-22 16:51:58 |

某一轮读数失败时会写一行空值占位，保证行号和参数一一对应，不会错位。

---

## 速度与调优

在开发机（1331x720 窗口）上实测，一轮大约 18 秒。第一轮会慢一些（约 23 秒），因为它要跑一次 OCR 建立定位缓存。

| 环节 | 优化前 | 现在 |
| --- | --- | --- |
| 全屏 OCR 找 `Temperature` / `Setting` | 4.57 秒 × 2 | 只第一轮一次，之后复用缓存 |
| 等设备稳定 | 10 秒 | 10 秒 |
| 读 B 软件（截图 + OCR） | 5.54 秒 | 5.54 秒 |
| 清空输入框 | 约 4.1 秒（32 次退格） | 约 1.5 秒（12 次） |
| 等画面冻结 | 约 4.3 秒 | 0.2 秒 |
| 点击 / 输入 / 框架调度 | 约 1.1 秒 | 约 1.1 秒 |
| 合计 | 约 34 秒/轮 | 约 18 秒/轮 |

想再压时间，按收益从大到小：

1. **每轮等待秒数**。设备多久稳定就填多少。
2. **定位缓存**。`my_task.json` 里 `LaserSweepFindTemperature` 和 `LaserSweepClickSetting` 的 `custom_recognition_param` 可调：

   | 参数 | 默认 | 含义 |
   | --- | --- | --- |
   | `use_cache` | `true` | 写 `false` 就退回每次都重新 OCR |
   | `cache_ttl` | `600` | 缓存有效期（秒），`0` 表示不因时间失效 |
   | `relocate_every` | `0` | 每复用几次强制重认一次，长跑可以写 `20` |
   | `hint_roi_pad` | `240` | 缓存失效后用「上次位置外扩」的小区域先试一次 |

   截图尺寸变化（窗口被移动或缩放）时缓存自动作废，不会拿旧坐标去点。

3. **`LaserSweepClearField` 的 `repeat`**。够清掉输入框旧值就行。如果日志里发现数值被拼接（如 `20.525`），说明清得不干净，把它调回 32。

日志里可以确认缓存是否生效：

```text
[laser_sweep]   📌 已缓存 A 软件控件位置：anchor=[497, 293, 95, 16]；target=[726, 292, 61, 20]；input=[592, 292, 134, 20]
[laser_sweep]   ♻️ 复用定位缓存（return_box=input）[592, 292, 134, 20]；已复用 3 次，缓存 12 秒
```

流水线里节点之间没有额外延时。框架默认的 `rate_limit`（1000 毫秒）和 `pre_delay` / `post_delay`（各 200 毫秒）已逐个显式压成 0，调整速度只需要看上面几项。

---

## 遇到问题

| 现象 | 处理 |
| --- | --- |
| 任务列表是空的，点 + 也没任务 | `interface.json` 没加载成功，日志里会有「文件 interface.json 加载失败」。常见原因是 `welcome` 写成了字符串数组，见[打包文档](docs/zh_cn/develop/packaging.md) |
| 点开始没反应，A 软件没被操作 | 确认 A 软件已打开，窗口标题和配置一致 |
| 提示找不到窗口 | 窗口标题变了，见「适配其他软件」 |
| 数值一直读不到 | B 软件没开，或者标签文字不对 |
| CSV 里测量值全是空的 | 读数失败，看日志里的 OCR 结果 |
| A 软件参数没被改，或点到别处 | 看日志里的 `锚点候选`、`按钮候选`、`点击 (x, y)`，能看出它选了谁、点在哪；选错就用 `near_text` 指定标签 |
| 改了定位缓存之后点偏 | 缓存只在截图尺寸没变时复用。如果怀疑它拿旧坐标硬点，把节点的 `use_cache` 写成 `false` |
| 数值被拼接（如 `20.525`） | 退格次数不够，把 `LaserSweepClearField.repeat` 调回 32 |
| 日志里一条 `[laser_sweep]` 都没有 | 用的是 v1.0.4 之前的老包，换新版 |
| 提示「应用程序无法正常启动」 | 装 VC++ 运行库 |

日志在通用 UI 的运行日志面板（agent 的打印都以 `[laser_sweep]` 开头），框架层细节在 `debug/maafw.log`。

---

## 适配其他软件

### 换 B 软件（读数的那个）

编辑 `resource/pipeline/my_task.json`，找到 `LaserSweepReadValueB`，改两处：

```jsonc
// 窗口标题，用 ^...$ 锚定；多套软件可以用 | 写在一起
"window_regex": "^(GaussianBeam|光器件耦合系统.*)$",
// 要读的量的标签，可以写数组，任一命中即可
"anchor_text": ["功率CH1", "Wavelength"],
```

数值靠位置识别，程序按顺序尝试：先找标签右边同一行的数值，再找标签正下方的数值，两条都不成立才退回「离标签最近」并打印警告。

两点注意：

- `anchor_text` 不要写成 `"CH1"` 这种既当标签、自身又含数字的词。宽松正则会先在标签自己身上匹配出 `1`，导致每轮都读到 1。现在有「候选不能和标签重叠」的规则兜着，但标签写得越明确越稳。
- `window_regex` 要加 `^...$`。标题叫 `GaussianBeam` 时，不加锚点会连「GaussianBeam - 文件资源管理器」一起匹配上。

### 换 A 软件（操作的那个）

| 改哪里 | 改成什么 |
| --- | --- |
| `interface.json` → `controller[].win32.window_regex` | A 软件的窗口标题 |
| `my_task.json` → `LaserSweepFindTemperature` 的 `anchor_text` | 参数标签文字 |
| `LaserSweepFindTemperature` 的 `near_text`（可选） | 界面上有多个同名标签时，指定用哪一个 |

不需要量任何坐标或 `roi`。

界面上有多个同名标签时（比如两个 `Temperature`），有两种指定方式：

```jsonc
"anchor_text": "Temperature",
"near_text": ["Module Config", "ModuleConfig"],  // 取离这个词最近的那个
// 或者按顺序数：
// "anchor_index": 1,                            // 从上到下第 2 个（0 是第一个）
```

`near_text` 没命中时日志里会打警告并退回原逻辑。每次运行还会打印 `锚点候选：...` 和 `按 near_text=... 钦定锚点 ...`，对着日志就能确认选对没有。

标签、按钮、数值三者都只接受短文本（由 `anchor_max_chars`、`target_max_chars`、`value_max_chars`、`min_digit_ratio` 控制）。界面上的提示行和日志行动辄几十个字，只要含关键词就可能被误认成标签或数值，所以按长度卡了一刀。日志里的 `锚点候选`、`按钮候选` 会列出过滤后的名单。

---

## 开发者：打包

完整说明见 [`docs/zh_cn/develop/packaging.md`](./docs/zh_cn/develop/packaging.md)。两条路：

**本地打包**：准备好 `deps/`（MaaFramework）和 `MFA/`（MFAAvalonia），然后

```bash
python tools/prepare_embedded_python.py
python tools/install.py v1.0.8 win x86_64
python tools/pack_zip.py
```

产物在 `dist/LaserControlAuto-win-x86_64-v1.0.8.zip`。

**CI 自动打包**：打一个 tag 推上去，GitHub Actions 会自动打包 Windows x64 版并创建 Release。

```bash
git tag v1.0.8
git push origin v1.0.8
```

### 热修复包

只改了 agent、流水线或界面配置时，不必重发全部内容，可以只打一个几十 KB 的覆盖包：

```bash
python tools/pack_hotfix.py v1.0.8
```

产物 `dist/LaserControlAuto-v1.0.8-hotfix-files.zip` 里包含三个文件：

| 文件 | 负责 |
| --- | --- |
| `agent/laser_sweep.py` | 逻辑：定位、读数、扫参序列 |
| `resource/pipeline/my_task.json` | 流程：节点、延时、默认参数 |
| `interface.json` | 界面：输入项和 `pipeline_override` |

解压后覆盖到 `MFAAvalonia.exe` 所在的那一层。脚本会打印每个文件的字节数和 md5，使用者对着运行日志里 `[laser_sweep] agent 文件：…` 那行就能确认替换成功。

`interface.json` 不能漏。界面上的输入项全部定义在它的 `option.*.inputs` 里，agent 和流水线只是接收方。只覆盖前两个文件的话，新增的输入项在别的电脑上不会出现，而本机因为 `install/` 已经被 `install.py` 重写过，看着却是正常的，很容易误判成热修复没生效。**动了界面输入项或 `pipeline_override`，就必须连 `interface.json` 一起发**，`tools/pack_hotfix.py` 的默认文件列表已包含它。

打包源是 `install/` 里的副本。`install/` 由 `tools/install.py` 从 `agent/` 和 `assets/resource/` 拷过来，脚本会先比对两边是否一致，不一致就停下并提示（用 `--sync` 就地同步，`--force` 按现状强打）。`interface.json` 不参与比对，因为它本来就该和 `assets/interface.json` 不同（版本号、agent 路径、`welcome` 都被 `install.py` 改写过）。

### 自检脚本

改完定位逻辑或输出逻辑，可以不连软件先跑一遍自检：

```bash
python tools/check_pickers.py   # 定位、读数、扫参序列、点数模式
python tools/check_output.py    # 输出目录、文件名、时间占位符、开关
```

两个脚本都用桩模块顶掉 `maa`，直接调纯函数，几秒钟跑完。

---

## 目录结构

```tree
LaserControlAuto/
├── assets/
│   ├── interface.json              ← 界面配置：窗口、任务、可填参数
│   └── resource/
│       ├── pipeline/my_task.json   ← 主流程
│       ├── image/                  ← 找图用的模板图
│       └── model/ocr/              ← OCR 模型
├── agent/
│   ├── main.py                     ← AgentServer 入口
│   └── laser_sweep.py              ← 自定义识别与动作，核心逻辑
├── tools/
│   ├── install.py                  ← 组装 install/
│   ├── pack_zip.py                 ← 打成发布 zip
│   ├── pack_hotfix.py              ← 打成热修复包
│   ├── prepare_embedded_python.py  ← 准备便携版 Python
│   ├── check_pickers.py            ← 定位逻辑自检
│   ├── check_output.py             ← 输出逻辑自检
│   └── configure.py                ← OCR 模型配置
├── docs/                           ← 文档
└── .github/workflows/              ← CI：检查与自动发版
```

---

## 鸣谢

由 [MaaFramework](https://github.com/MaaXYZ/MaaFramework) 驱动，基于 [MaaPracticeBoilerplate](https://github.com/MaaXYZ/MaaPracticeBoilerplate) 起步。

## 许可证

MIT，见 [LICENSE](./LICENSE)。
