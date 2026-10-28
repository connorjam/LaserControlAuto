"""激光控制自动化 —— 参数扫描的自定义识别与自定义动作。

对应流水线：assets/resource/pipeline/my_task.json 里的 LaserSweep* 节点。

流程：
    laser_sweep_begin        生成参数序列（20, 21, ... 30）并建好 CSV 表头
    laser_sweep_apply_param  把本轮参数值注入输入框节点的 input_text
    （点击 / 全选 / 输入 / 点 Setting 由流水线节点完成，不在本文件里）
    laser_find_nearest_setting  找出离 "Temperature" 最近的 "Setting" 并交给 Click
                             ★ 第一次会全屏 OCR，并把「标签 / 输入框 / 按钮」三个框缓存下来；
                               之后每轮直接复用缓存（见 _remember_controls / _controls_fresh），
                               省掉每轮两次全屏 OCR —— 实测这是整个流程里最贵的一步
    laser_read_value_b       读取 B 软件测量值
                             · 配了 window_regex → 单独建 Win32 控制器截 B 的图
                             · 没配            → 用主控制器的截图（也就是 A 的画面）
                             · mode=fixed      → 直接返回占位值，不识别
                             · 2026-10-27 起：先在 1 秒窗口里**反复快读**同一个数值框，
                               采样次数由 OCR 实际速度决定（第一次全屏定位后，
                               后续只认那一小块），最后取**平均数**写进 CSV
                               （见 DEFAULT_SAMPLE_WINDOW_SEC 那一段）
    laser_sweep_guard        读数校验（2026-10-26 新增）：把这次读到的值和上一次比，
                             差得离谱就等几秒重读；连续 5 次都不正常则终止扫描
    laser_sweep_retry_value  重测等待节点（流水线里的普通节点，只有 post_delay，
                             由 laser_sweep_guard 现改；本文件没有对应代码）
    laser_sweep_abnormal_stop  连续异常到顶：写空值占位、打印摘要、停止整个任务
    laser_sweep_record       把「序号, 设定值, 测量值, 时间」追加进 CSV
    laser_sweep_record_failed   识别失败时写入空值占位，保证行数对齐
    laser_sweep_advance      推进到下一个参数，跑完则跳去收尾节点
    laser_sweep_finish       打印结果文件路径，收尾
    laser_sweep_plot_curve   独立任务：把导出的 CSV 画成曲线图（实现在 plot_curve.py）
    laser_sweep_abort        定位失败时中止并打印原因

依赖：pip install MaaFw
"""

from __future__ import annotations

import csv
import ctypes
import json
import re
import smtplib
import socket
import ssl
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any, NamedTuple, Optional

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition

# ── 让每条 print 立刻进日志 ────────────────────────────────────────────────
# AgentServer 把子进程的 stdout 接到框架日志管道上，但 Python 默认是块缓冲：
# 输出要攒够 8KB 才真正写出去。表现就是任务跑完了、界面看着一切正常，
# 日志里却一条 [laser_sweep] 都搜不到 —— 2026-09-22 排查时被这个坑了半天
# （16:40 那次运行就是这样，整轮下来零条记录，只能靠猜）。
# 改成行缓冲后，每条打印都能实时看到。
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except (AttributeError, ValueError):  # 极老的解释器 / 已被替换成非文本流
    pass

# 默认值（可被流水线里的 custom_*_param 覆盖）
DEFAULT_CSV_DIR = "./output"  # 相对 interface.json 所在目录
DEFAULT_CSV_NAME = "laser_sweep_result.csv"
DEFAULT_HEADER = ["序号", "设定参数", "B软件测量值", "时间", "备注"]
DEFAULT_PATTERN = r"[-+]?\d+(?:\.\d+)?"

# ── 输出目录结构（2026-10-16 主人要求）─────────────────────────────────────
# 每个软件所在目录下固定有一个 output/ 总目录，每次扫描的结果各占一个子文件夹：
#
#     软件目录/
#     └── output/                                        ← 总目录（按钮打开的就是它）
#         ├── 20261016_110530_output/                    ← 本次扫描（默认名 = 系统时间_output）
#         │   └── 20261016_110530_20_30_11.csv           ← 默认名 = 时间_起始值_终止值_总点数
#         └── 20261016_143012_升温测试/
#             └── 升温测试结果.csv
#
# 「系统时间」用 %Y%m%d_%H%M%S 取到秒：同一天连跑多次也不会互相覆盖
# （每次 begin 都会覆盖写 CSV 表头，重名就等于把上一次的结果抹掉）。
OUTPUT_ROOT_NAME = "output"
DEFAULT_RUN_DIR_FORMAT = "%Y%m%d_%H%M%S_output"
# 用户把「结果文件名」填得只剩非法字符时的兜底名（正常情况下走 _default_csv_stem）
DEFAULT_CSV_STEM = "laser_sweep_result"

# Windows 文件名里不许出现的字符：\ / : * ? " < > |
# 用户可能把「文件夹名」填成 "2026/10/16 测试" 或 "a:b"，
# 直接拿去建目录会抛 OSError 或悄悄建出错层级，所以先换成下划线。
_ILLEGAL_NAME_CHARS = r'\/:*?"<>|'
_ILLEGAL_NAME_RE = re.compile(f"[{re.escape(_ILLEGAL_NAME_CHARS)}]")
# Windows 保留设备名，做成文件夹会失败（CON/PRN/AUX/NUL/COM1-9/LPT1-9）
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# 标签和数值都应该是「短文本」。界面上的日志/提示行动辄几十个字，
# 它们只要含有关键词就会冒充成标签，把整条定位链带偏（见 _short_hits）。
DEFAULT_ANCHOR_MAX_CHARS = 16  # "Temperature:"=12、"功率CH1"=6、"Wavelength"=10 都在内
DEFAULT_TARGET_MAX_CHARS = 16  # 按钮文字同样是短文本："Setting"=7、"è Setting"=9
DEFAULT_VALUE_MAX_CHARS = 24  # "-79.720dBm"=10、"1061 nm"=7 都在内
DEFAULT_MIN_DIGIT_RATIO = 0.3  # 数值文本里数字字符的最低占比

# ── A 软件控件定位缓存 ────────────────────────────────────────────────────
# 为什么要有它：一轮扫描里「找 Temperature」和「找 Setting」本来是两次全屏 OCR，
# 而这两次认的其实是**同一屏、同一行**的三个框（标签 / 输入框 / 按钮）。
# 2026-10-12 复核 09-22 的运行日志：一次全屏 OCR 中位 4.57 秒（38 次采样的中位数），
# 也就是说光定位每轮就烧掉约 9 秒 —— 而整个扫描期间窗口根本不会动。
# 于是改成：第一次 OCR 把三个框一起存下来，之后每轮直接复用，
# 只在「缓存过期 / 截图尺寸变了 / 显式关掉缓存」时才重新 OCR。
DEFAULT_CACHE_TTL = 600.0  # 缓存最长用多久（秒）；0 = 不因时间失效
DEFAULT_HINT_ROI_PAD = 240  # 缓存失效后重定位：拿上次的框外扩这么多像素当 ROI 先试一次
DEFAULT_RELOCATE_EVERY = 0  # 每复用多少次强制重新 OCR 一次；0 = 不强制

# ── 读数校验 / 重测 / 告警（2026-10-26）────────────────────────────────────
# 为什么要有这一层：B 软件的 OCR 偶尔会把 "-79.720dBm" 认成 "-95"，或者设备
# 还没稳定下来就把上一组的旧值读了回来。以前这种值会**直接写进 CSV**，只有人工
# 翻表时才发现 —— 而一整轮扫描要跑四十多分钟，等发现时前面几组数据已经全废了。
#
# 现在的做法是三步：读到一个值先和「上一次正常读数」比 → 差得离谱就等几秒重读
# → 连续 5 次都不正常就停下来、发邮件，并且**不把脏数据当基准**。
# 宁可停早一轮，也不要把错的结果写进交付文件。
DEFAULT_GUARD_ENABLED = True
# 绝对阈值：功率 dBm 这种量，相邻两组的正常波动本来就有 0.x
#
# 2026-10-27 主人要求从 1.0 放宽到 3.0：宁可漏掉一次偶发误读，也不要
# 因为阈值定得太紧而误杀真实波动、把好数据逼成重测。实测量级参考：
#   · 真实组间波动约 0.04（20→30 扫 11 组，功率从 -79.72 漂到 -80.11）
#   · 3.0 是它的 75 倍 —— 正常情况下绝不会触发；
#   · 而「认错一位数字」这类误读动辄偏 5 以上（如 -79.72 → -85.3），照样抓得住。
# 也就是说这个阈值挡的是「离谱」，不是「抖动」——定位很清晰。
DEFAULT_TOLERANCE_ABS = 3.0
# 相对阈值按**百分比**理解：10 = 10%，0.1 = 10%（详见 _percent_value）
#
# ⚠️ 为什么相对阈值只有 1%（2026-10-26 用真实数据算的）：
#    实测 20→30 跑 11 组，功率整段才漂 0.4。若相对阈值给 10%（-79.72 上 = ±7.97），
#    判据「取更宽松者」就永远由相对项胜出，绝对阈值一次都不生效，保护层等于没开。
#    压到 1%（-79.72 上 = ±0.8）之后，起约束的是绝对阈值，量级才对得上真实波动。
DEFAULT_TOLERANCE_PCT = 1.0
# 重测前等几秒：给 B 软件把新值刷出来的时间，太短会连着读到同一屏旧值
DEFAULT_RETRY_WAIT = 3.0
DEFAULT_RETRY_MAX = 5
# 连续异常到顶之后怎么办（2026-10-27 新增开关，默认「继续测试」）：
#   True  = 发完告警邮件、记空值占位，然后**继续扫下一个点**。
#           主人原话：异常出现了邮件发给我，但我未必有时间处理，
#           最合适的是让它继续跑完，别因为一个点把整轮扫描停在半路。
#   False = 老行为：终止整个扫描。
# 注意两种都会发邮件（只要「读数异常发送邮件」开着），区别只在要不要停。
DEFAULT_CONTINUE_ON_ABNORMAL = True
# ── 「真实跳变」的内部一致性容差（2026-10-27 审查建议分开）──────────────
#
# 为什么不复用 tolerance_abs / tolerance_pct：
#   那两个是**跨参数点**的判据（"这个点和上一个点差多少算正常"），
#   默认 3.0。而"这几次重测彼此像不像同一个值"是另一件事：
#   实测真实跳变的内部跨度只有 0.10~0.23，用 3.0 当门槛太宽松 ——
#   噪声只要碰巧落在 3.0 的带子里就会被误判成"稳定"。
#   分开之后语义清晰：跨点宽松（3.0，不误杀真实波动），
#   同点严格（0.3，只有真的重复才认）。
DEFAULT_JUMP_ABS_TOL = 0.3
DEFAULT_JUMP_PCT_TOL = 0.5
DEFAULT_RETRY_NODE = "LaserSweepRetryValue"
DEFAULT_ADVANCE_NODE = "LaserSweepAdvance"

# ── 1 秒窗口多点采样（2026-10-27 主人要求）─────────────────────────────────
# 原来的读法是「截一次图 → 全屏 OCR → 取那个数」，一次就是 5.5 秒，而这一秒里
# B 软件其实刷了好几个值。B 软件的读数本身在抖（组内波动比组间还大），
# 单点采样等于随手抓了其中一个，抓到大的一侧还是小的一侧全凭运气。
#
# 现在的做法：在 1 秒的时间窗里**反复读**同一个数值框，读多少次不写死 ——
# 由 OCR 的实际速度决定（快机器多读几次，慢机器少读几次），
# 最后把这些采样的**平均数**填进 CSV。这样这一秒里的抖动被平均掉，
# 组与组之间的差异才真实反映设定参数的影响。
#
# 为什么是 1 秒而不是 5 秒：一轮扫描的硬成本里，等设备稳定 10 秒 + 读数 5.5 秒
# 已经占了大头，采样窗再拉长会让 11 组扫描从 3 分钟涨到十几分钟。
# 1 秒是「抖动平均得动」和「不把整轮拖慢」之间的折中，也正好是一般仪表
# 刷新一到三帧的量级 —— 能覆盖住刷新周期，又不会把不同时刻的物理状态混进来。
#
# 采样窗内**至少**读一次：OCR 单次要 5.5 秒（全屏）时也不至于一个样本都没有。
DEFAULT_SAMPLE_WINDOW_SEC = 1.0
# 采样窗的最少/最多采样次数。最少 1 次保证「窗再短也有数据」（这个下限直接写成
# if attempts 判断，不单独留常量）；最多 60 次是防呆上限（小 ROI 的 OCR 可以做到
# 几十毫秒一次），免得参数被误填成 0 或很大时把一轮扫描拖成几分钟。
DEFAULT_SAMPLE_MAX_COUNT = 60
# 单次采样超过这个秒数就不再追加采样了：说明这一台机器的 OCR 比采样窗还慢
# （全屏 OCR 实测中位 5.5 秒），硬凑次数只会让一轮扫描变几十秒，
# 这时候「读一次」就是它真实的能力上限。
DEFAULT_SAMPLE_SLOW_SEC = 5.0
# 采样用的 OCR 节点：先自己做一次全屏定位，把数值框记下来，
# 之后每次只认那一小块（只有零点几秒），才有可能在一秒里读好几次。
DEFAULT_SAMPLE_OCR_NODE = "LaserSweepOcrValueB"
# 数值框外扩多少像素再送进 OCR。太小会切掉半个字，太大又退回慢路径。
DEFAULT_SAMPLE_ROI_PAD = 8

# 采样聚合时的「误读带」：以中位数为圆心，±max(绝对, |中位数|×百分比) 以内算正常，
# 带外的判为 OCR 误读、剔掉再取平均。
#
# 为什么必须在平均之前先剔：平均值的软肋就是——它会被离群值拉歪，而且拉歪之后
# 看起来仍然"平滑"。这一秒里要是混进一个 -797.20（小数点右移一位，README 里
# 记录的真实误读形态），11 次采样里有 1 次，平均值就从 -79.7 变成 -144.9，
# CSV 里就多一个彻底报废的点。所以先按中位数圈出正常簇，带外的不参与平均。
#
# 默认值和读数守卫（tolerance_abs=3.0 / tolerance_pct=1%）同量级：
#   · 正常刷新抖动 ±0.05 —— 远远在带内，全都被平均掉
#   · 认错一位数字 ±5 以上 —— 落在带外，被剔掉
# 两项取「更宽松者」和守卫一致：大数值靠百分比、小数值靠绝对值。
DEFAULT_SAMPLE_OUTLIER_ABS = 3.0
DEFAULT_SAMPLE_OUTLIER_PCT = 1.0
DEFAULT_RECORD_NODE = "LaserSweepRecord"
DEFAULT_STOP_NODE = "LaserSweepAbnormalStop"
DEFAULT_WAIT_NODE = "LaserSweepWaitSettle"

# ── 告警邮件 ──────────────────────────────────────────────────────────────
# 只用标准库（smtplib/ssl/email），打包版 Python 里已验证可用，不引第三方依赖：
# 现场机器往往连不上外网装包，而扫描一跑就是几十分钟，为了发一封邮件回滚依赖不值。
DEFAULT_NOTIFY_MODE = "auto"  # auto = 配了邮箱就发、没配就闭嘴；email = 必须发；off = 不发
DEFAULT_SMTP_PORT = 465  # 465 = 隐式 SSL（QQ/163 企业邮都认这个）
DEFAULT_SMTP_SSL = True
DEFAULT_TIMEOUT_SEC = 20.0  # 告警不能把终止流程卡住：超时就放弃
DEFAULT_NOTIFY_LIMIT = 1  # 一次扫描最多发几封（终止只会发生一次，这是防重入）
DEFAULT_MAIL_SUBJECT_PREFIX = "[激光扫描告警]"

# ── 「邮件通知」任务里的那份设置（2026-10-28）──────────────────────────────
# 邮件输入框只保留在「邮件通知」任务里；扫描任务的告警与「邮件测试」按钮
# 都不再各自带一份邮件配置，而是由 agent 在运行时直接读这个任务的设置。
MAIL_TASK_NAME = "邮件通知"
MAIL_OPTION_NAME = "邮件设置"
SCAN_TASK_NAME = "激光参数扫描"

# 界面字段名 → agent 内部参数名。键名和 interface.json 里对应的 inputs 一一对应。
# 2026-10-28 起，扫描设置被拆成可折叠的 checkbox 分组；MFA 折叠时不会把
# 子 option 的 pipeline_override 传给 agent，所以这里再留一份「从任务配置读」的
# 兜底：折叠只影响界面，不影响实际参数。
SCAN_UI_KEY_MAP = {
    "起始值": "start",
    "结束值": "end",
    "步长": "step",
    "扫描点数": "count",
    "等待秒数": "wait_sec",
    "结果文件夹名": "csv_dir",
    "结果文件名": "csv_name",
    "输出总目录": "output_root",
    "跑完自动打开": "open_when_done",
    "读数校验": "enabled",
    "绝对偏差阈值": "tolerance_abs",
    "相对偏差阈值": "tolerance_pct",
    "重测等待秒数": "retry_wait",
    "连续异常次数": "retry_max",
    "异常后继续测试": "continue_on_abnormal",
    "读数异常发送邮件": "notify_enabled",
    "读数采样": "sample_enabled",
    "采样时长": "sample_window_sec",
}

# 这些值只用于「旧数据 vs 新建子 option 默认值」的取舍：
# 用户没改过的新子项会带着默认值，不能拿它覆盖老版本里同一字段的自定义值。
SCAN_DEFAULT_VALUES = {
    "start": "20",
    "end": "30",
    "step": "1",
    "count": "0",
    "wait_sec": "10",
    "csv_dir": "",
    "csv_name": "",
    "output_root": "output",
    "open_when_done": "true",
    "enabled": "true",
    "tolerance_abs": "3.0",
    "tolerance_pct": "1",
    "retry_wait": "3",
    "retry_max": "5",
    "continue_on_abnormal": "true",
    "notify_enabled": "true",
    "sample_enabled": "true",
    "sample_window_sec": "1",
}

# 界面字段名 → agent 内部参数名。键名和 interface.json 的「邮件设置」inputs 一一对应。
MAIL_UI_KEY_MAP = {
    "发送完成通知": "enabled",
    "收件邮箱": "mail_to",
    "SMTP账号": "smtp_user",
    "SMTP密码": "smtp_password",
    "SMTP服务器": "smtp_host",
    "SMTP端口": "smtp_port",
    "邮件主题前缀": "mail_subject_prefix",
    "通知方式": "notify_mode",
    "每次最多通知": "notify_limit",
}

# 一封告警信需要的全部字段。用它把「邮件设置」那一份参数整体搬进告警路径，
# 避免再出现"Guard 只拿到半个配置 → 静默不发"（2026-10-27 实测事故）。
MAIL_PARAM_KEYS = (
    "notify_mode",
    "smtp_host",
    "smtp_port",
    "smtp_ssl",
    "smtp_starttls",
    "smtp_user",
    "smtp_password",
    "mail_to",
    "mail_subject_prefix",
    "timeout_sec",
    "notify_limit",
)


# 跨节点共享的运行状态；每次 laser_sweep_begin 都会整体重置
_state: dict[str, Any] = {
    "values": [],  # ["20", "21", ..., "30"]
    "index": 0,  # 当前跑到第几组
    "csv_path": None,  # 绝对路径 Path
    "out_root": None,  # output 总目录（绝对路径 Path），收尾/按钮打开它
    "open_when_done": True,  # 跑完是否自动打开 output 文件夹
    "measured": None,  # 本轮读到的测量值（float），None 表示没读到
    "image_size": None,  # 最近一次识别用的截图尺寸 (w, h)，用于按比例算偏移
    "controls": None,  # A 软件控件定位缓存 {boxes, size, stamp, hits}，见 _remember_controls
    # ── 1 秒多采样的「数值框」缓存（见 _sample_value_by_ocr）──────────────
    # 第一轮要做一次全屏定位（贵），把数值框的位置、以及它是「锚点右侧」
    # 还是「锚点正下方」记下来；之后每秒里的那十几次采样只认这一小块。
    # 窗口没动、截图尺寸没变就一直复用，尺寸一变立刻作废（不拿旧坐标硬认）。
    "value_box": None,  # {"box": [x, y, w, h], "layout": "right"/"below", "anchor": str, "size": [w, h]}
    "sample_stats": [],  # 最近一轮采样的统计 [{count, seconds, mean, span}]，写日志/摘要用
    "sample_param": {},  # 折叠分组里读到的采样开关/时长（LaserReadValueB 兜底用）
    "scan_settings_from_task": False,  # 本次 begin 是否从任务配置读到了折叠分组设置
    # ── 读数校验相关（见 _tolerance_ok / LaserSweepGuard）────────────────
    "last_measured": None,  # 上一次读数（原始值，未判定），校验层拿它当基准
    # 只有**判定正常**的读数才追加进 history。脏数据一旦进来，后面每一组都会
    # 拿它当基准，误差会一路继承下去 —— 这正是这层保护要防的事。
    "history": [],  # list[float]，已记录的历次正常测量值
    "guard": {},  # 本轮重测状态 {strikes, reference, abnormal, ...}
    "run_start": None,  # 扫描开始的 time.monotonic()，算总耗时用
    "started_at": None,  # 扫描开始的可读时间 "2026-10-26 17:00:00"
    "alert": {},  # 最近一次告警的汇总（见 _compose_alert / _send_alert）
    # 两条独立的发信计数：告警受 notify_limit 限制（防异常刷屏），
    # 完成通知各算各的（2026-10-27 分开，否则先发测试信就把完成通知挤掉了）
    "sent_alerts": 0,  # 本次扫描已发出的**告警**封数（notify_limit 用）
    "mail_done_count": 0,  # 「邮件通知」任务已发的完成通知封数
    "mail_result": {},  # 最近一次「邮件通知 / 邮件测试」的结果，界面上可查
    "last_note": "",  # 给 CSV / 摘要用的一句话说明（异常终止时写原因）
    "guard_param": {},  # UI 传进来的校验参数（begin 存一份，guard 当兜底）
    # 界面「邮件设置」的那一份（begin 存下来）。
    # 告警路径必须用它，不能用 Guard 自己的 param —— 后者没有 smtp_*。
    # 2026-10-27 事故：不存这一份，告警永远判"缺配置"、静默不发。
    "mail_param": {},
}

# 给 B 软件找到的窗口缓存：{window_regex: (hwnd, 标题, 类名)}
_window_cache: dict[str, tuple[int, str, str]] = {}


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def _param(raw: Any) -> dict[str, Any]:
    """解析流水线传来的 custom_*_param。

    绑定层传过来的是 JSON 字符串，但也容忍已经是 dict / None 的情况。
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            print(f"[laser_sweep] 参数不是合法 JSON，已忽略: {raw!r}")
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _fmt(value: float) -> str:
    """数值转字符串：整数不带小数点，小数最多保留 6 位。"""
    rounded = round(float(value), 6)
    if abs(rounded - round(rounded)) < 1e-9:
        return str(int(round(rounded)))
    return repr(rounded)


def _agent_fingerprint() -> str:
    """报出「这次跑的到底是哪份 agent 文件」—— 路径、字节数、修改时间。

    换电脑排查时最常听到的一句话是「我替换过了呀」。日志里带上这三样，
    跟本地 `Get-Item … | Select Length, LastWriteTime` 一对就知道换没换成功。
    """
    try:
        path = Path(__file__).resolve()
        stat = path.stat()
        stamp = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
        return f"{path}（{stat.st_size} 字节，改于 {stamp}）"
    except OSError:
        return str(__file__)


def _resolve(path_like: str) -> Path:
    """把配置里的相对路径解析成绝对路径（相对 interface.json 所在目录）。

    CWD 就是 interface.json 所在目录：
      开发时是 assets/，打包后是 install/。
    所以 UI 上填 "output" 会落到 assets/output/ 或 install/output/。
    """
    path = Path(path_like)
    return path if path.is_absolute() else (Path.cwd() / path).resolve()


def _sanitize_name(raw: str, fallback: str = "") -> str:
    """把用户填的「文件夹名 / 文件名」洗成能安全落盘的样子。

    用户可能在「文件夹名」里填日期模板、中文、空格 —— 这些都该保留；
    但路径分隔符和 Windows 非法字符必须先换掉，否则轻则建出错层级，
    重则直接抛 OSError 让整轮扫描开不了头。

        "2026/10/16 测试"  → "2026_10_16 测试"
        "a:b"              → "a_b"
        "  "               → fallback
        "CON"              → "CON_"（Windows 保留设备名，建目录会失败）
    """
    name = _ILLEGAL_NAME_RE.sub("_", str(raw or "").strip())
    # 结尾的点和空格 Windows 会悄悄吃掉，自己去掉免得日志里的路径和实际不符
    name = name.rstrip(". ").strip()
    if not name:
        return fallback
    if name.upper() in _RESERVED_NAMES:
        name += "_"
    return name


def _expand_time_format(raw: str) -> str:
    """把名称里的时间占位符换成当前时间。

    支持 strftime 写法（%%Y%%m%%d 等），也认几个顺手的中文别名，
    免得用户为了加个日期还要去查 strftime 表：

        {date}       → 20261016
        {time}       → 110530
        {datetime}   → 20261016_110530
    """
    text = str(raw or "")
    now = datetime.now()
    aliases = {
        "{date}": now.strftime("%Y%m%d"),
        "{time}": now.strftime("%H%M%S"),
        "{datetime}": now.strftime("%Y%m%d_%H%M%S"),
    }
    for key, value in aliases.items():
        text = text.replace(key, value)

    # strftime：先把 %% 保护起来，免得模板里真写了百分号被当格式符
    sentinel = "\x00PCT\x00"
    text = text.replace("%%", sentinel)
    try:
        text = now.strftime(text)
    except ValueError:
        # 用户写了 %Q 这种非法格式符 —— 保留原文，别让整轮扫描开不了头
        print(f"[laser_sweep] 名称里的时间格式无法解析：{raw!r}，按原样使用")
    return text.replace(sentinel, "%")


def _default_csv_stem(
    start: Optional[float] = None,
    end: Optional[float] = None,
    total: Optional[int] = None,
) -> str:
    """默认结果文件名：时间_起始值_终止值_总点数（2026-10-16 主人要求）。

    例：20→30 扫 11 个点 → 20261016_143435_20_30_11

    这样做的好处是**光看文件名就知道这次扫了什么**：
    翻 output/ 里的一堆结果时，不必逐个打开 CSV 去对照参数。

    · 时间取到秒（%%Y%%m%%d_%%H%%M%%S），与文件夹名一致的做法；
    · 「总点数」用的是**实际**组数（步长模式下由区间算出来），
      不是用户填的那个 count —— 名字要对得上 CSV 里的真实行数；
    · 三个参数拿不到时（比如「打开输出文件夹」只关心目录）就只留时间，
      不会拼出一串 None。
    """
    bits = [datetime.now().strftime("%Y%m%d_%H%M%S")]
    if start is not None:
        bits.append(_fmt(start))
    if end is not None:
        bits.append(_fmt(end))
    if total is not None:
        bits.append(str(int(total)))
    return "_".join(bits)


def _resolve_output_paths(
    param: dict[str, Any],
    start: Optional[float] = None,
    end: Optional[float] = None,
    total: Optional[int] = None,
) -> tuple[Path, Path]:
    """算出「本次扫描的结果文件」和「output 总目录」，返回 (csv_path, out_root)。

    目录结构（2026-10-16 起）：
        软件目录/output/<本次文件夹名>/<文件名>.csv

    读的参数（都来自 UI「扫描设置」）：
        csv_dir      本次结果的文件夹名，默认「系统时间_output」
                     留空 → 用默认名；填了绝对路径 → 直接当总目录用（兼容老配置）
        csv_name     结果文件名。留空 → 用默认名「时间_起始值_终止值_总点数」
                     （见 _default_csv_stem）；没写 .csv 会自动补
        output_root  总目录名，默认 output（相对 interface.json 所在目录）

    start / end / total 是本次扫描的区间与实际组数，只用于拼默认文件名；
    拿不到（None）时默认名退化成「只有时间」。

    兼容老行为：csv_dir 以前是「结果直接落在哪个文件夹」。
    现在它的语义变成「output 下面的子文件夹名」，
    但填绝对路径时仍旧当成总目录（老用户写 D:\\数据 不会突然跑到软件目录下）。
    """
    # ── 结果文件名 ────────────────────────────────────────────────────────
    stem = str(param.get("csv_name") or "").strip().strip('"').strip("'")
    # ── 本次结果的文件夹名 ────────────────────────────────────────────────
    run_dir = str(param.get("csv_dir") or "").strip().strip('"').strip("'")

    # ── 老写法 csv_path：直接给完整文件路径 ────────────────────────────────
    # 只在「新参数一个都没填」时才认它 —— 否则用户换到新界面后，
    # 旧的 csv_path 会把新设置悄悄顶掉。
    if not (run_dir or stem):
        legacy = str(param.get("csv_path") or "").strip().strip('"').strip("'")
        if legacy:
            target = _resolve(legacy)
            print(f"[laser_sweep] 用的是旧版 csv_path 参数：{target}")
            return target, target.parent

    # 用户填了就照用户的写（同样支持 %Y%m%d / {datetime} 这类时间占位符）；
    # 留空才用「时间_起始值_终止值_总点数」这个默认名。
    if stem:
        filename = _sanitize_name(_expand_time_format(stem), DEFAULT_CSV_STEM)
    else:
        filename = _default_csv_stem(start, end, total)
    filename = _sanitize_name(filename, DEFAULT_CSV_STEM)
    if not filename.lower().endswith(".csv"):
        filename += ".csv"

    run_name = _sanitize_name(_expand_time_format(run_dir), "")
    if not run_name:
        run_name = datetime.now().strftime(DEFAULT_RUN_DIR_FORMAT)
    # 万一用户填的模板把名字洗没了（比如全是非法字符），兜到默认时间名
    run_name = _sanitize_name(run_name, datetime.now().strftime(DEFAULT_RUN_DIR_FORMAT))

    # ── 总目录 ────────────────────────────────────────────────────────────
    root_raw = str(param.get("output_root") or "").strip().strip('"').strip("'")
    root_path = Path(root_raw) if root_raw else Path(OUTPUT_ROOT_NAME)

    # 用户把「本次文件夹名」填成了绝对路径 → 那是老写法的总目录，别往 output/ 里套
    if Path(run_dir).is_absolute() or (len(run_dir) > 1 and run_dir[1] == ":"):
        out_root = _resolve(run_dir)
        return out_root / filename, out_root

    out_root = _resolve(str(root_path))
    return out_root / run_name / filename, out_root


def _open_folder(path: Path) -> bool:
    """在资源管理器里打开这个文件夹（不存在就先建出来）。

    为什么不用 os.startfile：它只在 Windows 有，而且对「文件还是目录」不敏感。
    这里统一成「先确保目录存在，再按平台选一个打开命令」，
    顺带把失败原因打出来 —— 静默失败的话用户只会觉得"按钮没反应"。
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"[laser_sweep] 创建文件夹失败：{path}（{exc}）")
        return False

    target = str(path)
    try:
        if sys.platform == "win32":
            # explorer.exe 对含空格/中文的路径比 os.startfile 更稳，
            # 而且它打开目录时不会弹"选择打开方式"。
            import subprocess

            subprocess.Popen(["explorer", target])
        elif sys.platform == "darwin":
            import subprocess

            subprocess.Popen(["open", target])
        else:
            import subprocess

            subprocess.Popen(["xdg-open", target])
    except OSError as exc:
        print(f"[laser_sweep] 打开文件夹失败：{target}（{exc}）")
        return False

    print(f"[laser_sweep] 已在资源管理器中打开：{target}")
    return True


def _state_file() -> Path:
    """记录「最近一次实际使用的 output 总目录」的小文件。

    为什么需要它：界面上「打开输出文件夹」是个独立任务，
    如果用户把「输出总目录」改成了绝对路径（比如 D:\\扫描数据），
    按钮理应打开那个目录 —— 但它不一定拿得到「扫描设置」里的值
    （两个任务的 option 不共享，见 interface.json 的说明）。

    所以每次扫描开始时把真实用到的总目录写在这里，
    按钮优先读它；读不到（从没跑过、或换了台机器）就退回默认 output/。
    文件故意放在软件目录下、名字带 laser_sweep_ 前缀，一眼能看出是本项目生成的。
    """
    return _resolve("laser_sweep_output.json")


def _remember_output_root(root: Path, csv_path: Optional[Path] = None) -> None:
    """记下「最近一次实际使用的输出总目录」，顺手记下这次的 CSV 路径。

    2026-10-28 新增 last_csv：新任务「绘制曲线图」要默认画**最近导出的单个 CSV**。
    只记 output_root 的话，它得在目录里按修改时间猜 —— 如果用户刚手动往
    output 里放了一份旧数据，猜出来的就可能不是这次扫描的结果。直接记下
    begin 时算好的 csv_path，任务一读就知道该画哪一份，和「最近一次扫描」严格对应。
    """
    payload: dict[str, Any] = {"output_root": str(root)}
    if csv_path is not None:
        payload["last_csv"] = str(csv_path)
    try:
        _state_file().write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        # 记不住不影响扫描本身，只是按钮可能退回默认目录
        print(f"[laser_sweep] 提示：没能记下输出目录（{exc}），「打开输出文件夹」将使用默认 output/")


def _recall_output_root() -> Optional[Path]:
    path = _state_file()
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    raw = str(data.get("output_root") or "").strip()
    return Path(raw) if raw else None


def _recall_last_csv() -> Optional[Path]:
    """读回「最近一次扫描的 CSV 路径」（老状态文件里没有这个字段就返回 None）。

    注意：这里**不检查文件是否存在** —— 调用方要负责判断，因为老文件可能
    已经被使用者挪走/改名。返回 None 时才表示"状态文件根本没有这条记录"。
    """
    path = _state_file()
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    raw = str(data.get("last_csv") or "").strip()
    return Path(raw) if raw else None


def _resolve_csv_path(param: dict[str, Any]) -> Path:
    """兼容老接口：只要结果 CSV 的路径（见 _resolve_output_paths）。"""
    return _resolve_output_paths(param)[0]


def _apply_wait_seconds(context: Context, param: dict[str, Any]) -> bool:
    """把「每轮等待秒数」换算成流水线节点的 post_delay（毫秒）。

    UI 上让用户填秒更直观，但 pipeline 用的是毫秒，
    在扫描开始前覆盖一次 LaserSweepWaitSettle.post_delay 即可。
    """
    raw = param.get("wait_sec")
    if raw is None or str(raw).strip() == "":
        return True

    node = str(param.get("wait_node") or "LaserSweepWaitSettle")
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        print(f"[laser_sweep] 「等待秒数」不是数字：{raw!r}，沿用流水线里的默认值")
        return True

    if seconds < 0:
        print(f"[laser_sweep] 「等待秒数」不能为负：{seconds}，沿用默认值")
        return True

    millis = int(round(seconds * 1000))
    if not context.override_pipeline({node: {"post_delay": millis}}):
        print(f"[laser_sweep] 覆盖 {node}.post_delay 失败")
        return False

    print(f"[laser_sweep] 每轮等待：{_fmt(seconds)} 秒（{millis} 毫秒）")
    return True


# --------------------------------------------------------------------------- #
# A 软件控件定位缓存
#
# 一轮扫描里的两次全屏 OCR（找 Temperature、找 Setting）认的是同一屏、同一行的
# 三个框，纯属重复劳动。这里把它们缓存起来，让第二次以及后续每一轮都直接复用：
#
#     _remember_controls()  OCR 成功后把三个框一起存下来
#     _controls_fresh()     判断缓存还能不能直接用（开关 / 尺寸 / 时长 / 强制重定位）
#     _pad_roi()            缓存失效后重定位时，用上次的框外扩一圈当 ROI 先试一次
#
# 读参数的约定（都写在节点的 custom_recognition_param 里）：
#     use_cache        true（默认）/ false   关掉缓存，退回「每次都 OCR」的老行为
#     cache_ttl        秒，默认 600          缓存最长用多久；0 = 不因时间失效
#     relocate_every   次数，默认 0          每复用多少次强制重新 OCR 一次；0 = 不强制
#     hint_roi_pad     像素，默认 240        重新定位时先用「上次位置外扩」这个小 ROI 试；
#                                            0 = 关掉，直接全屏
# 安全兜底：截图尺寸一变（窗口被挪/被缩放）缓存立刻作废，不会拿着旧坐标去点。
# --------------------------------------------------------------------------- #
def _flag(value: Any, default: bool = True) -> bool:
    """把流水线传来的开关解析成 bool，字符串 "false"/"0"/"off" 都当关。"""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(value)


def _number(value: Any, default: float) -> float:
    """把流水线传来的数字解析成 float，认不出来就用默认值。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _controls_snapshot() -> Optional[dict[str, Any]]:
    """取出缓存字典本身（可能已过期），没有就返回 None。"""
    cached = _state.get("controls")
    return cached if isinstance(cached, dict) else None


def _controls_fresh(param: dict[str, Any], size: Optional[tuple[int, int]]) -> Optional[dict[str, Any]]:
    """缓存能不能直接用？能用就返回缓存（并累加复用次数），否则返回 None。

    四种作废条件各打印一行原因，真机上对着日志就能知道它为什么又去 OCR 了。
    """
    cached = _controls_snapshot()
    if cached is None:
        return None

    if not _flag(param.get("use_cache"), True):
        print("[laser_sweep]   ♻️ use_cache=false，每次都重新 OCR 定位")
        return None

    cached_size = cached.get("size")
    if size and cached_size and tuple(cached_size) != tuple(size):
        print(
            f"[laser_sweep]   截图尺寸变了（缓存 {tuple(cached_size)} → 现在 {tuple(size)}），"
            "定位缓存作废，重新 OCR"
        )
        return None

    ttl = _number(param.get("cache_ttl"), DEFAULT_CACHE_TTL)
    age = time.monotonic() - float(cached.get("stamp") or 0.0)
    if ttl > 0 and age > ttl:
        print(f"[laser_sweep]   定位缓存已过期（{age:.0f} 秒 > {ttl:.0f} 秒），重新 OCR")
        return None

    limit = int(_number(param.get("relocate_every"), DEFAULT_RELOCATE_EVERY))
    if limit > 0 and int(cached.get("hits") or 0) >= limit:
        print(f"[laser_sweep]   按 relocate_every={limit} 强制重新 OCR 一次（防止长跑慢慢跑偏）")
        cached["hits"] = 0
        return None

    cached["hits"] = int(cached.get("hits") or 0) + 1
    cached["age"] = age
    return cached


def _forget_controls(reason: str = "") -> None:
    """丢掉缓存（定位失败时用，免得下次还拿旧坐标硬试）。"""
    if _state.get("controls") is None:
        return
    _state["controls"] = None
    print(f"[laser_sweep]   定位缓存已清空" + (f"：{reason}" if reason else ""))


def _remember_controls(
    anchor_box: Any,
    target_box: Any,
    input_box: Any,
    size: Optional[tuple[int, int]],
) -> None:
    """把这一屏 OCR 出来的三个框一起存下来，供后续每一轮复用。

    三个框对应节点的 return_box：
        anchor → 参数标签（"Temperature:"）
        target → Setting 按钮
        input  → 标签与按钮之间那个输入框
    """
    boxes: dict[str, list[int]] = {}
    for key, box in (("anchor", anchor_box), ("target", target_box), ("input", input_box)):
        if box is None or len(tuple(box)) != 4:
            continue
        x, y, w, h = (int(v) for v in box)
        if w > 0 and h > 0:
            boxes[key] = [x, y, w, h]
    if not boxes:
        return

    _state["controls"] = {
        "boxes": boxes,
        "size": tuple(size) if size else None,
        "stamp": time.monotonic(),
        "hits": 0,
    }
    print(
        "[laser_sweep]   📌 已缓存 A 软件控件位置："
        + "；".join(f"{key}={box}" for key, box in boxes.items())
    )


def _pad_roi(
    boxes: list[list[int]],
    pad: int,
    size: Optional[tuple[int, int]],
) -> list[int]:
    """把若干框合并成「一个外扩 pad 像素」的 ROI，并裁到图像范围内。

    用于缓存失效后的重新定位：窗口没动的话，控件还在老位置附近，
    认这一小块比认整屏快得多（实测全屏 OCR 中位 4.57 秒，小 ROI 只要零点几秒）。
    """
    xs = [int(b[0]) for b in boxes]
    ys = [int(b[1]) for b in boxes]
    x1s = [int(b[0]) + int(b[2]) for b in boxes]
    y1s = [int(b[1]) + int(b[3]) for b in boxes]

    x0, y0 = min(xs) - pad, min(ys) - pad
    x1, y1 = max(x1s) + pad, max(y1s) + pad
    if size:
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(int(size[0]), x1), min(int(size[1]), y1)
    return [x0, y0, max(0, x1 - x0), max(0, y1 - y0)]


def _build_values(
    start: float,
    end: float,
    step: float,
    count: Optional[int] = None,
) -> list[str]:
    """生成参数序列，两种模式（用整数计数，避免浮点累加误差）：

      · 按步长（默认，count 为空或 <= 0）：20, 21, ... 30
            n = round((end - start) / step) + 1
      · 按总点数（count > 0，**优先于步长**）：
            步长由「区间 ÷ (count - 1)」反算，第 i 个点 = start + i × 步长，
            用 i × 步长 而不是逐个累加，误差不会滚雪球。

    总点数模式的约定（2026-10-14 按主人要求定下）：

      ① count == 1：整个扫描只有起始值那一个点（不是非法输入，别报错）。
      ② 区间除不尽（如 20→30 取 4 个点，间隔 10/3）时，**步长按反算值严格用**，
         不做任何"凑整"美化；点数照样走满，最后一个点落在结束值上：
             20 / 23.333333 / 26.666667 / 30
         想要每个点都是"整齐"的数，就把 区间 ÷ (点数-1) 凑成整数
         （例如 20→30 想每步 2，就填 6 个点）。

    浮点坑记一笔：count 模式下用 span/(count-1) 反算步长时，
    不能简单地把「第 count 个点」直接写成 end —— 有的区间反算后
    那个点的位置会略微越过终点，所以这里统一「先按步长生成、再裁掉越界尾巴」，
    并把与终点几乎重合的最后一点吸附成终点（20→30 填 4 点收尾就是干净的 30，
    步长 0.1 也不会收在 20.999999）。自检见 tools/check_pickers.py ⑩。
    """
    span = end - start

    if count is not None and count > 0:
        if count == 1:
            return [_fmt(start)]
        step = span / (count - 1)

    # ⚠️ 别用「直接算 count 个点、最后一点写死成 end」的写法：
    #    浮点反算出来的步长会让少数区间在末端多出一个点（越过 end），
    #    也会让本该收在终点的序列停成 20.999999 这种脏数。
    #    统一走「按步长生成 → 裁掉越界尾巴 → 末端吸附」这三步。
    n = int(round(span / step)) + 1
    values = [start + i * step for i in range(max(n, 0))]
    while len(values) > 1 and values[-1] > end:
        values.pop()
    if count is not None and count > 1 and values and values[-1] != end:
        # 只在「最后那个点的位置本来就是终点」时才吸附（容差按区间取，避免误吸）
        if abs(values[-1] - end) <= abs(span) * 1e-9:
            values[-1] = end
    return [_fmt(v) for v in values]


def _resolve_count(param: dict[str, Any]) -> list[str]:
    """读「总点数」参数：认得出正整数就当点数模式，否则当没填。

    写 0（默认）/ 留空 / 写错 —— 一律退回「按步长算」的老行为，
    这样老配置和热修复包拿到的结果完全不变。
    """
    raw = param.get("count")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return []
    try:
        value = float(raw)
    except (TypeError, ValueError):
        print(f"[laser_sweep] 「扫描点数」不是数字：{raw!r}，已按「按步长扫描」处理")
        return []
    if value != int(value):
        print(f"[laser_sweep] 「扫描点数」只能填整数：{raw!r}，已按「按步长扫描」处理")
        return []
    return [int(value)]


def _center(box: tuple[int, int, int, int]) -> tuple[float, float]:
    """矩形中心点。"""
    x, y, w, h = box
    return (x + w / 2.0, y + h / 2.0)


def _image_size_of(image: Any) -> Optional[tuple[int, int]]:
    """从截图里读出 (宽, 高)，读不到就 None —— 定位缓存的尺寸校验要用它。"""
    shape = getattr(image, "shape", None)
    if shape is not None and len(shape) >= 2:
        return int(shape[1]), int(shape[0])
    return None


def _remember_image_size(image: Any) -> None:
    """记下当前截图的尺寸，供「按比例算偏移」使用。

    直接取识别回调收到的 argv.image 是最省事的——它一定存在，
    不用去依赖 controller.cached_image 之类的接口。
    """
    size = _image_size_of(image)
    if size is not None:
        _state["image_size"] = size


def _distance_sq(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """两个矩形中心点距离的平方（只用来比大小，不用开方）。"""
    ax, ay = _center(a)
    bx, by = _center(b)
    return (ax - bx) ** 2 + (ay - by) ** 2


def _rect_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> tuple[float, float]:
    """两个矩形的水平 / 垂直重叠长度（没有重叠就是 0）。"""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    overlap_x = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    overlap_y = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    return overlap_x, overlap_y


def _overlap_ratio(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """交集面积 ÷ 较小那个框的面积，用来判断「这俩是不是同一处文字」。"""
    overlap_x, overlap_y = _rect_overlap(a, b)
    smaller = min(a[2] * a[3], b[2] * b[3])
    return (overlap_x * overlap_y) / smaller if smaller > 0 else 0.0


def _edge_gap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """两个框「边缘到边缘」的最短距离（贴在一起就是 0）。

    有了它，「右边的数值」和「下面的数值」才能放在同一把尺子上比远近 ——
    水平间距和垂直间距本来没法直接比大小。
    """
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    dx = max(0.0, max(ax - (bx + bw), bx - (ax + aw)))
    dy = max(0.0, max(ay - (by + bh), by - (ay + ah)))
    return (dx * dx + dy * dy) ** 0.5


def _pick_right_side(
    anchor_box: tuple[int, int, int, int],
    candidates: list[tuple[tuple[int, int, int, int], str, float]],
    gap_tolerance: float = 6.0,
    overlap_ratio: float = 0.5,
) -> list[tuple[float, tuple[int, int, int, int], str, float]]:
    """挑「锚点右边、同一行」的候选，按水平距离从近到远排好。

    这一条是 2026-09-22 换电脑后踩出来的坑：B 软件的标签本身含数字
    （"功率CH1"），宽松正则会在**标签自己**身上匹配出 "1"，
    中心距离 = 0，于是永远选中标签自己，测量值恒定 = 1。
    所以在这里立三条硬规矩：

        ① 不许和锚点重叠 —— 直接掐掉自匹配；
        ② 中心必须在锚点中心右侧；
        ③ 必须和锚点在同一水平带（垂直方向重叠够多）。

    真正的目标 "-79.720dBm" 正好满足全部三条：
        锚点 (748,83,89,25) → 右边界 837；候选 (850,84,132,24) → 起点 850，y 几乎完全重合。
    """
    ax, ay, aw, ah = anchor_box
    anchor_cx = ax + aw / 2.0
    anchor_right = ax + aw

    picked: list[tuple[float, tuple[int, int, int, int], str, float]] = []
    for box, text, value in candidates:
        bx, by, bw, bh = box
        if _overlap_ratio(anchor_box, box) > 0.3:
            continue  # ① 自匹配 / 压在锚点上的文字
        if bx + bw / 2.0 <= anchor_cx:
            continue  # ② 在左边
        if bx < anchor_right - gap_tolerance:
            continue  # ② 起点跑到锚点里去了
        _, overlap_y = _rect_overlap(anchor_box, box)
        if overlap_y < min(ah, bh) * overlap_ratio:
            continue  # ③ 不在同一行
        picked.append((bx - anchor_right, box, text, value))

    picked.sort(key=lambda item: item[0])
    return picked


def _pick_below(
    anchor_box: tuple[int, int, int, int],
    candidates: list[tuple[tuple[int, int, int, int], str, float]],
    gap_tolerance: float = 6.0,
    overlap_ratio: float = 0.5,
) -> list[tuple[float, tuple[int, int, int, int], str, float]]:
    """挑「锚点正下方」的候选，按垂直距离从近到远排好。

    旧 B 软件（GaussianBeam）是「标签在上、数值在下」的布局
    （"Wavelength" 下面一行就是 "1061 nm"），所以右侧那套规则对它不适用，
    这里补一套上下布局的规则，两边都能用。
    """
    ax, ay, aw, ah = anchor_box
    anchor_cy = ay + ah / 2.0
    anchor_bottom = ay + ah

    picked: list[tuple[float, tuple[int, int, int, int], str, float]] = []
    for box, text, value in candidates:
        bx, by, bw, bh = box
        if _overlap_ratio(anchor_box, box) > 0.3:
            continue
        if by + bh / 2.0 <= anchor_cy:
            continue  # 在上方
        if by < anchor_bottom - gap_tolerance:
            continue
        overlap_x, _ = _rect_overlap(anchor_box, box)
        if overlap_x < min(aw, bw) * overlap_ratio:
            continue  # 不在同一列
        picked.append((by - anchor_bottom, box, text, value))

    picked.sort(key=lambda item: item[0])
    return picked


def _input_box_between(
    anchor_box: tuple[int, int, int, int],
    button_box: tuple[int, int, int, int],
    hits: list[tuple[tuple[int, int, int, int], str]],
) -> tuple[int, int, int, int]:
    """用同一屏 OCR 到的真实框，夹出「标签」与「按钮」之间那个输入框的位置。

    A 软件每一行都是 [标签] [输入框] [Setting 按钮] 的排法。
    以前输入框位置是「标签中心 + 图像宽度 × 固定比例」算出来的，
    换台电脑窗口尺寸一变就偏了（2026-09-22 换机后点不到输入框）。
    现在改成实测算：

        ① 标签右边界 ~ 按钮左边界之间如果有文字（通常就是输入框里的当前值），
           直接拿那块文字的框 —— 它就是输入框内容，点在它身上必然准；
        ② 中间什么都没有（输入框是空的）就取两者中点，高度取标签高度。

    全程只用这一屏 OCR 出来的框，不依赖任何写死的像素或比例，
    窗口怎么缩放、DPI 怎么变都跟得上。
    """
    ax, ay, aw, ah = anchor_box
    bx, by, bw, bh = button_box

    left = ax + aw  # 标签右边界
    right = bx  # 按钮左边界
    band_top = min(ay, by) - 6
    band_bottom = max(ay + ah, by + bh) + 6

    if right <= left:
        # 说明这一屏的「标签 / 按钮」不是左右排布（比如按钮跑到标签上面去了），
        # 夹逼法失效，退回按钮左侧一小块，至少还在同一行
        fallback = (int(bx - max(48, bw)), int(by), int(max(48, bw)), max(1, int(bh)))
        print(f"[laser_sweep]   标签/按钮不是左右排布，退回按钮左侧 {fallback}")
        return fallback

    # ① 中间夹着的文字 = 输入框里的当前值
    inner: list[tuple[int, tuple[int, int, int, int], str]] = []
    for box, text in hits:
        cx, cy, cw, ch = box
        if cx < left - 4 or cx + cw > right + 4:
            continue  # 不在标签与按钮之间
        if not (band_top <= cy + ch / 2.0 <= band_bottom):
            continue  # 不在同一行
        if not re.search(r"\d", text):
            continue  # 输入框里应该是数字，纯文字多半是别的标签
        inner.append((cw, box, text))

    if inner:
        inner.sort(key=lambda item: -item[0])  # 最宽的那个最像输入框内容
        _, box, text = inner[0]
        print(f"[laser_sweep]   标签与按钮之间命中 {text!r} @ {box}，按它定位输入框")
        return box

    # ② 输入框是空的，取中点
    mid_x = left + (right - left) / 2.0
    center_y = (ay + ah / 2.0 + by + bh / 2.0) / 2.0
    width = max(1, int(right - left))
    height = max(1, int(max(ah, bh)))
    box = (
        int(round(mid_x - width / 2.0)),
        int(round(center_y - height / 2.0)),
        width,
        height,
    )
    print(
        f"[laser_sweep]   标签右边界 {int(left)} ~ 按钮左边界 {int(right)}"
        f" 之间没有文字，取中点作为输入框 {box}"
    )
    return box


def _collect_hits(results: Any) -> list[tuple[tuple[int, int, int, int], str]]:
    """把 OCR 结果列表转成 [(box, text), ...]，忽略没有位置的项。"""
    hits: list[tuple[tuple[int, int, int, int], str]] = []
    for item in results or []:
        box = getattr(item, "box", None)
        if box is None:
            continue
        hits.append((tuple(box), str(getattr(item, "text", "") or "")))
    return hits


def _ocr_hits(
    context: Context,
    image: Any,
    node: str,
    wanted: list[str],
    roi: Any,
    only_rec: bool,
) -> list[tuple[tuple[int, int, int, int], str]]:
    """跑一次指定 OCR 节点，返回全部命中结果 [(box, text), ...]。"""
    override: dict[str, Any] = {"only_rec": bool(only_rec)}
    if roi:
        override["roi"] = roi
    if wanted:
        override["expected"] = list(wanted)

    reco = context.run_recognition(node, image, pipeline_override={node: override})
    if reco is None or not reco.hit:
        return []

    # 优先用被 expected 过滤过的结果；万一 OCR 把字认岔了导致全被滤掉，
    # 就退回未过滤的结果，由调用方自己再筛一遍
    for attr in ("filtered_results", "all_results", "best_result"):
        hits = _collect_hits(
            [getattr(reco, attr)] if attr == "best_result" else getattr(reco, attr, None)
        )
        if hits:
            return hits
    return []


def _match_text(
    hits: list[tuple[tuple[int, int, int, int], str]],
    wanted: list[str],
) -> list[tuple[tuple[int, int, int, int], str]]:
    """按关键词做一次不区分大小写的包含匹配。"""
    if not wanted:
        return hits
    needles = [w.lower() for w in wanted]
    return [(box, text) for box, text in hits if any(n in text.lower() for n in needles)]


def _anchor_texts(param: dict[str, Any], key: str = "anchor_text") -> list[str]:
    """把锚点词读成列表：既支持 "Temperature" 也支持 ["功率CH1", "Wavelength"]。

    写成数组的好处是**一台配置能同时伺候两套软件**：换电脑/换 B 软件之后
    标签词不一样，把新词加进数组即可，不用删掉旧的再改回来。
    """
    raw = param.get(key)
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw).strip()
    return [text] if text else []


def _short_hits(
    hits: list[tuple[tuple[int, int, int, int], str]],
    max_chars: int,
    label: str = "",
) -> list[tuple[tuple[int, int, int, int], str]]:
    """只留「短文本」，把日志行那种长句子挡在门外。

    2026-09-22 换电脑后连踩两回，根子都在这里：

      · A 软件下方有一行提示
        `[2026.09.22-15:49:35] Tips：Setting TEC Temperature Successfully!`
        它**包含** "Temperature"，于是冒充成参数标签、还把配对逻辑带偏，
        最后点到 (228, 420) 这种莫名其妙的位置；
      · B 软件下方是日志区，OCR 认出
        `Start Wavelength 1548.000000rm not in range[...],please check!`
        它**包含** "Wavelength"，冒充成锚点之后，紧挨着它下一行的
        `code=-1073807265, VISA Write in ...`（边缘间距 1px）就被当成了数值。

    真标签、真数值都是短文本，所以按字数直接卡一刀最省事。
    """
    if max_chars <= 0:
        return hits
    kept = [(box, text) for box, text in hits if len(text.strip()) <= max_chars]
    if label and len(kept) != len(hits):
        print(f"[laser_sweep]   {label}：滤掉 {len(hits) - len(kept)} 条超过 {max_chars} 字的长文本")
    return kept


def _digit_ratio(text: str) -> float:
    """数字字符占全文的比例，用来判断「这段话像不像一个数值」。

        '-79.720dBm'                          → 6/10 ≈ 0.60  ✔
        '1061 nm'                             → 4/7  ≈ 0.57  ✔
        'code=-1073807265, VISA Write in ...' → 10/90 ≈ 0.11 ✘ 一眼是日志
    """
    stripped = text.strip()
    if not stripped:
        return 0.0
    return sum(ch.isdigit() for ch in stripped) / len(stripped)


def _choose_anchor_by_hint(
    anchors: list[tuple[tuple[int, int, int, int], str]],
    hints: list[tuple[tuple[int, int, int, int], str]],
) -> tuple[tuple[int, int, int, int], str]:
    """多个同名标签里，挑「离参照词最近」的那一个。

    参照词可能命中多处（标题栏、菜单、提示里都写了同一个词），
    所以比的是「到这个词最近那一处的距离」。

    实测场景（A 软件低噪声驱动调试软件）：
        左侧板 "Laser → Temperature"        @ (40, 417)
        Module Config 面板里的 "Temperature:" @ (497, 293)   ← 要的是这个
        near_text = "Module Config"（面板标题，约在 y≈40 上方）
    取距离最近的那个即可，换分辨率也不用重配。
    """

    def gap(item: tuple[tuple[int, int, int, int], str]) -> float:
        return min(_distance_sq(item[0], hint_box) for hint_box, _ in hints)

    return min(anchors, key=gap)


def _append_row(measured: Optional[float]) -> bool:
    """把当前这一组结果追加写入 CSV。

    measured 是**这一组已经合成好的值**：1 秒多点采样那套逻辑会先把十几次读数
    的平均数算出来再交过来（见 _aggregate_samples），所以这里写的永远是"一个数"，
    不会因为采样把一行拆成十几行 —— 行号与设定参数一一对应的约定不变。
    """
    csv_path = _state.get("csv_path")
    values, index = _state["values"], _state["index"]

    if csv_path is None or index >= len(values):
        print("[laser_sweep] 状态未初始化或索引越界，无法写入 CSV")
        return False

    # 这一行有没有"需要人看一眼"的标记（2026-10-27 新增第 5 列「备注」）
    #
    # 起因：审查指出「跳变留痕只写进 _state，CSV 和摘要都没读它」——
    #      也就是说我在日志里承诺的"摘要会列出跳变"根本没落地，
    #      可疑值仍然以普通有效数据的样子交付出去。
    # 现在：走过「稳定但可疑 → 判为真实跳变」那条路的行，备注写 `跳变(自 -14.68)`；
    #      连续异常判废写空值时的原因也写在这里。留空 = 常规读数，无任何可疑标记。
    #      四列顺序保持不动（Excel 里已有的模板/图表不会错位），只在末尾追加。
    note = ""
    jumps = _state.get("jumps")
    if isinstance(jumps, list):
        for item in reversed(jumps):
            if int(item.get("index") or -1) == index + 1:
                note = "跳变(自 %s)" % _fmt(float(item.get("from") or 0))
                break
    if not note and measured is None:
        note = str(_state.get("last_note") or "读数失败")

    row = [
        index + 1,
        values[index],
        "" if measured is None else _fmt(measured),
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        note,
    ]

    try:
        # utf-8-sig 让 Excel 打开中文表头不乱码
        with Path(csv_path).open("a", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(row)
    except OSError as exc:
        print(f"[laser_sweep] 写入 CSV 失败: {exc}")
        return False

    shown = row[2] if row[2] != "" else "(空)"
    tail = f", 备注={row[4]}" if row[4] else ""
    print(f"[laser_sweep] 已记录第 {row[0]} 行：设定={row[1]}, 测量={shown}{tail}")
    return True


# --------------------------------------------------------------------------- #
# 读数校验 / 重测 / 告警（2026-10-26）
#
# 为什么要这一整套：扫描是无人值守的，一跑一两个小时。B 软件的读数有**三种安静
# 的坏数据** —— OCR 偶尔认错一位、设备还在爬升时被截到中间值、通信掉一次包后
# 界面短暂停住旧值。它们都**不会**让识别失败，所以 on_error 兜不住，只会安安静静
# 把错的数写进 CSV；等到人工翻表发现时，前面几组已经白跑了。
#
# 所以判据只看「上一个有效读数」（不做全局拟合，免得误杀真实拐点）：
#
#     |当前 - 基准| <= 绝对阈值            → 正常
#     相对偏差 <= 相对阈值(%)              → 正常
#     任一成立即正常（取更宽松者）
#
# 为什么要「取更宽松者」：功率这类量在小信号时相对偏差天然很大（-0.5 → -0.6
# 就是 20%），只用相对阈值会天天误报；可到了几十 dBm 的量级，绝对阈值 1.0 又
# 太死，设备正常波动就能顶到。两个都卡会把真实拐点也判成异常，最后没人再信告警。
#
# 这里全部写成**不碰 Context 的纯函数**，好让 tools/check_guard.py 在没框架、
# 没邮箱的机器上把判据和正文逐条验一遍 —— 这类逻辑改错的代价是「一整轮数据报废」，
# 不能只靠真机跑一遍看看。
# --------------------------------------------------------------------------- #
def _remember_measured(value: float) -> None:
    """记下本轮读数，同时更新「上一次读数」快照。

    为什么要单独一个函数：`_state["measured"]` 在识别里有 4 处赋值
    （fixed 模式 / 无锚点 / 位置命中 / 兜底），四处都要顺带记一份
    `_state["last_measured"]` —— 复制粘贴四次迟早会漏一处，而漏掉的那一处
    恰好会让校验层拿不到基准（表现为「突然开始按第一组的值当基准」）。
    所以统一收敛到这里，识别那边只管调一次。

    ⚠️ 注意 last_measured 是「上次**读到**的值」，不是「上次**可信**的值」：
    每次读数都会覆盖它，包括读错的那一次。校验层真正的基准存在
    `_state["guard"]["reference"]` 里（只有判定通过才更新），别把两者搞混。
    """
    _state["measured"] = value
    _state["last_measured"] = value


def _elapsed_seconds() -> Optional[float]:
    """本次扫描已经跑了多少秒（用 monotonic，不受系统改时间影响）。"""
    start = _state.get("run_start")
    if start is None:
        return None
    try:
        return max(0.0, time.monotonic() - float(start))
    except (TypeError, ValueError):
        return None


def _percent_value(value: Any, default: float = DEFAULT_TOLERANCE_PCT) -> float:
    """把「相对阈值」读成百分数：界面上填什么就是多少百分比。

    界面那一条写的是「允许的相对偏差(%)」，并明确说明「填 1 就是 1%」。
    所以解析规则只有一条：**数值即百分数**。
        1    → 1%
        0.5  → 0.5%
        10   → 10%
        100  → 100%
        0    → 0（= 关掉相对判据）
        负值 → 按 0 处理并告警

    ⚠️ 2026-10-27 修掉的一个真实缺陷：这里曾经对「严格小于 1 的数」乘 100，
       试图兼容"比例写法"（0.1 当 10%）。后果是：界面填 0.5 本意是 0.5%，
       却被解释成 **50%** —— 基准 -80、当前 -70（差 12.5%）会被直接放行，
       保护层等于没开。而且用户完全看不出来，因为界面上显示的还是 0.5。

       为什么会写那段"聪明"的兼容：早期版本在文档里同时出现过「比例」和
       「百分数」两种说法，于是想在解析层把两者都吃下。但**猜用户意图**
       在数值上是有害的 —— 0.1 既可能是 0.1%，也可能是 10%，没有可靠依据。
       现在统一按界面声明解释；要表达 10% 就填 10。

    ⚠️ 0 必须单独放行：它表示「用户明确要关掉相对判据」，不能被当成
       0% 之外的任何东西，否则会把所有跨点数据全判成异常。
    """
    number = _number(value, default)
    if number == 0:
        return 0.0
    if number < 0:
        print(f"[laser_sweep] ⚠️ 相对偏差阈值不能为负（{value!r}），已按 0 处理（= 不用相对判据）")
        return 0.0
    return number


def _tolerance_ok(
    value: float,
    ref: float,
    abs_tol: float,
    pct_tol: float,
) -> tuple[bool, float, float]:
    """判断 value 相对基准 ref 是否「可接受」，返回 (是否正常, 绝对偏差, 相对百分比)。

    · 绝对偏差 = |value - ref|，永远可算；
    · 相对偏差 = |value - ref| / |ref| × 100，**ref == 0 时返回 inf** ——
      不是偷懒，而是「以 0 为基准谈百分比」在数学上没有意义（0.5 相对 0 是无穷大倍），
      这时只保留绝对阈值做判据；
    · abs_tol 与 pct_tol 任一为 0 就表示该项不生效，两者都是 0 时只有完全相等才放行。

    返回值里的两个偏差是给日志/邮件用的：判异常时要说清「差了多少」，
    只报一句「异常」没法让人判断是设备真漂了还是阈值设紧了。
    """
    got = float(value)
    base = float(ref)
    abs_diff = abs(got - base)

    # ref == 0：不除零，相对项直接判「无穷大」（等价于不生效）
    rel_pct = float("inf") if base == 0 else abs_diff / abs(base) * 100.0

    abs_tol = max(0.0, float(abs_tol))
    pct_tol = max(0.0, float(pct_tol))

    abs_ok = abs_tol > 0 and abs_diff <= abs_tol
    pct_ok = pct_tol > 0 and rel_pct <= pct_tol
    # 阈值全关（都是 0）时退回「必须完全相等」—— 否则这一层就等于没开，
    # 而用户填 0/0 的意图显然不是「什么都放行」。
    if abs_tol == 0 and pct_tol == 0:
        return (abs_diff == 0, abs_diff, rel_pct)
    return (abs_ok or pct_ok, abs_diff, rel_pct)


def _parse_notify_mode(raw: Any) -> str:
    """把 notify_mode 归一成 "auto" / "email" / "off" 三者之一。

    为什么要这么宽容：三处配置对它的写法没对齐过 ——
        my_task.json 的注释写的是 auto / off / mail，
        interface.json 的注释写的是 auto / email / off，
        而用户手改时又常写成 AUTO 或带空格的 " Email "。
    任何一个写成另一种都会被当成「未知值」→ 邮件永远发不出去，而且不报错，
    是那种「保护层拦下了扫描，但没人收到告警」的最坏组合。
    所以这里全部认，只在真正认不出时退回 auto 并**打一行提示**。
    """
    text = str(raw or "").strip().lower()
    if not text:
        return "auto"  # 留空 = 默认（配了邮箱就发，没配就闭嘴）
    if text in ("email", "mail", "smtp"):
        return "email"
    if text in ("off", "none", "no", "false", "0", "disable", "disabled"):
        return "off"
    if text == "auto":
        return "auto"
    print(f"[laser_sweep] ⚠️ 通知方式 {raw!r} 认不出来，已按 auto 处理（可选 auto / email / off）")
    return "auto"


def _split_mail_list(raw: Any) -> list[str]:
    """把收件人拆成地址列表：中英文逗号、分号、空格都当分隔符。

    为什么要宽容：用户在界面上顺手会写 "a@x.com, b@x.com" 或者用中文逗号
    "a@x.com，b@x.com" —— 而 smtplib 对后者的反应是直接发信失败。
    在这里一次拆干净，比等到发信报错再去猜要省事。
    """
    text = str(raw or "").strip()
    if not text:
        return []
    return [part for part in re.split(r"[,，;；\s]+", text) if part]


def _mail_config_file() -> Path:
    """本地告警配置文件（存 SMTP 账号/授权码）。

    ⚠️ 为什么凭据单独放一个文件，而不是写进 interface.json / my_task.json：
        那两个文件是**入库**的，而这里的 smtp_password 是邮箱授权码 ——
        拿到它等于拿到半个邮箱。写进仓库再推一次 GitHub，就等于公开泄漏。
        所以凭据只落在「本机、git 忽略」的这个文件里：
            assets/config/laser_sweep_mail.json      （开发时）
            <软件目录>/config/laser_sweep_mail.json   （打包后）
        .gitignore 里的 `config` 一行已经把它们全挡住了。

    格式（键名与「邮件设置」里的输入项一致，方便对照）：
        {
          "smtp_host": "smtp.qq.com",
          "smtp_port": 465,
          "smtp_ssl": true,
          "smtp_user": "xxx@qq.com",
          "smtp_password": "授权码",
          "mail_to": "yyy@qq.com",
          "notify_mode": "email"
        }
    """
    return _resolve("config/laser_sweep_mail.json")


# 本地配置只读一次就够：它在一次扫描里不会变，而每轮都读盘纯属浪费。
# 实测读一次 178 微秒 —— 单看不痛，但主人明确要求「没异常就迅速进入下一步」，
# 能省的就省掉，而且省下来的是每一轮都要付的钱。
# 缓存键取 (路径, mtime, 大小)：文件真被编辑过会自动重读，不会一直用旧值。
_mail_config_cache: dict[str, Any] = {"key": None, "data": {}}


def _load_mail_config() -> dict[str, Any]:
    """读本地告警配置；没有/读坏了都返回空字典，绝不抛异常。

    为什么要容忍坏文件：这层是「出事时通知」的最后一环，
    它读失败只该退化成「不发邮件」，不该让扫描本身起不来。

    为什么带缓存：这个函数在每一轮读数校验里都可能被调到，
    而配置文件在一次扫描中不会变。用 (路径, mtime, 大小) 当键，
    文件被编辑后自动失效；文件不存在时不缓存负结果 ——
    免得用户刚建好文件还要重启程序才生效。
    """
    path = _mail_config_file()
    try:
        stat = path.stat()
    except OSError:
        return {}  # 文件不存在是正常情况（很多人根本不用邮件）
    key = (str(path), stat.st_mtime_ns, stat.st_size)

    if _mail_config_cache.get("key") == key:
        return _mail_config_cache.get("data") or {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[laser_sweep] ⚠️ 本地告警配置读取失败（{exc}），已忽略：{path}")
        _mail_config_cache.update(key=key, data={})
        return {}
    if not isinstance(data, dict):
        print(f"[laser_sweep] ⚠️ 本地告警配置不是对象，已忽略：{path}")
        _mail_config_cache.update(key=key, data={})
        return {}

    _mail_config_cache.update(key=key, data=data)
    return data


# ── 从「邮件通知」任务的保存配置里读邮件设置（2026-10-28）──────────────────
# 为什么需要这层：
#   2026-10-28 起，「激光参数扫描」任务不再挂「邮件设置」option，
#   「邮件测试」任务也只剩下按钮。MFA 不会把别的任务的 option 值传给当前任务，
#   所以 agent 必须自己去读「邮件通知」任务保存下来的那一份配置。
#
# 读哪里：
#   · MFAAvalonia 多实例布局：config/instances/*.json 的 TaskItems[].option[]
#   · 老版单文件布局：config/maa_pi_config.json 的 task[].option
# 多个实例时按「填得最完整」的候选优先；都差不多时取文件较新的那个。
_MAIL_TASK_CONFIG_CACHE: dict[str, Any] = {"key": None, "data": {}}


def _mail_task_config_files() -> list[Path]:
    """列出可能保存「邮件通知」任务设置的配置文件（按修改时间新→旧）。"""
    files: list[Path] = []
    instances_dir = _resolve("config/instances")
    try:
        if instances_dir.is_dir():
            files.extend(instances_dir.glob("*.json"))
    except OSError:
        pass

    legacy = _resolve("config/maa_pi_config.json")
    try:
        if legacy.is_file():
            files.append(legacy)
    except OSError:
        pass

    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(files, key=_mtime, reverse=True)


def _normalize_mail_ui_data(data: Any) -> dict[str, Any]:
    """把界面上的中文键名转成 agent 内部参数名；只保留非空值。"""
    if not isinstance(data, dict):
        return {}
    result: dict[str, Any] = {}
    for ui_key, value in data.items():
        target = MAIL_UI_KEY_MAP.get(str(ui_key))
        if target is None or value is None:
            continue
        text = str(value).strip()
        if text:
            result[target] = text
    return result


def _extract_mail_task_candidates(root: Any) -> list[dict[str, Any]]:
    """从一份 MFA 实例配置里提取所有「邮件通知」任务的邮件设置候选。"""
    if not isinstance(root, dict):
        return []

    tasks = root.get("TaskItems")
    if not isinstance(tasks, list):
        tasks = root.get("task")
    if not isinstance(tasks, list):
        return []

    candidates: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if str(task.get("name") or "").strip() != MAIL_TASK_NAME:
            continue

        option = task.get("option")
        data: Any = None
        if isinstance(option, list):
            for item in option:
                if not isinstance(item, dict):
                    continue
                if str(item.get("name") or "").strip() != MAIL_OPTION_NAME:
                    continue
                data = item.get("data")
                if isinstance(data, dict):
                    break
        elif isinstance(option, dict):
            # 兼容老版：option 直接是 {"邮件设置": {...}} 或 {"邮件设置": {"data": {...}}}
            raw = option.get(MAIL_OPTION_NAME)
            if isinstance(raw, dict):
                data = raw.get("data") if isinstance(raw.get("data"), dict) else raw

        normalized = _normalize_mail_ui_data(data)
        if normalized:
            candidates.append(normalized)
    return candidates


def _load_mail_settings_from_email_task() -> dict[str, Any]:
    """读「邮件通知」任务里保存的那份「邮件设置」。

    返回值是 agent 内部键名（smtp_host / mail_to / ...）。读不到就返回空字典，
    绝不抛异常 —— 这个函数可能在扫描异常终止的路径上被调用，不能制造新故障。
    """
    files = _mail_task_config_files()
    cache_key: list[tuple[str, int, int]] = []
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        cache_key.append((str(path), stat.st_mtime_ns, stat.st_size))

    if _MAIL_TASK_CONFIG_CACHE.get("key") == cache_key:
        return dict(_MAIL_TASK_CONFIG_CACHE.get("data") or {})

    best: dict[str, Any] = {}
    best_score = -1
    best_mtime = -1.0

    for path in files:
        try:
            root = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        for candidate in _extract_mail_task_candidates(root):
            # 「收件邮箱 / SMTP账号 / SMTP服务器」三项最能说明这份配置是否真的可用；
            # 密码常常留空（走本地 config/laser_sweep_mail.json），不参与打分。
            score = sum(
                1 for key in ("mail_to", "smtp_user", "smtp_host") if candidate.get(key)
            )
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            if score > best_score or (score == best_score and mtime > best_mtime):
                best = candidate
                best_score = score
                best_mtime = mtime

    _MAIL_TASK_CONFIG_CACHE.update(key=cache_key, data=best)
    if best:
        print(
            "[laser_sweep] 已从「邮件通知」任务读取邮件设置："
            f"{best.get('smtp_host') or '(服务器未填)'}"
            f"，收件人 {best.get('mail_to') or '(未填)'}"
            f"，授权码{'已填' if best.get('smtp_password') else '未填'}"
        )
    return dict(best)


_SCAN_TASK_CONFIG_CACHE: dict[str, Any] = {"key": None, "data": {}}


def _collect_option_data(option: Any, out: dict[str, Any]) -> None:
    """递归收集一个 MFA option（含 sub_options）里的 data 字段。"""
    if not isinstance(option, dict):
        return
    data = option.get("data")
    if isinstance(data, dict):
        for key, value in data.items():
            if value is None:
                continue
            text = str(value).strip()
            if text != "":
                out[str(key)] = value
    sub_options = option.get("sub_options")
    if isinstance(sub_options, list):
        for sub in sub_options:
            _collect_option_data(sub, out)


def _extract_scan_task_settings(root: Any) -> list[dict[str, Any]]:
    """从一份 MFA 配置里提取「激光参数扫描」任务的全部 option 数据。

    返回的是「界面字段名 -> 值」的候选列表。兼容三种布局：
      · v1.0.18+ 的折叠分组：父 checkbox 的 data + sub_options 里的 data
      · v1.0.18 的扁平 input option：每个 option 自带 data
      · 老版 maa_pi_config.json：task[].option 是 name -> data 的字典
    """
    if not isinstance(root, dict):
        return []

    tasks = root.get("TaskItems")
    if not isinstance(tasks, list):
        tasks = root.get("task")
    if not isinstance(tasks, list):
        return []

    candidates: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if str(task.get("name") or "").strip() != SCAN_TASK_NAME:
            continue

        legacy: dict[str, Any] = {}
        nested: dict[str, Any] = {}
        option = task.get("option")

        if isinstance(option, list):
            for item in option:
                if not isinstance(item, dict):
                    continue
                data = item.get("data")
                if isinstance(data, dict):
                    for key, value in data.items():
                        if value is not None and str(value).strip() != "":
                            legacy[str(key)] = value
                sub_options = item.get("sub_options")
                if isinstance(sub_options, list):
                    for sub in sub_options:
                        _collect_option_data(sub, nested)
        elif isinstance(option, dict):
            for _name, raw in option.items():
                if not isinstance(raw, dict):
                    continue
                data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
                for key, value in data.items():
                    if value is not None and str(value).strip() != "":
                        legacy[str(key)] = value

        merged: dict[str, Any] = {}
        # 先把老版本的扁平数据放进来
        for ui_key, value in legacy.items():
            target = SCAN_UI_KEY_MAP.get(ui_key)
            if target is not None:
                merged[target] = value
        # 新子 option 的数据优先；但如果它只是新建时的默认值，而老版本有
        # 同字段的自定义值，则保留老值（避免升级后悄悄变回默认）。
        for ui_key, value in nested.items():
            target = SCAN_UI_KEY_MAP.get(ui_key)
            if target is None:
                continue
            text = str(value).strip()
            if text == "":
                continue
            old = merged.get(target)
            if old is None or text != SCAN_DEFAULT_VALUES.get(target, ""):
                merged[target] = value

        if merged:
            candidates.append(merged)

    return candidates


def _load_scan_settings_from_task() -> dict[str, Any]:
    """读取「激光参数扫描」任务保存的扫描/校验/采样设置。

    这是折叠分组的关键兜底：checkbox 收起时，子 option 不会参与
    `pipeline_override`，但配置仍然保存在任务文件里。agent 从任务配置读，
    所以「收起」只影响界面显示，不会让扫描参数退回默认值。
    """
    files = _mail_task_config_files()
    cache_key: list[tuple[str, int, int]] = []
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        cache_key.append((str(path), stat.st_mtime_ns, stat.st_size))

    if _SCAN_TASK_CONFIG_CACHE.get("key") == cache_key:
        return dict(_SCAN_TASK_CONFIG_CACHE.get("data") or {})

    best: dict[str, Any] = {}
    best_score = -1
    best_mtime = -1.0

    for path in files:
        try:
            root = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        for candidate in _extract_scan_task_settings(root):
            score = sum(1 for value in candidate.values() if str(value).strip() != "")
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            if score > best_score or (score == best_score and mtime > best_mtime):
                best = candidate
                best_score = score
                best_mtime = mtime

    _SCAN_TASK_CONFIG_CACHE.update(key=cache_key, data=best)
    return dict(best)


def _merge_scan_settings_from_task(param: dict[str, Any]) -> dict[str, Any]:
    """把「任务配置里保存的折叠分组设置」合并进节点参数。

    pipeline_override 正常会传 UI 值；但折叠时子 option 不参与合并。
    这里用任务配置兜底，保证两种状态下参数完全一致。
    """
    settings = _load_scan_settings_from_task()
    if not settings:
        _state["scan_settings_from_task"] = False
        return param
    merged = dict(param)
    merged.update({key: value for key, value in settings.items() if value is not None})
    _state["scan_settings_from_task"] = True
    print(
        "[laser_sweep] 已从任务设置读取折叠分组参数："
        f"start={merged.get('start')} end={merged.get('end')} step={merged.get('step')}"
    )
    return merged


def _resolve_mail_param(param: dict[str, Any]) -> dict[str, Any]:
    """解析「发信」需要的这一组参数：本次节点 > 「邮件通知」任务设置 > 本地配置 > 默认。

    为什么要独立成一个函数（2026-10-28 重构）：
        邮件参数只在「邮件通知」任务里填一份；扫描任务的异常告警和「邮件测试」
        按钮都没有自己的邮件输入项。agent 直接读取「邮件通知」任务的设置，
        所以界面只需要维护一处，用户也不会看到重复的输入框。

    容错原则和 _resolve_guard_param 一致：任何一项填错都只让发信失败，
    绝不能让程序抛异常 —— 它跑在「扫描即将终止」这条路径上。
    """
    local = _load_mail_config() or {}
    task_settings = _load_mail_settings_from_email_task() or {}

    def pick(ui_key: str, default: Any = "") -> Any:
        """按「节点参数 > 邮件通知任务设置 > 本地文件 > 默认」取值。

        只对**空值**兜底：界面上填了就以界面为准。
        否则用户改了收件人，却发现信还发到旧地址 —— 那比不兜底更糟。
        """
        for source in (param, task_settings, local):
            raw = source.get(ui_key)
            text = "" if raw is None else str(raw).strip()
            if text:
                return text
        return default

    port = int(_number(pick("smtp_port", DEFAULT_SMTP_PORT), DEFAULT_SMTP_PORT))
    if not 0 < port < 65536:
        print(f"[laser_sweep] ⚠️ SMTP 端口 {port} 不合法，已改用 {DEFAULT_SMTP_PORT}")
        port = DEFAULT_SMTP_PORT

    timeout_sec = _number(pick("timeout_sec", DEFAULT_TIMEOUT_SEC), DEFAULT_TIMEOUT_SEC)
    if timeout_sec <= 0:
        timeout_sec = DEFAULT_TIMEOUT_SEC

    notify_limit = int(_number(pick("notify_limit", DEFAULT_NOTIFY_LIMIT), DEFAULT_NOTIFY_LIMIT))
    if notify_limit < 0:
        notify_limit = 0

    # 通知方式：节点 > 邮件通知任务设置 > 本地 > 默认
    mode_raw = ""
    for source in (param, task_settings, local):
        mode_raw = str(source.get("notify_mode") or "").strip()
        if mode_raw:
            break

    def _flag_from_sources(key: str, default: bool) -> bool:
        for source in (param, task_settings, local):
            raw = source.get(key)
            if raw is None or str(raw).strip() == "":
                continue
            return _flag(raw, default)
        return default

    return {
        "notify_mode": _parse_notify_mode(mode_raw or DEFAULT_NOTIFY_MODE),
        "notify_mode_raw": mode_raw,
        "smtp_host": str(pick("smtp_host", "")).strip(),
        "smtp_port": port,
        "smtp_ssl": _flag_from_sources("smtp_ssl", DEFAULT_SMTP_SSL),
        # STARTTLS 只在用户显式开启、且没开 SSL 时才用得上（两者互斥）
        "smtp_starttls": _flag_from_sources("smtp_starttls", False),
        "smtp_user": str(pick("smtp_user", "")).strip(),
        # 密码不做任何加工：授权码里可能带特殊字符，strip 掉反而会认证失败
        "smtp_password": str(
            param.get("smtp_password")
            or task_settings.get("smtp_password")
            or local.get("smtp_password")
            or ""
        ),
        "mail_to": str(pick("mail_to", "")).strip(),
        "mail_subject_prefix": str(pick("mail_subject_prefix", DEFAULT_MAIL_SUBJECT_PREFIX)).strip(),
        "timeout_sec": timeout_sec,
        "notify_limit": notify_limit,
    }


def _stable_readings(
    values: list[float],
    abs_tol: float,
    pct_tol: float,
) -> bool:
    """这几次「异常」读数彼此是不是一致的（= 真实跳变，而非噪声）。

    为什么需要它（2026-10-27 用真实数据换来的判据）：
        阈值只能回答「和上一个点差多少」，回答不了「这个新值是真的还是读错了」。
        实测 201 组扫描里第 176/177 两组被判废，可它们的多次重测彼此只差
        0.10~0.23，而下一组立刻回到基准附近 —— 这是设备真实的跳变，
        不是 OCR 误读。两个点就这么白丢了。

    判据：连续 N 次读数（N = retry_max）的**跨度**（max - min）落在允许带内。
        · 允许带 = max(abs_tol, |中位数| × pct_tol / 100)，与采样聚合同一套口径
        · 真实跳变：5 次都读到 -18.1 ± 0.1 → 跨度 0.2 ≪ 3.0 → 判为一致 ✓
        · OCR 误读：5 次读成 -18 / -797 / -7.9 / -85 / -14 → 跨度几百 → 不算 ✗
        · 通信抖动：界面卡在旧值或乱跳，跨度同样很大 → 不算 ✗

    ⚠️ 只用「重测之间稳不稳」这一个性质，不拟合、不看历史趋势 ——
       这一层的职责只是把「稳定的新值」和「不稳定的坏值」分开。

    ⚠️⚠️ 但「稳定」不等于「真实」—— 这是本判据的**硬边界**，必须说清：
        如果 OCR 把同一个错值连着读 5 次（比如小数点稳定错位成 -797.2，
        或者界面卡死一直显示同一个数），这 5 次也"彼此一致"，
        会被误判成真实跳变。
        两道防线兜住它：
          ① 这个值虽然被记录，但**同时记进 _state["jumps"]**，
             收尾摘要里会列出来「第 N 组接受了跳变：-14.68 → -18.14」，
             翻结果的人一眼能看到被动过手脚的地方，而不是静默改掉；
          ② 判据要求「偏离基准」才成立 —— 若真是稳定错位（-797.2），
             它和基准的差会有几百，日志里"跨度"与"偏离"两个数字摆在一起，
             人工核对时极其显眼。
        所以这里选择"记录 + 留痕"而不是"丢弃"：
        真实跳变被丢掉会让整条曲线少一个点，比多一个可疑点更难发现。
    """
    data = sorted(float(v) for v in values)
    if len(data) < 2:
        return False

    median = _median(data)
    band = max(max(0.0, abs_tol), abs(median) * max(0.0, pct_tol) / 100.0)
    span = data[-1] - data[0]
    return span <= band


def _alert_mail_param(node_param: dict[str, Any]) -> dict[str, Any]:
    """给「告警」这条路凑出一份完整的发信参数。

    为什么需要它（2026-10-27 实测事故）：
        Guard 节点的 custom_action_param 里**只有**阈值/次数/开关 —— 因为它属于
        「扫描设置」，而 SMTP 账号、授权码、收件人全部搬到了独立的「邮件设置」。
        于是告警这条路径直接把自己的 param 交给 _resolve_mail_param 时，
        smtp_host / smtp_user / mail_to 全是空，_send_alert 判定"缺配置"、
        静默降级为 off —— 扫描跑完了、CSV 也写了空值，但**一封告警信都没有**。
        而扫描结束时那封「完成通知」却发得出去，于是用户看到的现象是
        "别的信能收到，偏偏异常告警收不到"，很难自查。

    合并顺序（前者优先）：
        ① 本次扫描开始时由 begin 存下的邮件参数（_state["mail_param"]）
           —— 它就是「邮件通知」任务里「邮件设置」的完整内容，最权威；
        ② 节点自己带的字段（万一哪天有人把邮件参数又写回节点）；
        ③ 都不含的字段交给 _resolve_mail_param 去读本地 config 文件兜底。
    """
    merged: dict[str, Any] = {}

    stored = _state.get("mail_param")
    if isinstance(stored, dict):
        merged.update({k: v for k, v in stored.items() if v is not None})

    # 节点参数里如果真带了邮件字段，以它为准（更接近"当前这一次的意图"）
    for key in MAIL_PARAM_KEYS:
        if key in node_param and node_param[key] is not None:
            merged[key] = node_param[key]

    return merged


def _resolve_guard_param(param: dict[str, Any]) -> dict[str, Any]:
    """把 UI/流水线传来的 guard 参数解析成一份「全是正确类型」的配置。

    为什么不直接在动作里现取现用：这些值来自 JSON 字符串（"true"/"10"/""），
    类型全得自己归一；而且 LaserSweepBegin 要把它存一份进 _state，
    LaserSweepGuard / LaserSweepAbnormalStop 又要各取一次 ——
    解析逻辑只写一份，三处拿到的东西才不会各说各话。

    容错原则：**任何一项填错都不该让保护层失效或误伤**。
    认不出来的值退回默认值并打一行日志，绝不抛异常 ——
    这一层本身是为了防故障，它自己变成新的故障源就本末倒置了。
    """
    # 总开关：界面可能传 "true"/"false" 字符串，走现成的 _flag()，
    # 保证和「跑完自动打开文件夹」那类开关的判法完全一致。
    enabled = _flag(param.get("enabled"), DEFAULT_GUARD_ENABLED)

    abs_raw = param.get("tolerance_abs")
    tolerance_abs = _number(abs_raw, DEFAULT_TOLERANCE_ABS)
    if tolerance_abs < 0:
        print(f"[laser_sweep] ⚠️ 绝对偏差阈值不能为负（{abs_raw!r}），已改用 {_fmt(DEFAULT_TOLERANCE_ABS)}")
        tolerance_abs = DEFAULT_TOLERANCE_ABS

    tolerance_pct = _percent_value(param.get("tolerance_pct"), DEFAULT_TOLERANCE_PCT)

    retry_wait = _number(param.get("retry_wait"), DEFAULT_RETRY_WAIT)
    if retry_wait < 0:
        print(f"[laser_sweep] ⚠️ 重测等待不能为负（{param.get('retry_wait')!r}），已按 0 处理")
        retry_wait = 0.0

    # retry_max 至少 1：填 0 会让「一次异常就停机」，那不是用户想要的「关掉校验」
    # （关掉校验请用 enabled=false）。这里夹到 1 并说明，免得保护层静默变形。
    retry_max = int(_number(param.get("retry_max"), DEFAULT_RETRY_MAX))
    if retry_max < 1:
        print(f"[laser_sweep] ⚠️ 连续异常次数至少为 1（收到 {param.get('retry_max')!r}），已按 1 处理")
        retry_max = 1

    timeout_sec = _number(param.get("timeout_sec"), DEFAULT_TIMEOUT_SEC)
    if timeout_sec <= 0:
        timeout_sec = DEFAULT_TIMEOUT_SEC

    notify_limit = int(_number(param.get("notify_limit"), DEFAULT_NOTIFY_LIMIT))
    if notify_limit < 0:
        notify_limit = 0

    def _text(key: str, default: str = "") -> str:
        return str(param.get(key) or default).strip()

    # ── 邮件参数：只从「邮件设置」那份配置来 ──────────────────────────────
    # 2026-10-27 改（主人的要求）：邮件相关的输入项全部搬到独立的
    # 「邮件通知」任务里，主任务只留一个「读数异常是否需要发送邮件」开关。
    #
    # 为什么这个重构是对的：
    #   1. 主任务的节点参数因此不再包含 SMTP 账号/授权码 —— 那些字段少写一个就会
    #      被 pipeline_override 整体替换掉（本项目踩过的坑），字段越少越不容易出事；
    #   2. 邮件配置只有一处可改，不会出现「扫描任务里改了一半、通知任务里还是旧的」；
    #   3. 读数异常时到底发不发，变成一个明确的布尔开关，而不是靠一串参数是否填齐去推断。
    #
    # 读取顺序：邮件设置（界面） > 本地 config 文件 > 内置默认。
    #
    # ⚠️ 这里**一次都不读邮件配置** —— 这是主人明确要求的「没异常就迅速进入下一步」。
    #    读数校验每轮都会走这个函数，如果在这儿顺手把 SMTP 配置解析出来，
    #    就等于每轮白付一次文件 stat + JSON 解析的钱（实测 0.178 ms/轮）。
    #    所以只解析出一个布尔开关；真需要发信时，由 LaserSweepGuard 在
    #    「连续异常到顶」那个分支里现调 _resolve_mail_param —— 那种时刻慢一点无所谓。
    want_mail = _flag(param.get("notify_enabled"), True)

    # 连续异常到顶之后：继续扫下一个点，还是终止整个扫描（2026-10-27 新增）
    # 默认 True = 继续。理由见 DEFAULT_CONTINUE_ON_ABNORMAL 的注释：
    # 告警邮件发出去之后，主人未必能马上处理，让它跑完比停在半路更实用。
    continue_on_abnormal = _flag(param.get("continue_on_abnormal"), DEFAULT_CONTINUE_ON_ABNORMAL)

    return {
        "enabled": enabled,
        "tolerance_abs": tolerance_abs,
        "tolerance_pct": tolerance_pct,
        "retry_wait": retry_wait,
        "retry_max": retry_max,
        "retry_node": _text("retry_node", DEFAULT_RETRY_NODE) or DEFAULT_RETRY_NODE,
        "record_node": _text("record_node", DEFAULT_RECORD_NODE) or DEFAULT_RECORD_NODE,
        "stop_node": _text("stop_node", DEFAULT_STOP_NODE) or DEFAULT_STOP_NODE,
        "wait_node": _text("wait_node", DEFAULT_WAIT_NODE) or DEFAULT_WAIT_NODE,
        # 「判废后继续扫」时跳这里（跳过 Record，避免空值行写两遍）。
        # 2026-10-27 之前的坑正是跳 record_node 导致重复写：CSV 多出两行。
        "advance_node": _text("advance_node", DEFAULT_ADVANCE_NODE) or DEFAULT_ADVANCE_NODE,
        # 主任务唯一关心的邮件开关：读到异常时要不要发信。
        # 对应的邮件参数不在这里解析（见上面的说明），由终止分支现取。
        "notify_enabled": want_mail,
        # 连续异常到顶之后：True = 继续扫下一个点，False = 终止扫描
        "continue_on_abnormal": continue_on_abnormal,
    }


def _fmt_elapsed(seconds: Optional[float]) -> str:
    """把秒数说成人话："3725 秒（约 1 小时 2 分）"。

    邮件正文里只写 "3725" 的话，收到信的人还得心算一下这是多久 ——
    而告警邮件的读者往往是在手机上看的，多一行换算省一次心算。
    """
    if seconds is None:
        return "未知"
    total = max(0, int(round(float(seconds))))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        human = f"{hours} 小时 {minutes} 分"
    elif minutes:
        human = f"{minutes} 分 {secs} 秒"
    else:
        human = f"{secs} 秒"
    return f"{total} 秒（约 {human}）"


def _compose_alert(
    *,
    values: list[str],
    index: int,
    history: list[float],
    abnormal: list[float],
    reference: Optional[float],
    tolerance_abs: float,
    tolerance_pct: float,
    elapsed_sec: float,
    csv_path: Any,
    started_at: str,
    retry_max: int,
    continuing: bool = False,
) -> dict[str, Any]:
    """组装告警邮件：返回 {subject, body, expected, abnormal, summary}。

    正文写成**纯文本分段**而不是 HTML，就一个原因：这是给人救命看的信 ——
    手机上、企业微信里、网页邮箱里都要原样显示。HTML 在客户端之间差异太大，
    而中文一旦编码没弄对就是一封乱码信，等于没发。

    结构按「先结论、再证据、最后怎么查」排：
        ① 一句话结论（哪一组、连续几次、已终止）
        ② 关键数字（当前组号/设定值、异常读数、正常参考值、允许偏差）
        ③ 现场信息（耗时、CSV 路径、机器名、时间）—— 事后追查全靠这一段

    参数全用关键字，是为了让调用处一眼看清「这一栏填的是什么」：
    这个函数有十几个数字，位置参数写反了不会有任何报错，只会发出一封错信。
    """
    total = len(values)
    # 当前组：index 是 0 起的下标，人对人说话时永远是「第 3 组」
    current_no = min(index + 1, total) if total else index + 1
    current_value = values[index] if 0 <= index < total else "(已越界)"

    abnormal = [float(v) for v in abnormal]
    # 异常读数只留最近 10 个：邮件是给人看的，不是日志转储。
    # 连续异常最多也就 retry_max 个，正常不会截断，这里是防手改配置后刷屏。
    shown_abnormal = abnormal[-10:]
    abnormal_line = "、".join(_fmt(v) for v in shown_abnormal) if shown_abnormal else "(无)"

    # 「正常应该是什么样」：优先用传入的基准，退而求其次用历史最后一个。
    # 这句话是整封信里最值钱的 —— 收到信的人第一反应就是「那本来该是多少」。
    if reference is None and history:
        reference = float(history[-1])
    if reference is None:
        reference_text = "无（这是本次扫描的第一个读数，没有可比对象）"
        # 没有基准时，用阈值反推一个大致区间，至少给个参照
        expected_text = f"无法推断（缺少历史读数），阈值：绝对 ±{_fmt(tolerance_abs)}"
    else:
        ref = float(reference)
        expected_text = f"约 {_fmt(ref)}"
        if tolerance_abs > 0:
            expected_text += f"（允许 ±{_fmt(tolerance_abs)}"
        else:
            expected_text += "（绝对阈值已关闭"
        if tolerance_pct > 0:
            expected_text += f"，或 ±{_fmt(tolerance_pct)}%"
        expected_text += "）"

    # 历史最后几个正常值：让收信人一眼看出「前面是稳的，从这一组开始崩的」
    tail = [float(v) for v in history[-6:]]
    history_line = "、".join(_fmt(v) for v in tail) if tail else "(还没有正常读数)"

    threshold_text = (
        f"绝对偏差 ≤ {_fmt(tolerance_abs)}  或  相对偏差 ≤ {_fmt(tolerance_pct)}%"
        + ("（任一满足即视为正常）" if tolerance_abs > 0 and tolerance_pct > 0 else "")
    )

    try:
        csv_text = str(csv_path) if csv_path is not None else "(未知，本次扫描可能还没建好文件)"
    except Exception:  # noqa: BLE001 - 拼信不能因为一个路径对象失败
        csv_text = repr(csv_path)

    try:
        machine = socket.gethostname()
    except OSError:
        machine = "未知"

    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 标题和结论都要说清「接下来是继续还是停」——
    # 2026-10-27 起默认继续扫描，如果信里还写「已终止」，收信人会去查一个
    # 其实还在正常跑的扫描，白跑一趟。
    strikes = len(abnormal) or retry_max
    if continuing:
        subject = (
            f"读数异常告警：第 {current_no}/{total} 组（设定 {current_value}）"
            f"连续 {strikes} 次异常，已跳过并继续扫描"
        )
    else:
        subject = (
            f"读数异常告警：第 {current_no}/{total} 组（设定 {current_value}）"
            f"连续 {strikes} 次异常，已终止扫描"
        )

    conclusion = [
        f"  第 {current_no}/{total} 组（设定参数 {current_value}）的读数连续 "
        f"{strikes} 次偏离正常范围，",
        (
            "  已按设置跳过这一组、继续扫描后面的点 —— 扫描仍在进行，无需立即处理；"
            if continuing
            else "  已停止扫描，避免继续把不可信的数据写进结果文件。"
        ),
        "  CSV 里这一组写的是空值占位，行号与参数仍一一对应。",
    ]

    body = "\n".join(
        [
            (
                "激光参数扫描 —— 读数异常，已跳过该组并继续"
                if continuing
                else "激光参数扫描 —— 读数异常，已自动终止"
            ),
            "=" * 46,
            "",
            "【结论】",
            *conclusion,
            "",
            "【异常读数】",
            f"  {abnormal_line}",
            f"  对比基准（上一个正常读数）：{_fmt(reference) if reference is not None else '(无)'}",
            "",
            "【正常数据应该是什么样】",
            f"  期望值范围：{expected_text}",
            f"  允许的偏差阈值：{threshold_text}",
            f"  最近的正常读数：{history_line}",
            "",
            "【现场信息】",
            f"  本次扫描总耗时：{_fmt_elapsed(elapsed_sec)}",
            f"  进度：第 {current_no} 组 / 共 {total} 组"
            f"（已完成 {min(index, total)} 组）",
            f"  开始时间：{started_at or '(未知)'}",
            f"  终止时间：{now_text}",
            f"  结果文件：{csv_text}",
            f"  机器名：{machine}",
            "",
            "【建议排查】",
            (
                "  1. 扫描还在跑，可以现在就到 B 软件看一眼读数是否正常；"
                if continuing
                else "  1. 回到 B 软件看当前读数是否还在正常范围；"
            ),
            "  2. 若画面数值已正常，多半是 OCR 认错或设备那一下的抖动，可重跑这一组；",
            "  3. 若读数确实漂了，先查设备状态（功率/波长/温度），再决定要不要重扫；",
            "  4. 觉得阈值定得太紧，可在「扫描设置」里放宽绝对/相对偏差阈值后重跑。",
            (
                "  5. 若希望出现异常就立即停下来，把「出现异常后是否继续测试」关掉。"
                if continuing
                else "  5. 若希望出现异常也继续扫完，把「出现异常后是否继续测试」打开。"
            ),
            "",
            "—— 本邮件由激光参数扫描程序自动发送，请勿直接回复。",
        ]
    )

    return {
        "subject": subject,
        "body": body,
        "expected": expected_text,
        "abnormal": abnormal,
        # summary 是给日志/界面用的短摘要，避免把整封信打进日志
        "summary": (
            f"第 {current_no}/{total} 组（设定 {current_value}）连续 {len(abnormal) or retry_max} 次异常，"
            f"基准 {_fmt(reference) if reference is not None else '(无)'}，"
            f"异常值 {abnormal_line}"
        ),
    }


def _send_alert(param: dict[str, Any], info: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """把告警发出去，返回 {"ok": bool, "channel": "email"|"off", "error": str}。

    这个函数的**唯一硬要求**是「无论出什么事都不能往上抛」：
    它是在「扫描要终止」这条路径上被调用的，发信失败绝不能让终止流程本身失败 ——
    否则就变成「为了通知一个问题而制造一个更大的问题」。
    所以整个函数体都在 try/except 里，任何异常都收敛成 ok=False。

    参数（都从 param 里取，兼容缺省）：
        notify_mode          auto / email / mail / off（大小写、空格都宽容）
        smtp_host            服务器，留空 = 发不了
        smtp_port            默认 465
        smtp_ssl             默认 True（465 隐式 SSL）
        smtp_starttls        默认 False（587 用；与 ssl 互斥，ssl 优先）
        smtp_user            账号（QQ 等要填完整邮箱）
        smtp_password        密码/授权码 —— **绝不进日志**，只报长度和有无
        mail_to              收件人，逗号/分号/中文逗号分隔都行
        mail_subject_prefix  主题前缀，默认 "[激光扫描告警]"
        timeout_sec          连接与读写超时，默认 20 秒
        notify_limit         本次扫描最多发几封（按 count_key 计数）

    info 是 _compose_alert 的返回值（至少要有 subject / body）。

    count_key 是**计数用的 _state 键名**，默认 "sent_alerts"（告警专用）。
    为什么要有它：「邮件测试」「任务完成通知」和「异常告警」是三条独立的发信路径，
    共用一个计数器会互相挤占名额 —— 2026-10-27 实测撞到过：先点一次「邮件测试」
    发成功后，紧接着点「邮件通知」就被提示「已达到上限 notify_limit=1，不再发送」。
    所以只有告警走限额，其余两类用自己的键（传 count_key="" 表示这次不计入、
    也不受上限限制）。
    """
    count_key = str(kwargs.get("count_key", "sent_alerts") or "")

    mode = _parse_notify_mode(param.get("notify_mode"))
    prefix = str(param.get("mail_subject_prefix") or DEFAULT_MAIL_SUBJECT_PREFIX).strip()
    host = str(param.get("smtp_host") or "").strip()
    user = str(param.get("smtp_user") or "").strip()
    password = str(param.get("smtp_password") or "")
    recipients = _split_mail_list(param.get("mail_to"))
    timeout_sec = _number(param.get("timeout_sec"), DEFAULT_TIMEOUT_SEC)
    if timeout_sec <= 0:
        timeout_sec = DEFAULT_TIMEOUT_SEC

    # off：明确不发。放在最前面，连「配置齐不齐」都不必看。
    if mode == "off":
        print("[laser_sweep] 通知方式为 off，只记录日志、不发邮件")
        return {"ok": False, "channel": "off", "error": "notify_mode=off"}

    # 缺字段时的处理分两档，因为 auto 和 email 的**意图完全不同**：
    #   auto  = 「能发就发」→ 缺配置是正常情况（很多人根本不想收信），静默降级
    #   email = 「必须发」  → 缺配置要明确说出来，否则用户以为配好了却没收到信
    missing = [
        name
        for name, value in (
            ("smtp_host", host),
            ("smtp_user", user),
            ("mail_to", param.get("mail_to")),
        )
        if not str(value or "").strip()
    ]
    if missing:
        reason = "缺少配置：" + "、".join(missing)
        if mode == "auto":
            print(f"[laser_sweep] {reason}，通知降级为 off（不会报错，扫描照常终止）")
            return {"ok": False, "channel": "off", "error": reason}
        print(f"[laser_sweep] ❌ 告警邮件发不出去 —— {reason}")
        return {"ok": False, "channel": "off", "error": reason}

    # 密码缺失单独判：它是最常见的配错（很多人把「登录密码」和「授权码」搞混），
    # 而 smtplib 报出来的是一句难懂的认证失败，不如在这里直接点明。
    if not password:
        reason = "缺少 smtp_password（很多邮箱要求填「授权码」而不是登录密码）"
        print(f"[laser_sweep] ❌ 告警邮件发不出去 —— {reason}")
        return {"ok": False, "channel": "off", "error": reason}

    # notify_limit：提前拦，避免配了 50 个点 + 反复异常时把邮箱刷爆。
    #
    # ⚠️ 三条约束，都是踩过坑之后定下来的：
    #    ① 只有**显式传了这个键**才启用上限 —— 缺键表示「调用方没关心这件事」，
    #       这时不该拿默认值 1 去拦（自检里就撞到过：上一封成功之后，
    #       下一封真正的"网络不通"根本走不到连接那一步，错误原因被"已达上限"顶掉）。
    #    ② 计数只统计**告警**邮件（见 _send_alert 的 count_key 参数）。
    #       否则「邮件测试」发过一封之后，「任务完成通知」就会被误挡下来 ——
    #       2026-10-27 实测撞到：测试信成功 → 完成通知提示「已达上限，不再发送」。
    #       限额的本意是「防止异常反复触发刷屏」，完成通知每次都只发一次，不该受限。
    #    ③ 上限 0 表示一封都不发（只写日志），照拦。
    if "notify_limit" in param:
        limit = int(_number(param.get("notify_limit"), DEFAULT_NOTIFY_LIMIT))
        sent_before = int(_state.get(count_key) or 0)
        if limit >= 0 and sent_before >= limit:
            reason = f"本次扫描已发 {sent_before} 封，达到上限 notify_limit={limit}，不再发送"
            print(f"[laser_sweep] {reason}")
            return {"ok": False, "channel": "off", "error": reason}

    subject = f"{prefix}{info.get('subject') or '扫描异常'}"
    body = str(info.get("body") or "")

    message = EmailMessage()
    message["From"] = user
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    # 纯文本正文 + 显式 utf-8：中文在 QQ/163/Outlook 里都不会变乱码。
    # 不用 HTML 是因为它在各家客户端里差异太大，乱码一次就等于没发。
    message.set_content(body, subtype="plain", charset="utf-8")

    port = int(_number(param.get("smtp_port"), DEFAULT_SMTP_PORT))
    use_ssl = _flag(param.get("smtp_ssl"), DEFAULT_SMTP_SSL)
    use_starttls = _flag(param.get("smtp_starttls"), False) and not use_ssl

    # 密码只在**这里**被读一次，后面只引用长度；日志里永远不出现原文。
    print(
        f"[laser_sweep] 正在发送告警邮件 → {host}:{port}"
        f"（{'SSL' if use_ssl else ('STARTTLS' if use_starttls else '明文')}"
        f"，账号 {user}，密码长度 {len(password)}，收件人 {len(recipients)} 个，超时 {_fmt(timeout_sec)} 秒）"
    )

    try:
        if use_ssl:
            # ⚠️ 必须显式传 context=ssl.create_default_context()（2026-10-27 修）。
            #
            # 之前这里**不传 context**，注释还写着"默认参数就会建一个校验过证书的
            # SSLContext"—— 那句话是错的。实测本项目的便携版 Python 3.13：
            #     未传 context 的 SMTP_SSL  -> check_hostname=False, verify_mode=CERT_NONE
            #     ssl.create_default_context() -> check_hostname=True,  verify_mode=CERT_REQUIRED
            # 也就是说连接虽然加密，却**不认证服务器身份**：中间人可以冒充 smtp.qq.com，
            # 拿到 SMTP 授权码（等于半个邮箱）。
            #
            # 当时不传 context 的理由是"假 SMTP 对象只接受 (host, port, timeout)"——
            # 为了迁就测试替身而放着生产环境的证书校验不开，是本末倒置。
            # 正确做法是让替身接受 context 参数（自检里已同步改）。
            #
            # ⚠️ 必须用 with：smtplib 的 SMTP 对象是上下文管理器，
            #    用 with 才能保证异常时也把连接（和 socket）收干净。
            with smtplib.SMTP_SSL(
                host, port, timeout=timeout_sec, context=ssl.create_default_context()
            ) as smtp:
                smtp.login(user, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=timeout_sec) as smtp:
                if use_starttls:
                    smtp.starttls(context=ssl.create_default_context())
                smtp.login(user, password)
                smtp.send_message(message)
    except Exception as exc:  # noqa: BLE001 - 通知失败绝不能让终止流程失败
        reason = f"{type(exc).__name__}: {exc}"
        print(f"[laser_sweep] ❌ 告警邮件发送失败（不影响扫描终止）：{reason}")
        return {"ok": False, "channel": "email", "error": reason}

    # 计数只统计**成功发出**的信：失败的那次不该占用 notify_limit 的名额，
    # 否则网络抖一下就把「本次扫描只能发 1 封」用掉了，真出事时反而不发了。
    # count_key 为空表示这条路径不参与限额统计（邮件测试 / 完成通知）。
    sent_now = 0
    if count_key:
        sent_now = int(_state.get(count_key) or 0) + 1
        _state[count_key] = sent_now
    print(
        f"[laser_sweep] ✉️ 已发送邮件{'第 %d 封' % sent_now if count_key else ''} → "
        f"{'、'.join(recipients)}（{host}:{port}）"
    )
    print(f"[laser_sweep]   主题：{subject}")
    return {"ok": True, "channel": "email", "error": ""}


# --------------------------------------------------------------------------- #
# 控制器的小配置
# --------------------------------------------------------------------------- #
def _configure_controller(context: Context) -> None:
    """尽最大努力把常用修饰键声明为「后台受管键」。

    背景（踩过的坑）：Win32 走 PostMessage 系列输入方式时，单独发 KeyDown(Ctrl)
    再发 ClickKey(A)，目标程序（Qt）拿不到 Ctrl 的按下状态 —— Ctrl+A 全选不生效，
    反倒把字母 'a' 当普通字符打了进去，于是数值每轮往后拼接：
        a200a2a281a30a27.a240a2200

    官方文档给的解法就是声明受管键（docs/zh_cn/2.4-控制方式说明.md「后台受管键守护」）：
        MaaControllerSetOption(ctrl, MaaCtrlOption_BackgroundManagedKeys, keycodes, ...)

    ⚠️ 当前流水线已经改成**不依赖修饰键**的「End + Backspace」清空方案，
    所以这里只是顺手设置：成功最好，失败（比如框架版本过旧没这个选项）也不影响流程。
    """
    try:
        controller = context.tasker.controller
    except Exception as exc:  # noqa: BLE001 - 纯尽力而为的配置
        print(f"[laser_sweep] 取不到控制器，跳过后台受管键设置: {exc}")
        return

    # Ctrl(17) / Alt(18) / Shift(16) / Win(91)
    keys = [17, 18, 16, 91]

    setter = getattr(controller, "set_background_managed_keys", None)
    if setter is None:
        print("[laser_sweep] 当前框架版本的绑定没有 set_background_managed_keys，已跳过")
        return

    try:
        ok = bool(setter(keys))
    except Exception as exc:  # noqa: BLE001
        print(f"[laser_sweep] 声明后台受管键失败（框架版本可能不支持）: {exc}")
        return

    if ok:
        print(f"[laser_sweep] 已声明后台受管键 {keys}（Ctrl/Alt/Shift/Win）")
    else:
        print("[laser_sweep] 后台受管键设置被拒绝（框架版本可能过旧），已忽略")


# --------------------------------------------------------------------------- #
# B 软件窗口：独立截图（方案 A）
#
# ⚠️ 2026-10-27 按实测重写了整条截图链路 —— 原因是「1 秒多点采样」把截图
#    从"每轮一次"变成"每秒十几次"，那些原本可以忽略的每帧固定开销开始有意义。
#    实测（1440x829，本机 B 软件客户区）：
#
#        现有做法                                  每帧
#          GetDIBits(32bpp，多传 25% 无用字节)       0.35 ms
#          numpy BGRA→BGR  ascontiguousarray       2.91 ms   ← 大头
#          create_string_buffer 4.6MB              1.01 ms   ← 每帧重新分配
#          ctypes.WinDLL × 2 + 重设 argtypes        0.04 ms
#          ----------------------------------------------
#          合计                                     4.31 ms
#
#        重写之后（24bpp + 只取数值框 + 复用缓冲 + 模块级绑定）
#          GetDIBits(24bpp，只取 200x40)            ~0.01 ms
#          numpy 只 reshape（零拷贝视图）             0.00 ms
#          create_string_buffer（复用，不再分配）      0.00 ms
#          ----------------------------------------------
#          合计                                    ~0.01 ms   → 快两个数量级
#
#    这段省下来的绝对值不大，但它乘的是采样频率：20 帧/秒时等于每秒白付 86 ms，
#    直接吃掉了"这一秒能多读几次"的余量。压掉之后采样间隔也更均匀，
#    而间隔越均匀，那一秒的平均值代表性越好。
#
#    另外：每帧只渲染数值框那一小块（ROI 位图 + 视口平移 + 裁剪区），
#    PrintWindow 的实际绘制量也随之减小 —— 这是唯一能碰到 PrintWindow 成本的办法。
#    若目标窗口是 DirectComposition/硬件加速渲染、不吃裁剪与视口，
#    会得到一帧纯色（什么都没画），这时自动退回"整窗渲染再裁"的稳妥路径。
# --------------------------------------------------------------------------- #
def _list_windows() -> list[tuple[int, str, str]]:
    """枚举所有可见的顶层窗口，返回 [(hwnd, 标题, 类名), ...]。

    ⚠️ 这里**不能**用 maa.toolkit.Toolkit：
    只要导入过 maa.agent，Library 就会切成 AgentServer 模式，
    此时调用 Toolkit 会直接抛 `ValueError: Toolkit is not available in AgentServer context`。
    （见 maa/library.py 的 Library.toolkit()）
    所以改用 ctypes 直接调 user32，不依赖框架的 toolkit 模块。

    顺带一提：MaaAgentServer.dll 里所有 `*ControllerCreate` 都只是空壳
    （见 MaaAgentServerNotImpl.cpp），所以 agent 进程里也建不了 Win32Controller，
    截图只能自己走 GDI —— 也就是下面的 _screencap_window()。
    """
    if sys.platform != "win32":
        print("[laser_sweep] 窗口查找目前只实现了 Windows")
        return []

    user32, _gdi32 = _win_gdi_handles()

    wndenumproc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows.argtypes = [wndenumproc, ctypes.c_void_p]
    user32.EnumWindows.restype = ctypes.c_bool

    found: list[tuple[int, str, str]] = []

    def _collect(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_name, 256)
        found.append((int(hwnd), title.value, class_name.value))
        return True

    user32.EnumWindows(wndenumproc(_collect), 0)
    return found


def _find_window(param: dict[str, Any]) -> Optional[tuple[int, str, str]]:
    """按 window_regex 找到 B 软件窗口，返回 (hwnd, 标题, 类名)，结果按正则缓存。"""
    pattern = str(param.get("window_regex") or "").strip()
    if not pattern:
        return None

    cached = _window_cache.get(pattern)
    if cached is not None:
        return cached

    try:
        regex = re.compile(pattern)
    except re.error as exc:
        print(f"[laser_sweep] window_regex 不是合法正则: {exc}")
        return None

    windows = _list_windows()
    matched = [w for w in windows if regex.search(w[1])]
    if not matched:
        print(f"[laser_sweep] 没有窗口标题匹配 /{pattern}/，当前可见窗口：")
        for hwnd, title, class_name in windows[:20]:
            print(f"[laser_sweep]     {title!r}  class={class_name!r}  hwnd={hwnd}")
        return None

    index = int(param.get("window_index") or 0)
    if not 0 <= index < len(matched):
        index = 0
    window = matched[index]

    if len(matched) > 1:
        print(f"[laser_sweep] 有 {len(matched)} 个窗口匹配 /{pattern}/，当前用第 {index} 个")

    _window_cache[pattern] = window
    return window


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


_dpi_aware_done = False


def _ensure_dpi_aware() -> bool:
    """声明进程 DPI 感知，只做一次。

    这一步很关键：显示器有缩放（例如 200%）时，未声明 DPI 感知的进程拿到的是
    被 Windows 虚拟化过的坐标——实测同一扇窗口 GetWindowRect 返回 1122x636，
    而真实尺寸是 2244x1271。用虚拟化尺寸去建位图，PrintWindow 又按物理尺寸渲染，
    结果就只能抓到窗口左上角一小块。
    """
    global _dpi_aware_done
    if _dpi_aware_done:
        return True

    if sys.platform != "win32":
        return False

    # Win8.1+：2 = PROCESS_PER_MONITOR_DPI_AWARE
    try:
        ctypes.WinDLL("shcore", use_last_error=True).SetProcessDpiAwareness(2)
        _dpi_aware_done = True
        return True
    except (OSError, AttributeError):
        pass

    # 老接口兜底
    try:
        if ctypes.WinDLL("user32", use_last_error=True).SetProcessDPIAware():
            _dpi_aware_done = True
            return True
    except (OSError, AttributeError):
        pass

    print("[laser_sweep] 警告：无法声明 DPI 感知，窗口截图可能不完整")
    return False


# ── 模块级一次性绑定：WinDLL 加载与 argtypes 设置都是每帧白付的钱 ──────────
# 实测 WinDLL×2 = 16 μs、重设 argtypes = 22 μs。单看很小，但采样时每秒十几次。
_win_gdi: dict[str, Any] = {"ready": False, "user32": None, "gdi32": None}

# 复用的像素缓冲区：按需长大，绝不缩小（窗口尺寸来回变时不反复分配）。
#   key  "bpp"    当前缓冲的位深（24 / 32）
#   key  "bytes"  已分配的字节数
#   key  "buf"    ctypes buffer
_cap_buffer: dict[str, Any] = {"bpp": 0, "bytes": 0, "buf": None}


def _win_gdi_handles() -> tuple[Any, Any]:
    """取 (user32, gdi32)，并把要用的函数签名都设好 —— 只做一次。

    为什么要设 argtypes/restype：默认 ctypes 把返回值当 32 位 int，
    64 位下的句柄会被截断成 32 位（本项目最早就是这么踩的坑）。
    """
    if _win_gdi["ready"]:
        return _win_gdi["user32"], _win_gdi["gdi32"]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
    user32.GetClientRect.restype = ctypes.c_bool
    user32.GetWindowDC.argtypes = [ctypes.c_void_p]
    user32.GetWindowDC.restype = ctypes.c_void_p
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    user32.PrintWindow.restype = ctypes.c_bool
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user32.GetDpiForWindow.argtypes = [ctypes.c_void_p]
    user32.GetDpiForWindow.restype = ctypes.c_uint
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.IsWindowVisible.restype = ctypes.c_bool

    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteObject.restype = ctypes.c_bool
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.restype = ctypes.c_bool
    gdi32.GetDIBits.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int
    # 只渲染目标区域用的两个调用：把窗口坐标系平移到 ROI 左上角，再裁掉外框
    gdi32.SetViewportOrgEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    gdi32.SetViewportOrgEx.restype = ctypes.c_bool
    gdi32.IntersectClipRect.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    gdi32.IntersectClipRect.restype = ctypes.c_int

    _win_gdi.update(ready=True, user32=user32, gdi32=gdi32)
    return user32, gdi32


def _row_stride(width: int, bytes_per_pixel: int) -> int:
    """DIB 行跨度：每行必须按 4 字节对齐。

    24bpp 时 W*3 未必是 4 的倍数（1441 宽 → 4323，要补 1 字节）。
    1440 宽时恰好 4320 整除、没有填充 —— 这时 reshape 出来就是连续数组，
    零拷贝能成立；有填充时走带 copy 的兜底分支。
    """
    return ((width * bytes_per_pixel + 3) // 4) * 4


def _reuse_buffer(need: int, bpp: int) -> Any:
    """拿一块可复用的原始字节缓冲，只增不减。

    为什么复用：`ctypes.create_string_buffer(4.6MB)` 实测 1.0 ms/次，
    采样时每秒十几次就等于每秒白送十几毫秒。
    """
    buf = _cap_buffer.get("buf")
    if buf is None or _cap_buffer.get("bpp") != bpp or _cap_buffer.get("bytes", 0) < need:
        buf = ctypes.create_string_buffer(need)
        _cap_buffer.update(buf=buf, bytes=need, bpp=bpp)
    return buf


def _frame_looks_empty(image: Any) -> bool:
    """整帧是不是「每个像素都是同一种颜色」（= 什么都没画上去）。

    用途：用「只渲染数值框」的快路径时，若目标窗口是
    DirectComposition / 硬件加速渲染，它可能不吃裁剪区和视口平移，
    结果就是一块纯色位图。这时必须退回整窗渲染，否则 OCR 读不到任何东西。

    ⚠️ 判据不能用 `min(image) == max(image)`（2026-10-27 审查发现）：
        那要求**所有像素的所有通道**都相等，只覆盖灰度纯色。
        一张每个像素都是 BGR(0,0,255) 的纯红图，min=0、max=255，
        会被判成"有内容"，于是彩色纯色帧不会触发兜底 —— 恰好漏掉最该兜的情况。

    正确判据：**通道内空间恒定** —— 每个通道在自己的所有像素上取值相同。
        纯黑/纯白/纯红/任意纯色  → True
        有任何一个通道在空间上有变化 → False（说明确实画了东西）

    另外加一条廉价的前置判断：整块内存完全相同（bytes 相等）直接返回 True，
    这是最常见的情况（空白位图），比逐通道 min/max 还快。
    """
    try:
        import numpy as np

        if image is None or getattr(image, "size", 0) == 0:
            return True

        arr = np.asarray(image)
        if arr.ndim == 2:  # 灰度图：一个通道
            return bool(arr.min() == arr.max())

        # 三通道：逐个通道比 min/max（通道内空间恒定才算纯色）
        for channel in range(arr.shape[2]):
            plane = arr[:, :, channel]
            if plane.min() != plane.max():
                return False
        return True
    except Exception:  # noqa: BLE001 - 诊断辅助，绝不能因为它把截图搞失败
        return False


def _client_size(hwnd: int) -> Optional[tuple[int, int]]:
    """窗口客户区尺寸 (w, h)。取不到返回 None。

    单独一个函数是因为「只截小块」之后，截图尺寸不再等于窗口尺寸，
    而数值框缓存的失效判断必须拿**窗口尺寸**比（窗口一动就该重新定位）。
    """
    if sys.platform != "win32":
        return None
    _ensure_dpi_aware()
    user32, _gdi32 = _win_gdi_handles()
    rect = _RECT()
    if not user32.GetClientRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
        return None
    w, h = rect.right - rect.left, rect.bottom - rect.top
    return (w, h) if w > 0 and h > 0 else None


def _grab_region(
    user32: Any,
    gdi32: Any,
    handle: Any,
    hwnd: int,
    region: Optional[tuple[int, int, int, int]],
    np: Any,
) -> Optional[Any]:
    """把窗口的一块区域渲染成 BGR numpy 数组（24bpp，零拷贝视图）。

    region = (x, y, w, h)，按**客户区坐标**；None = 整个客户区。

    实现要点：
      · 位图只建 region 那么大 —— 省掉 3.6MB 的搬运和 1.0ms 的分配
      · SetViewportOrgEx 把窗口坐标原点平移到 ROI 左上角，窗口仍按自己的
        客户区坐标绘制，正好落进这块小位图
      · IntersectClipRect 裁掉区域外的绘制（GDI 窗口会因此少画很多）
      · 24bpp：GDI 直接给 BGR，Python 侧不用再 copy 一次换通道
    """
    rect = _RECT()
    if not user32.GetClientRect(handle, ctypes.byref(rect)):
        print(f"[laser_sweep] GetClientRect 失败 (hwnd={hwnd})")
        return None

    client_w = rect.right - rect.left
    client_h = rect.bottom - rect.top
    if client_w <= 0 or client_h <= 0:
        print(f"[laser_sweep] 客户区尺寸异常: {client_w}x{client_h}")
        return None

    if region is None:
        x0, y0, w, h = 0, 0, client_w, client_h
    else:
        # 裁到客户区内：ROI 超界时 GDI 会静默画出奇怪结果
        rx, ry, rw, rh = (int(v) for v in region[:4])
        x0 = max(0, min(rx, client_w - 1))
        y0 = max(0, min(ry, client_h - 1))
        w = max(1, min(rw, client_w - x0))
        h = max(1, min(rh, client_h - y0))

    window_dc = user32.GetWindowDC(handle)
    if not window_dc:
        print("[laser_sweep] GetWindowDC 失败")
        return None

    mem_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, w, h)
    old_bitmap = gdi32.SelectObject(mem_dc, bitmap)
    restored = False

    try:
        if region is not None:
            # ── 裁剪区：先用**未平移**的逻辑坐标裁出位图大小，再平移原点 ──────
            #
            # 2026-10-27 审查发现（用内存 DIB 实测确认）：
            #   IntersectClipRect 的参数是**逻辑坐标**，而逻辑坐标要经过
            #   viewport origin 平移后才映射到设备坐标。
            #
            #   原写法先 SetViewportOrgEx(-x0,-y0) 再 IntersectClipRect(0,0,w,h)：
            #   平移后逻辑 (0,0,w,h) 映射到设备 (x0,y0,x0+w,y0+h)，而位图只有
            #   w×h —— 交集为空。实测 GetClipBox 返回 (0,0,0,0)：
            #       SetViewportOrgEx 后 Intersect(0,0,w,h)  → clip=(0,0,0,0)  空
            #       Intersect(0,0,w,h) 后 SetViewportOrgEx  → clip=(192,292,338,332) 有效
            #   （GetClipBox 返回的是逻辑坐标；(192,292,338,332) 平移后
            #     正好覆盖设备 0,0,w,h，也就是整块小位图 —— 这才是我们想要的。）
            #
            #   后果：对遵守裁剪状态的窗口，快路径必然画空 → 每次都退回整窗渲染，
            #   "只渲染数值框"的优化等于没生效（还白付一次整窗截图）。
            #
            # 正确顺序（C 方案，已实测）：
            #   ① IntersectClipRect(0, 0, w, h)   —— 未平移，逻辑坐标 = 设备坐标
            #   ② SetViewportOrgEx(-x0, -y0)      —— 平移后窗口按客户区坐标画正好落进来
            gdi32.IntersectClipRect(mem_dc, 0, 0, w, h)
            gdi32.SetViewportOrgEx(mem_dc, -x0, -y0, None)
            restored = True

        # PW_CLIENTONLY(1) | PW_RENDERFULLCONTENT(2)
        #   PW_CLIENTONLY 只抓客户区（不含标题栏），和框架对 A 软件的取景一致
        #   PW_RENDERFULLCONTENT 才能抓到 DirectComposition / 硬件加速的内容
        if not user32.PrintWindow(handle, mem_dc, 3):
            return None

        stride = _row_stride(w, 3)
        pad = stride - w * 3

        header = _BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        header.biWidth = w
        header.biHeight = -h  # 负数 = 自顶向下，省得再翻转
        header.biPlanes = 1
        header.biBitCount = 24  # ← 关键：GDI 直接吐 BGR，省掉 Python 侧的换通道 copy
        header.biCompression = 0  # BI_RGB

        buf = _reuse_buffer(stride * h, 24)
        if not gdi32.GetDIBits(mem_dc, bitmap, 0, h, buf, ctypes.byref(header), 0):
            print("[laser_sweep] GetDIBits 失败")
            return None

        raw = np.frombuffer(buf, dtype=np.uint8)
        if pad == 0:
            # 无填充：直接得到 (h, w, 3) 视图，零拷贝
            return raw[: w * h * 3].reshape(h, w, 3)
        # 有填充（宽度不是 4 的倍数 ×3 时才会发生）：切掉每行末尾的填充字节
        padded = raw[: stride * h].reshape(h, stride)
        return np.ascontiguousarray(padded[:, : w * 3]).reshape(h, w, 3)
    finally:
        if restored:
            gdi32.SetViewportOrgEx(mem_dc, 0, 0, None)
        gdi32.SelectObject(mem_dc, old_bitmap)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(handle, window_dc)


def _screencap_window(
    hwnd: int,
    region: Optional[tuple[int, int, int, int]] = None,
) -> Optional[Any]:
    """用 PrintWindow 抓取 B 窗口，返回 **BGR 的 numpy 数组**。

    region 给了就只抓那一块（客户区坐标），返回的图尺寸就是该区域大小，
    里面的坐标自然从 (0, 0) 起算 —— 调用方拿它做 OCR 时 roi 要写 [0,0,0,0]。

    为什么不用 MaaFramework 的 Win32Controller：
        MaaAgentServer.dll 里的 MaaWin32ControllerCreate 只是个空壳，调用时
        会打印 "MaaAgentServer Not implement this API, Please use MaaFramework"
        并返回空句柄。也就是说 agent 进程里根本建不了第二个控制器。
        所以这里直接走 Win32 GDI，纯 ctypes，不依赖框架。

    ⚠️ 返回的是**复用缓冲区的视图**，下一次截图会覆盖它。
       调用方必须在下次截图前用完（采样循环里正是"读一帧 → 立刻 OCR"）。
       需要长期持有请自己 .copy()。
    """
    import numpy as np

    if sys.platform != "win32":
        print("[laser_sweep] 窗口截图目前只实现了 Windows")
        return None

    _ensure_dpi_aware()
    user32, gdi32 = _win_gdi_handles()

    handle = ctypes.c_void_p(hwnd)

    # ── DPI 上下文必须和目标窗口自己的感知性一致 ──────────────────────────
    # 否则 PrintWindow 只会渲染出内容的一部分。实测真值表：
    #          线程上下文     量到的客户区     截图填充
    #   A 软件   UNAWARE      1109x600       100%
    #   A 软件   PER-MONITOR  2218x1200      100%
    #   Gauss    UNAWARE      1440x829       100%   <-- 只有这个对
    #   Gauss    PER-MONITOR  2880x1658       50%   <-- 只填左上角
    #
    # GetDpiForWindow 返回 96 表示这个窗口自己不是 DPI 感知的
    # （Windows 会给它一套虚拟化坐标系），这时就要把线程也切到 UNAWARE。
    DPI_AWARENESS_CONTEXT_UNAWARE = ctypes.c_void_p(-1)
    prev_ctx = None
    dpi = user32.GetDpiForWindow(handle)
    if not dpi or dpi == 96:
        prev_ctx = user32.SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_UNAWARE)
        # 只在第一次说明：采样时每秒十几次，刷屏没有意义
        if not _win_gdi.get("dpi_noted"):
            print(f"[laser_sweep] 目标窗口 DPI={dpi}（不感知 DPI），已切到虚拟化坐标系截图")
            _win_gdi["dpi_noted"] = True

    try:
        image = _grab_region(user32, gdi32, handle, hwnd, region, np)
        if image is None:
            print(f"[laser_sweep] PrintWindow 失败 (hwnd={hwnd})")
            return None

        # ── 快路径失效的兜底 ──────────────────────────────────────────────
        # 要 region 却得到一整块纯色 = 这个窗口不吃裁剪/视口平移
        # （DirectComposition、部分 WPF/UWP 宿主）。退回整窗渲染再裁，
        # 多花一次 GetDIBits，但保证拿到真实内容。
        if region is not None and _frame_looks_empty(image):
            if not _win_gdi.get("clip_fallback_noted"):
                print(
                    "[laser_sweep] 该窗口不响应局部渲染（只得到纯色），"
                    "已退回整窗渲染后裁剪 —— 读数正确，只是每帧稍慢"
                )
                _win_gdi["clip_fallback_noted"] = True
            full = _grab_region(user32, gdi32, handle, hwnd, None, np)
            if full is None:
                return None
            # 从整窗图里切出 ROI（这一步才有一次真实拷贝，但比每帧都拷好）
            rx, ry, rw, rh = (int(v) for v in region[:4])
            fh, fw = full.shape[:2]
            cx0, cy0 = max(0, min(rx, fw - 1)), max(0, min(ry, fh - 1))
            cw, ch = max(1, min(rw, fw - cx0)), max(1, min(rh, fh - cy0))
            return np.ascontiguousarray(full[cy0 : cy0 + ch, cx0 : cx0 + cw])
        return image
    finally:
        if prev_ctx:
            user32.SetThreadDpiAwarenessContext(prev_ctx)


# --------------------------------------------------------------------------- #
# ① 初始化
# --------------------------------------------------------------------------- #
@AgentServer.custom_action("laser_sweep_begin")
class LaserSweepBegin(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        param = _merge_scan_settings_from_task(_param(argv.custom_action_param))

        # 先把「这次跑的到底是哪份代码」写进日志。
        # 换机排查时最怕的就是「文件到底替换成功没有」—— 带上文件名、字节数和
        # 修改时间，一眼就能跟手头那份对上（2026-09-22 就为这个来回猜过一轮）。
        print(f"[laser_sweep] agent 文件：{_agent_fingerprint()}")

        start = float(param.get("start", 20))
        end = float(param.get("end", 30))
        step = float(param.get("step", 1))

        # 「扫描点数」：填 0（默认）就按步长扫；填 > 0 就以点数优先，步长由区间反算。
        # 用列表取值是为了区分「没填」和「填了 0」—— 两者都退回按步长。
        count_list = _resolve_count(param)
        count = count_list[0] if count_list else None

        if count is None:
            if step <= 0:
                print(f"[laser_sweep] step 必须为正数，当前为 {step}")
                return False
        elif count < 0:
            print(f"[laser_sweep] 扫描点数不能为负：{count}（填 0 = 按步长扫描）")
            return False
        if end < start:
            print(f"[laser_sweep] 区间不合法：start={_fmt(start)} > end={_fmt(end)}")
            return False

        values = _build_values(start, end, step, count)
        if not values:
            print("[laser_sweep] 参数序列为空")
            return False

        # 点数模式下真正的步长是反算出来的（可能与界面上填的步长输入框无关），
        # 所以单独算一份，只用于打印，免得日志里的「步长」骗人。
        if count and count > 1:
            effective_step = (end - start) / (count - 1)
            mode = f"按点数（{count} 个点，反算步长 {_fmt(effective_step)}）"
        elif count == 1:
            effective_step = step
            mode = "按点数（1 个点，只扫起始值）"
        else:
            effective_step = step
            mode = f"按步长（{_fmt(step)}）"

        # ⚠️ 只在「真的填了点数」时才比这个数。count=0 是「按步长」的开关，
        #    它和实际组数天然对不上，以前会打出一句「填的是 0 个点」的假告警。
        if count and len(values) != count:
            print(
                f"[laser_sweep] 注意：填的是 {count} 个点，按步长 {_fmt(effective_step)} "
                f"严格跑下来只有 {len(values)} 个（再下一个点会越过结束值 {_fmt(end)}，故不再生成）"
            )

        # 把区间与实际组数一并传进去：默认文件名要用它们拼成
        # 「时间_起始值_终止值_总点数」，光看名字就知道这次扫了什么。
        csv_path, out_root = _resolve_output_paths(
            param, start=start, end=end, total=len(values)
        )
        header = param.get("csv_header") or DEFAULT_HEADER

        try:
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            # 覆盖写：每次开始扫描都重建表头，避免旧数据混进新结果
            with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerow(header)
        except OSError as exc:
            print(f"[laser_sweep] 创建 CSV 失败: {exc}")
            print(f"[laser_sweep]   目标路径：{csv_path}")
            return False

        # 记下 output 总目录，收尾时「自动打开文件夹」要用它
        _state["out_root"] = out_root
        # 顺手落盘一份，好让独立的「打开输出文件夹」和图「绘制曲线图」任务
        # 也能找到同一个目录 / 同一份 CSV
        _remember_output_root(out_root, csv_path)
        # 跑完是否自动打开 output 文件夹（UI 上的开关，默认开）
        _state["open_when_done"] = _flag(param.get("open_when_done"), True)

        # 上一轮找到的 B 软件窗口可能已经关了，清掉重新找
        _window_cache.clear()

        # 尽力声明后台受管键（修饰键）；失败也不影响主流程
        _configure_controller(context)

        # 每轮等待时长：UI 上填的是「秒」，流水线用的是毫秒，在这里换算一次
        if not _apply_wait_seconds(context, param):
            return False

        # ── 读数校验：把 UI 传进来的那套参数解析一次，存进 _state ─────────
        # 为什么不等到 LaserSweepGuard 再解析：那个节点在每一轮（乃至每次重测）
        # 都会被调用，而它的 custom_action_param 可能因为 pipeline_override 没写全
        # 而缺字段。begin 时解析一次存下来，guard 取不到就用这份兜底，
        # 保证「界面上设的阈值」一定生效（这条链路漏一次，界面就成了摆设）。
        guard_param = _resolve_guard_param(param)
        _state["guard_param"] = guard_param

        # ── 把「邮件通知」任务的设置存一份，供告警路径使用 ─────────────────
        # 2026-10-28 起主任务不再挂「邮件设置」：
        #   邮件信息只在「邮件通知」任务里填一份，这里开始扫描时直接去读
        #   那份保存好的配置。这样监控告警和完成通知永远用同一份设置，
        #   主任务界面也不会再出现重复的邮件输入框。
        #
        # 节点参数里的邮件字段只作为兜底（正常不会再有），优先级最高。
        mail_seed = dict(_load_mail_settings_from_email_task() or {})
        for key in MAIL_PARAM_KEYS:
            if param.get(key) is not None and str(param.get(key)).strip() != "":
                mail_seed[key] = param[key]
        _state["mail_param"] = mail_seed
        if mail_seed:
            print(
                "[laser_sweep] 告警邮件：已从「邮件通知」任务读到设置 "
                f"{mail_seed.get('smtp_host') or '(服务器未填)'}"
                f"，收件人 {mail_seed.get('mail_to') or '(未填)'}"
                f"，授权码{'已填' if mail_seed.get('smtp_password') else '未填'}"
            )
        else:
            print(
                "[laser_sweep] ⚠️ 没从「邮件通知」任务读到邮件设置 —— "
                "读数异常时可能发不出告警信（会尝试读本地 config 文件兜底）"
            )

        sample_param = {
            key: param.get(key)
            for key in ("sample_enabled", "sample_window_sec")
            if param.get(key) is not None
        }

        _state.update(
            values=values,
            index=0,
            csv_path=csv_path,
            measured=None,
            controls=None,
            sample_param=sample_param,
            # 每次扫描都重新定位 B 软件的数值框：上次那个窗口可能已经关了、
            # 换软件了，或者窗口尺寸变了 —— 拿旧坐标去认只会认出错数。
            value_box=None,
            sample_stats=[],
            # 读数校验相关：每次扫描都从干净状态开始，
            # 否则上一轮残留的 reference/history 会让第一组就被误判成异常。
            last_measured=None,
            history=[],
            guard={
                "strikes": 0,  # 本轮连续异常次数
                "abnormal": [],  # 本轮异常读数（未通过判定的，不进 history）
                "reference": None,  # 上一个可信读数（只有判定通过的才更新）
            },
            alert={},  # 上一次告警的汇总
            sent_alerts=0,  # 本次扫描已发告警封数（notify_limit 用）
            # ⚠️ 完成通知的计数也必须重置（2026-10-27 审查发现）：
            #    mail_done_count 是"本进程已发过几封完成通知"，之前 begin 没清它。
            #    同一个 agent 进程连跑两次扫描时（很常见：改个参数再点一次开始），
            #    第二次的完成通知会因为第一次留下的 1 直接被 notify_limit=1 挡掉，
            #    日志写「已达到上限，不再发送」—— 用户只看到"这次没收到邮件"。
            mail_done_count=0,
            # 跳变留痕、采样失败计数同样属于"本次扫描"状态，必须一起清
            # （sample_stats 在上面 value_box 那边已经重置过了，这里不重复）
            jumps=[],
            sample_failures={},
            last_note="",  # 写进 CSV 备注/终止摘要的一句话
            run_start=time.monotonic(),  # 总耗时用单调时钟，不受系统改时间影响
            started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

        print(
            f"[laser_sweep] 参数序列共 {len(values)} 组：{values[0]} → {values[-1]}（"
            + mode
            + "）"
        )
        print(f"[laser_sweep] 结果文件：{csv_path}")
        print(f"[laser_sweep] 输出目录：{out_root}（本次结果在上面的时间子文件夹里，文件名 {csv_path.name}）")
        print(
            "[laser_sweep] 定位缓存：本轮开始先全屏 OCR 一次，之后每轮复用"
            "（想关掉就在节点参数里写 use_cache=false）"
        )
        # 把「这一轮到底会不会拦读数、拦到什么程度」打一行出来：
        # 现场日志里最常被问的就是「它到底生效了吗、阈值是多少」。
        if guard_param["enabled"]:
            mail_note = (
                "邮件通知"
                if guard_param.get("notify_enabled")
                else "不发邮件（可在主任务里打开「读数异常发送邮件」）"
            )
            print(
                "[laser_sweep] 读数校验：开，"
                f"绝对阈值 {_fmt(guard_param['tolerance_abs'])}，"
                f"相对 {_fmt(guard_param['tolerance_pct'])}%，"
                f"连续 {guard_param['retry_max']} 次异常则终止并"
                + mail_note
                + f"（重测等待 {_fmt(guard_param['retry_wait'])} 秒）"
            )
        else:
            print(
                "[laser_sweep] 读数校验：关，读到什么记什么"
                "（想开启就把节点参数 enabled 设为 true）"
            )

        # ── 读数采样：这一轮的「一秒读几次」是怎么定的 ────────────────────
        # 采样参数不在 Guard 里，而在读数节点自己的 custom_recognition_param 里
        # （它属于识别层，Guard 管不着）。这里只是想把它打一行日志出来。
        #
        # ⚠️ 为什么不读 begin 自己的 param 来猜：
        #    begin 节点的 custom_action_param 里**没有**采样参数 ——
        #    pipeline_override 是按节点整体替换的，少写一个字段就等于抹掉它，
        #    所以采样参数只写在 LaserSweepReadValueB 那个节点上（唯一真源）。
        #    要如实报出来，就得去问框架那个节点现在到底是什么参数。
        #    取不到（老框架 / 节点改名）就跳过这一行 —— 日志少一行不影响扫描。
        sample_param: dict[str, Any] = {}
        try:
            node_data = context.get_node_data("LaserSweepReadValueB")
        except Exception as exc:  # noqa: BLE001 - 纯日志用途
            print(f"[laser_sweep] （读不到读数节点参数，跳过采样配置打印: {exc}）")
            node_data = None
        if isinstance(node_data, dict):
            raw_param = node_data.get("custom_recognition_param")
            sample_param = _param(raw_param) if isinstance(raw_param, (str, dict)) else {}

        if sample_param:
            if _flag(sample_param.get("sample_enabled"), True):
                window_sec = _number(
                    sample_param.get("sample_window_sec"), DEFAULT_SAMPLE_WINDOW_SEC
                )
                upper = int(
                    _number(sample_param.get("sample_max_count"), DEFAULT_SAMPLE_MAX_COUNT)
                )
                upper = max(1, upper)
                print(
                    f"[laser_sweep] 读数采样：开，每个点读 {_fmt(window_sec)} 秒，"
                    f"次数由 OCR 速度决定（上限 {upper} 次/点），取平均值写入 CSV"
                )
            else:
                print("[laser_sweep] 读数采样：关，每个点只读 1 次（旧行为）")
        return True


# --------------------------------------------------------------------------- #
# ② 注入本轮参数值
# --------------------------------------------------------------------------- #
@AgentServer.custom_action("laser_sweep_apply_param")
class LaserSweepApplyParam(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        param = _param(argv.custom_action_param)
        input_node = str(param.get("input_node") or "LaserSweepTypeParam")

        values, index = _state["values"], _state["index"]

        # 兜底：万一序列已经跑完还回到这里，直接改道去收尾
        if not values or index >= len(values):
            done_node = str(param.get("done_node") or "LaserSweepDone")
            context.override_next(argv.node_name, [done_node])
            print("[laser_sweep] 参数已用尽，直接收尾")
            return True

        value = values[index]
        if not context.override_pipeline({input_node: {"input_text": value}}):
            print(f"[laser_sweep] 覆盖 {input_node}.input_text 失败")
            return False

        # ── 进入新参数点：清掉「上一点」的重测状态（2026-10-27 修）──────────
        #
        # 为什么必须清：strikes / abnormal 是**逐参数点**的连续计数，
        # 但 _state 是跨节点共享的。之前这里只清 measured，于是出现这条真实路径：
        #     点 A 异常累计到 4 次 → 本点 OCR 失败走 RecordFailed → Advance
        #     → 进入点 B，strikes 仍是 4
        #     → 点 B 只要读到 1 次偏离值，就被凑成"连续 5 次彼此一致"
        #     → _stable_readings 判为真实跳变，**直接接受**
        # 结果是点 B 的第一次读数（很可能只是噪声）没经过任何重测就被当成数据。
        #
        # ⚠️ 只清「本轮重测计数」，**不清 reference**：
        #    reference 是上一个可信读数，跨点保留才能发现"相邻两组之间跳变"。
        #    清掉它会让每个点都变成"首读建基准"，保护层直接失效。
        guard = _state.get("guard")
        if isinstance(guard, dict):
            guard["strikes"] = 0
            guard["abnormal"] = []
            guard["point_index"] = index  # 标明这份状态属于哪个参数点，便于排查
        # 顺带把"这一轮采样的失败计数"也清掉，否则上一轮的失败原因会记到这一轮
        failures = _state.get("sample_failures")
        if isinstance(failures, dict):
            failures.clear()

        _state["measured"] = None  # 清空上一轮结果
        print(f"[laser_sweep] ▶ 第 {index + 1}/{len(values)} 组：设定参数 = {value}")
        return True


# --------------------------------------------------------------------------- #
# ②b 点「锚点右侧」某个比例的位置（不写死像素）
# --------------------------------------------------------------------------- #
def _resolve_image_size(context: Context) -> Optional[tuple[int, int]]:
    """兜底：从 controller.cached_image 取当前截图尺寸。"""
    try:
        image = context.tasker.controller.cached_image
    except Exception as exc:  # noqa: BLE001
        print(f"[laser_sweep] 取 cached_image 失败: {exc}")
        return None
    shape = getattr(image, "shape", None)
    if shape is None or len(shape) < 2:
        return None
    return int(shape[1]), int(shape[0])


@AgentServer.custom_action("laser_click_right_of")
class LaserClickRightOf(CustomAction):
    """点「识别出来的那个框」——默认为框中心，也可按比例/像素再偏一点。

    v1.0.1 之前写的是「标签中心 + 图像宽度 × dx_ratio」，那个比例是照着
    1331x720 那台电脑量出来的。2026-09-22 换到另一台电脑（窗口更小）就点不到
    输入框了 —— 因为界面控件的间距并不会跟着窗口等比缩放，比例法必然失准。

    现在输入框位置由 laser_find_nearest_setting 的 return_box="input" 在
    **每一屏实时 OCR** 里夹出来，这里默认直接点框中心即可；
    dx_ratio / dy_ratio / dx_px / dy_px 仍保留，需要额外微调时再用。

    框通过节点的 target 字段传进来，会落在 argv.box。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        param = _param(argv.custom_action_param)
        box = tuple(argv.box or ())
        if len(box) != 4 or box[2] <= 0 or box[3] <= 0:
            print(f"[laser_sweep] 拿不到锚点框（argv.box={argv.box}），检查一下节点的 target 字段")
            return False

        dx_px = float(param.get("dx_px", 0.0) or 0.0)
        dy_px = float(param.get("dy_px", 0.0) or 0.0)
        dx_ratio = float(param.get("dx_ratio", 0.0) or 0.0)
        dy_ratio = float(param.get("dy_ratio", 0.0) or 0.0)

        if dx_ratio or dy_ratio:
            # 兼容老写法：再按截图宽高的比例补一点偏移
            size = _state.get("image_size") or _resolve_image_size(context)
            if size:
                img_w, img_h = size
                dx_px += dx_ratio * img_w
                dy_px += dy_ratio * img_h
                print(f"[laser_sweep]   截图尺寸 {img_w}x{img_h}，按比例补偏移")
            else:
                print("[laser_sweep]   拿不到截图尺寸，只按绝对偏移算")

        cx, cy = _center(box)
        x, y = int(round(cx + dx_px)), int(round(cy + dy_px))
        print(
            f"[laser_sweep]   目标框 {box} 中心 ({cx:.0f}, {cy:.0f})"
            f" + ({dx_px:.0f}, {dy_px:.0f}) → 点击 ({x}, {y})"
        )

        try:
            context.tasker.controller.post_click(x, y).wait()
        except Exception as exc:  # noqa: BLE001
            print(f"[laser_sweep] 点击失败: {exc}")
            return False
        return True


# --------------------------------------------------------------------------- #
# ③ 找离 "Temperature" 最近的 "Setting"
# --------------------------------------------------------------------------- #
@AgentServer.custom_recognition("laser_find_nearest_setting")
class LaserFindNearestSetting(CustomRecognition):
    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> CustomRecognition.AnalyzeResult:
        param = _param(argv.custom_recognition_param)
        ocr_node = str(param.get("ocr_node") or "LaserSweepOcrSetting")
        roi = param.get("roi") or [0, 0, 0, 0]
        anchor_texts = _anchor_texts(param) or ["Temperature"]
        target_text = str(param.get("target_text") or "Setting")
        # 界面上有多个同名标签时的钦定方式（二选一）：
        #   near_text    —— 取「离这个词最近」的那个标签（推荐，换分辨率也不受影响）
        #   anchor_index —— 按「从上到下、从左到右」直接点第几个（0 = 第一个）
        # near_text 也支持写数组，OCR 少认一个空格时可以多写几种写法兜着。
        near_texts = _anchor_texts(param, "near_text")
        anchor_index = param.get("anchor_index")
        # 标签一定是短文本；界面上那些 Tips / 日志行长得很像标签，必须挡掉
        anchor_max_chars = int(param.get("anchor_max_chars", DEFAULT_ANCHOR_MAX_CHARS))
        # "target"（默认）返回离锚点最近的按钮；"anchor" 返回那个锚点本身；
        # "input" 返回「标签与按钮之间」那个输入框的位置。
        # 三者都由第一次全屏 OCR 现场折算（不依赖任何写死的比例，换电脑/换分辨率都能跟上），
        # 之后每轮直接吃缓存 —— 想强制每次都重认，就写 use_cache=false。
        return_box = str(param.get("return_box") or "target").lower()

        # 记下当前截图的尺寸，仅作日志/兜底用；定位本身已经不依赖它了
        _remember_image_size(argv.image)
        size = _image_size_of(argv.image) or _state.get("image_size")

        # 把这次真正生效的定位配置打出来：万一流水线没换成新版，
        # 日志里一眼就能看出来（near_text 缺失 = pipeline 还是旧的）
        print(
            f"[laser_sweep]   定位参数：anchor_text={anchor_texts!r} target_text={target_text!r} "
            f"return_box={return_box} near_text={near_texts!r} anchor_max_chars={anchor_max_chars}"
        )

        # ── ★ 缓存优先 ────────────────────────────────────────────────────
        # 同一轮扫描期间窗口不会动：第一次全屏 OCR 已经把「标签 / 输入框 / 按钮」
        # 三个框都夹出来了，那么第二次（点 Setting）以及后面每一轮，
        # 没有任何理由再把同一屏认一遍 —— 而全屏 OCR 正是整个流程里最贵的一步。
        cached = _controls_fresh(param, size)
        if cached is not None:
            boxes = cached.get("boxes") or {}
            reused = boxes.get(return_box) or boxes.get("input")
            if reused:
                print(
                    f"[laser_sweep]   ♻️ 复用定位缓存（return_box={return_box}）{reused}"
                    f"；已复用 {cached.get('hits')} 次，缓存 {cached.get('age', 0):.0f} 秒"
                )
                return CustomRecognition.AnalyzeResult(
                    box=tuple(reused),
                    detail={
                        "cached": True,
                        "returned": return_box,
                        "boxes": boxes,
                        "reuse_count": cached.get("hits"),
                    },
                )
            print(f"[laser_sweep]   缓存里没有 return_box={return_box} 这个框，重新 OCR 定位")

        # ── OCR 定位 ──────────────────────────────────────────────────────
        # 正常情况下一整轮扫描只会走到这里一次（第一轮）。
        # 缓存过期 / 窗口尺寸变化后重新定位时，先用「上次位置外扩一圈」的小 ROI 试一把：
        # 窗口没动的话控件还在老位置附近，认一小块比认整屏快得多。
        tries: list[Any] = []
        hint_pad = int(_number(param.get("hint_roi_pad"), DEFAULT_HINT_ROI_PAD))
        stale = _controls_snapshot() or {}
        stale_boxes = [b for b in (stale.get("boxes") or {}).values()]
        stale_size = stale.get("size")
        hint_ok = (
            hint_pad > 0
            and stale_boxes
            and size is not None
            and stale_size is not None
            and tuple(stale_size) == tuple(size)  # 尺寸变了老坐标就没意义了
        )
        if hint_ok:
            hint = _pad_roi(stale_boxes, hint_pad, size)
            if hint[2] > 0 and hint[3] > 0:
                tries.append(hint)
        tries.append(roi)

        raw: list[tuple[tuple[int, int, int, int], str]] = []
        for attempt, try_roi in enumerate(tries):
            if attempt:
                print(f"[laser_sweep]   小 ROI 没认到东西，退回 roi={roi} 再认一次")
            raw = _ocr_hits(context, argv.image, ocr_node, [], try_roi, only_rec=False)
            if raw:
                if attempt:
                    print(f"[laser_sweep]   ✓ 小 ROI {tries[0]} 里认到了，省下一次全屏 OCR")
                break
        if not raw:
            _forget_controls("重新定位时这一屏什么都没 OCR 到")
            return CustomRecognition.AnalyzeResult(
                box=None,
                detail={"error": "这一屏没有 OCR 到任何文字", "tried_rois": tries},
            )

        anchors = _short_hits(_match_text(raw, anchor_texts), anchor_max_chars, "锚点")
        if not anchors:
            return CustomRecognition.AnalyzeResult(
                box=None,
                detail={
                    "error": f"没找到 {anchor_texts!r}",
                    "ocr_texts": [t for _, t in raw],
                },
            )

        print(
            "[laser_sweep]   锚点候选："
            + "；".join(f"{text!r}@({box[0]},{box[1]})" for box, text in anchors[:8])
        )

        # ── 同名标签太多时，用参照词钦定 ──────────────────────────────────
        if near_texts and len(anchors) > 1:
            hints = _short_hits(
                _match_text(raw, near_texts), max(anchor_max_chars, 32), "参照词"
            )
            if hints:
                chosen_box, chosen_text = _choose_anchor_by_hint(anchors, hints)
                print(
                    f"[laser_sweep]   按 near_text={near_texts!r} 钦定锚点 {chosen_text!r} @ {chosen_box}"
                    f"（参照词命中 {len(hints)} 处："
                    + "；".join(f"{t!r}@({b[0]},{b[1]})" for b, t in hints[:4])
                    + "）"
                )
                anchors = [(chosen_box, chosen_text)]
            else:
                print(
                    f"[laser_sweep]   ⚠️ 没找到参照词 {near_texts!r}，"
                    "退回按「与按钮的配对距离」挑，可能挑错行"
                )

        # ── 或者干脆数着来：0 = 最上面那个 ────────────────────────────────
        if anchor_index is not None:
            ordered = sorted(anchors, key=lambda item: (item[0][1], item[0][0]))
            try:
                wanted_index = int(anchor_index)
            except (TypeError, ValueError):
                wanted_index = -1
            if 0 <= wanted_index < len(ordered):
                anchors = [ordered[wanted_index]]
                print(
                    f"[laser_sweep]   按 anchor_index={wanted_index} 直接钦定 "
                    f"{ordered[wanted_index][1]!r} @ {ordered[wanted_index][0]}"
                )
            else:
                print(f"[laser_sweep]   ⚠️ anchor_index={anchor_index} 越界（共 {len(ordered)} 个锚点），已忽略")

        # 按钮候选同样只认短文本 —— 这一条是 2026-09-22 最后一环的元凶：
        # A 软件下方那行提示
        #   "[2026.09.22-15:49:35] Tips：Setting TEC Temperature Successfully!"
        # 既含 "Temperature"（冒充锚点）又含 "Setting"（冒充按钮），
        # 而且它离锚点的距离²=15493，比真按钮 (726,292) 的 44945 还近，
        # 于是「点 Setting」变成了「点那行提示」，最后落到 (228, 420)。
        target_max_chars = int(param.get("target_max_chars", DEFAULT_TARGET_MAX_CHARS))
        candidates = _short_hits(_match_text(raw, [target_text]), target_max_chars, "按钮")
        if not candidates:
            return CustomRecognition.AnalyzeResult(
                box=None,
                detail={
                    "error": f"找到了 {anchor_texts!r} 但没找到 {target_text!r}",
                    "ocr_texts": [t for _, t in raw],
                },
            )

        print(
            "[laser_sweep]   按钮候选："
            + "；".join(f"{text!r}@({box[0]},{box[1]})" for box, text in candidates[:8])
        )

        # 全局最小配对：每个锚点各自找最近的按钮，再取整体距离最小的那一对。
        # 这样做是因为界面上经常有多个同名文字（例如左侧状态栏也有一个 "Temperature"），
        # 只取第一个锚点会挑错行；按「谁离按钮最近」来选，才能锁定真正要操作的那一行。
        best: Optional[tuple[float, tuple, str, tuple, str]] = None
        for a_box, a_text in anchors:
            nearest_box, nearest_text = min(
                candidates, key=lambda hit: _distance_sq(a_box, hit[0])
            )
            dist = _distance_sq(a_box, nearest_box)
            print(
                f"[laser_sweep]   锚点 {a_text!r} @ {a_box} "
                f"→ 最近 {nearest_text!r} @ {nearest_box}  距离²={dist:.0f}"
            )
            if best is None or dist < best[0]:
                best = (dist, a_box, a_text, nearest_box, nearest_text)

        assert best is not None
        dist, anchor_box, anchor_text_used, best_box, best_text = best
        print(f"[laser_sweep]   选中：锚点 {anchor_text_used!r} @ {anchor_box} → 按钮 {best_text!r} @ {best_box}")

        # 输入框每次都由**这一屏**的「标签框 + 按钮框」现场夹出来（不写死比例），
        # 顺带把三个框一起存进缓存：后面每一轮 return_box=input 就不必再 OCR 了。
        input_box = _input_box_between(anchor_box, best_box, raw)

        if return_box == "input":
            chosen_box = input_box
            returned = "input"
        elif return_box == "anchor":
            chosen_box = anchor_box
            returned = "anchor"
            print(f"[laser_sweep]   按 return_box=anchor 返回锚点框 {chosen_box}")
        else:
            chosen_box = best_box
            returned = "target"

        _remember_controls(anchor_box, best_box, input_box, size)

        return CustomRecognition.AnalyzeResult(
            box=chosen_box,
            detail={
                "anchor": anchor_text_used,
                "anchor_box": list(anchor_box),
                "chosen": best_text,
                "chosen_box": list(best_box),
                "returned": returned,
                "input_box": list(input_box),
                "distance_sq": round(dist, 1),
                "anchor_count": len(anchors),
                "candidate_boxes": [list(b) for b, _ in candidates],
                "cached": False,
            },
        )


# --------------------------------------------------------------------------- #
# ④ 读取 B 软件测量值 —— 1 秒窗口里反复读、取平均
#
# 为什么改成这样（2026-10-27 主人要求）：
#   旧读法是「截一帧 → 整屏 OCR → 取那个数」，一次约 5.5 秒，而真正被记录的只是
#   其中某一瞬间的值。B 软件的读数本身就在抖（同一组里差 0.05 是常态，和组间
#   差异同量级），单次采样等于随手抓了其中一个：抓到大的一侧还是小的一侧全凭
#   运气。11 组扫下来，曲线上的起伏有一半来自读数噪声，而不是设定参数真的改变了
#   被测对象。
#
# 现在的流程（三步）：
#   ① 定位：整屏 OCR 找到「锚点」和它旁边的「数值框」，一次约 5.5 秒。找到之后
#      把框记进 _state["value_box"]，之后每轮直接复用（和 A 软件的控件定位缓存
#      是同一个思路）。
#   ② 采样：在 **1 秒**的时间窗里反复做「截新帧 → 只认数值框那一小块」。
#      一秒能读几次**不写死** —— 让 OCR 的实际速度自己决定：快机器几十毫秒
#      一次就读十几次，慢机器一次几百毫秒就读两三次；连一次都超过 1 秒
#      （整屏 OCR 的旧路径）时就是 1 次，行为与旧版一致，不会更差。
#   ③ 聚合：这一秒里的采样取**平均数**写进 CSV（见 _aggregate_samples）：
#      刷新抖动被平均掉，噪声不再冒充信号。
#
# 为什么是 1 秒：仪表刷新一般 100~500 毫秒一帧，1 秒能覆盖三四帧，足够把抖动
# 平均掉；再长就会把设备真实的爬升也抹平，而且 11 组扫描会从 3 分钟涨到十几分钟。
#
# 时间账（本机实测口径）：
#   · 第一轮：整屏定位 5.5 秒 + 采样 1 秒 ≈ 6.5 秒（旧版 5.5 秒）
#   · 之后每轮：只认数值框，约 1.1~1.3 秒（旧版 5.5 秒）→ 整轮反而更快
#   · 读数异常后的自动重测同样受益：重测一次约 4 秒（旧版 9 秒）
#
# ⚠️ 采样失败不会让整轮失败：一次没认到数字就跳过这次，窗到点就把已采到的取平均；
#    一次都没采到才判这一轮识别失败，交给流水线的 on_error 写空值占位。
# --------------------------------------------------------------------------- #
class _Reading(NamedTuple):
    """一次读数的结果：数值 + 它在图上的位置 + 它是被哪条规则找到的。"""

    value: float
    box: tuple[int, int, int, int]
    text: str
    label: str = ""
    layout: str = ""
    gap: float = 0.0


def _pad_box(
    box: Any,
    pad: int,
    size: Optional[tuple[int, int]],
) -> Optional[list[int]]:
    """把单个框外扩 pad 像素并裁到图像范围内，返回 [x, y, w, h]。

    外扩是必须的：OCR 对「贴边」的文字识别率明显下降，数值框如果正好卡在
    字符边缘上，多认几次就会时不时漏一次。宁可多认一圈背景。
    """
    try:
        x, y, w, h = (int(v) for v in tuple(box)[:4])
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None

    x0, y0 = x - pad, y - pad
    x1, y1 = x + w + pad, y + h + pad
    if size:
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(int(size[0]), x1), min(int(size[1]), y1)
    else:
        x0, y0 = max(0, x0), max(0, y0)
    if x1 <= x0 or y1 <= y0:
        return None
    return [x0, y0, x1 - x0, y1 - y0]


def _median(values: list[float]) -> float:
    """中位数（不动点排序，不依赖 numpy）。"""
    ordered = sorted(float(v) for v in values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def _aggregate_samples(
    samples: list[float],
    abs_tol: float,
    pct_tol: float,
) -> tuple[Optional[float], dict[str, Any]]:
    """把一秒里采到的多个值合成一个：圈出正常簇、剔掉明显误读，再取平均。

    为什么不直接 sum/len：
        这一层平均的目的是压掉**刷新抖动**；而 OCR 偶尔会把 -79.720 认成 -797.20
        或 -7.9720（README 里有一整张误读形态表）。这种偏离动辄 10 以上，
        混进平均里会把整组的平均值拉歪到不可用 —— 平均值反而成了放大器。
    所以规则是（只用中位数，不拟合、不建模型）：
        ① 取中位数 m；
        ② 允许带 = m ± max(abs_tol, |m| × pct_tol / 100)；
        ③ 带外的判为误读丢弃，带内的取**算术平均**；
        ④ 万一全被丢掉（样本少且离得远，例如 [0, 100]），退回全部样本的平均 ——
           宁可记一个可疑值交给读数守卫（LaserSweepGuard）去处理，
           也不要在这里制造一个「读不到」。
    abs_tol / pct_tol 默认 3.0 / 1%，和读数守卫同量级：正常抖动 ±0.05 远远在带内，
    而「认错一位数字」那种 ±5 以上的偏离会被丢掉。
    """
    info: dict[str, Any] = {"count": len(samples)}
    if not samples:
        return None, info

    median = _median(samples)
    band = max(max(0.0, abs_tol), abs(median) * max(0.0, pct_tol) / 100.0)
    kept = [v for v in samples if abs(v - median) <= band]
    dropped = [v for v in samples if abs(v - median) > band]
    if not kept:
        kept = list(samples)
        dropped = []

    mean = sum(kept) / len(kept)
    info.update(
        median=median,
        kept=len(kept),
        dropped=dropped,
        band=band,
        mean=mean,
        minimum=min(kept),
        maximum=max(kept),
        span=max(kept) - min(kept),
    )
    return mean, info


# ── 数值框缓存 ────────────────────────────────────────────────────────────
# 有了它，一轮扫描里只有第一组要付「整屏 OCR 5.5 秒」，之后每秒只管那一小块。
# 三个作废条件，任何一个成立就重新整屏定位（不拿旧坐标硬认）：
#   ① 截图尺寸变了（窗口被移动/缩放/改分辨率）
#   ② 窗口正则 / 锚点词 / roi 变了（换了软件或改了配置）
#   ③ 采样时整块都认不出数字（可能布局变了）
def _value_box_signature(param: dict[str, Any]) -> str:
    """数值框缓存的身份：配置一变就不该复用旧框。"""
    return "|".join(
        [
            str(param.get("window_regex") or ""),
            ",".join(_anchor_texts(param)),
            str(param.get("roi") or ""),
            str(param.get("prefer") or "auto"),
        ]
    )


def _value_box_cached(
    size: Optional[tuple[int, int]],
    param: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """取数值框缓存；配置或截图尺寸对不上就返回 None（并说明原因）。"""
    cached = _state.get("value_box")
    if not isinstance(cached, dict):
        return None

    if cached.get("signature") != _value_box_signature(param):
        print("[laser_sweep]   数值框缓存对应的配置变了（窗口/锚点/roi），需要重新定位")
        return None

    cached_size = cached.get("size")
    if size and cached_size and tuple(cached_size) != tuple(size):
        print(
            f"[laser_sweep]   截图尺寸变了（缓存 {tuple(cached_size)} → 现在 {tuple(size)}），"
            "数值框缓存作废"
        )
        return None

    box = cached.get("box")
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    return cached


def _is_narrow_box(box: Any, size: Optional[tuple[int, int]]) -> bool:
    """这个框够不够小，值得每秒反复「只认它」？

    判定的是**框本身**，不是「配没配锚点词」：
        · 有锚点词时，框是锚点旁边那个数值的框 —— 一定窄
        · 没配锚点词时，框是整屏 OCR 里第一个像数值的文本的框 —— 同样窄
        · 唯一的例外是 OCR 把整屏当成一段文字返回（框≈整屏），
          这时"只认这一块"等于每次都整屏，不如老老实实走慢路径。

    为什么要判：fast 决定采样时走「窄 ROI（几十毫秒）」还是「整屏（几秒）」，
    判错了要么白等、要么把一个快路径当成慢路径用，一秒里只能读一次。
    """
    if not size:
        return True  # 尺寸不明时按窄处理：读得快总是好事，读不出会自己回退
    try:
        _, _, bw, bh = (float(v) for v in tuple(box)[:4])
    except (TypeError, ValueError):
        return False
    screen = float(size[0]) * float(size[1])
    if screen <= 0:
        return True
    # 占整屏 45% 以上就当"整屏"处理（正常数值框连 1% 都占不到）
    return (bw * bh) / screen < 0.45


def _remember_value_box(
    box: Any,
    detail: dict[str, Any],
    size: Optional[tuple[int, int]],
    param: dict[str, Any],
    fast: Optional[bool] = None,
) -> dict[str, Any]:
    """把「数值框」记下来，之后每秒只认这一小块。

    fast 省略时按框的大小自动判断（见 _is_narrow_box）。
    """
    if fast is None:
        fast = _is_narrow_box(box, size)
    info = {
        "box": [int(v) for v in tuple(box)[:4]],
        "layout": str(detail.get("layout") or ""),
        "anchor": str(detail.get("anchor") or ""),
        "size": tuple(size) if size else None,
        "signature": _value_box_signature(param),
        # fast=False 表示「这个框几乎就是整屏」（OCR 把整屏当成一段文字返回），
        # 这时采样只能每次整屏 OCR，次数自然就少，但绝不算错。
        "fast": bool(fast),
    }
    _state["value_box"] = info
    where = "锚点旁边那一小块" if fast else "整屏（这一屏的框太大，按整屏认）"
    print(f"[laser_sweep]   📌 数值框已记住 {info['box']}（{where}），本轮之后每秒只认它")
    return info



def _forget_value_box(reason: str = "") -> None:
    """丢掉数值框缓存（认不出数字 / 布局可能变了时用）。"""
    if _state.get("value_box") is None:
        return
    _state["value_box"] = None
    print("[laser_sweep]   数值框缓存已清空" + (f"：{reason}" if reason else ""))


def _fresh_screencap(context: Context) -> Any:
    """重新截一帧主控制器的画面（没配 window_regex、退用 A 画面时用）。

    ⚠️ argv.image 是**进入节点那一刻**的截图；采样期间每秒要好几帧，
    所以要主动找控制器再要一张。取不到就返回 None，调用方沿用上一帧 ——
    采样宁可少一次快照，也不能因为一次截图失败把整轮读数判成失败。
    """
    try:
        controller = context.tasker.controller
        job = controller.post_screencap()
        if job is None:
            return None
        job.wait()
        return job.get()
    except Exception as exc:  # noqa: BLE001 - 截图失败只降级，不抛
        print(f"[laser_sweep]   重新截图失败（沿用上一帧）: {exc}")
        return None


def _read_region(
    context: Context,
    param: dict[str, Any],
    image: Any,
    region: Any,
) -> tuple[Optional[_Reading], dict[str, Any]]:
    """在给定的一块区域里认数字 —— 采样快路径的核心。

    调用方式有两种，都很快（不涉及整屏文本检测）：
        · 采样时：image 就是"只渲染了数值框"的那张小图，region 传整张图的矩形，
          此时 roi = 整图 = 数值框，一个字都不多认
        · 缓存刚命中时：image 是整窗图，region 传数值框坐标

    为什么比 _read_full_screen 快两个数量级：
        · roi 只覆盖数值框 —— 检测阶段要扫的面积小了三个数量级
        · only_rec=True —— 跳过文本检测，整块当一段文字直接识别
    """
    ocr_node = str(
        param.get("sample_ocr_node") or param.get("ocr_node") or DEFAULT_SAMPLE_OCR_NODE
    )
    pad = int(_number(param.get("sample_roi_pad"), DEFAULT_SAMPLE_ROI_PAD))
    size = _image_size_of(image)
    if region is None:
        # 「整张图就是要认的那块」—— 采样快路径专用。
        # 这张图已经是按数值框外扩后渲染出来的，**不能再扩一次**：
        # 再 pad 只会让 roi 越出图边界，白算一遍（裁回图内不报错，但没意义）。
        # 也不做 only_rec=False 的兜底重试 —— 先按最省的路径认，认不到交给上层统计。
        roi = [0, 0, int(size[0]), int(size[1])] if size else None
    else:
        roi = _pad_box(region, pad, size)
    if roi is None:
        return None, {"error": f"识别区域不合法: {region!r}"}

    pattern = re.compile(str(param.get("pattern") or DEFAULT_PATTERN))
    value_max_chars = int(param.get("value_max_chars", DEFAULT_VALUE_MAX_CHARS))
    min_digit_ratio = float(param.get("min_digit_ratio", DEFAULT_MIN_DIGIT_RATIO))

    hits = _ocr_hits(context, image, ocr_node, [], roi, only_rec=True)
    used_only_rec = True
    if not hits:
        # only_rec 把整块当成一个词，框切得太紧时会认不出来；
        # 退回「检测 + 识别」在同一个**小 ROI** 里再试一次（仍然比整屏快得多）。
        hits = _ocr_hits(context, image, ocr_node, [], roi, only_rec=False)
        used_only_rec = False
    if not hits:
        return None, {"error": "数值框里没 OCR 到内容", "roi": roi}

    best: Optional[tuple[tuple[int, float], tuple, str, float]] = None
    texts: list[str] = []
    for hit_box, text in hits:
        texts.append(str(text))
        match = pattern.search(str(text))
        if match is None:
            continue
        stripped = str(text).strip()
        if len(stripped) > value_max_chars or _digit_ratio(stripped) < min_digit_ratio:
            continue
        value = float(match.group())
        # 只认那一块时结果本来就只有一个；退回检测时可能有好几个，取离原框最近的。
        # ⚠️ region 可能是 None（"整张图就是要认的那块"）：这时没有"原框中心"可比，
        #    直接按返回顺序取。
        #    2026-10-27 实机事故：这里对 None 做 tuple(region) 会抛 TypeError，
        #    而异常被 _sample_loop 的 except 吞掉，表现为「16 次采样全部静默失败、
        #    日志却显示每秒 16.5 次」—— 见 _note_sample_failure 的注释。
        if used_only_rec or region is None:
            key = (0, 0.0)
        else:
            key = (1, _distance_sq(tuple(region), tuple(hit_box)))
        if best is None or key < best[0]:
            best = (key, tuple(hit_box), text, value)

    if best is None:
        return None, {"error": "数值框里没有像数值的文本", "roi": roi, "ocr_texts": texts[:8]}

    _, hit_box, text, value = best
    return (
        _Reading(value=value, box=hit_box, text=str(text)),
        {"mode": "ocr", "roi": roi, "only_rec": used_only_rec},
    )


def _read_full_screen(
    context: Context,
    param: dict[str, Any],
    image: Any,
) -> tuple[Optional[_Reading], dict[str, Any]]:
    """整屏 OCR：找锚点 → 找它旁边的数值。返回 (读数, 诊断信息)。

    这是「定位」那一步，也是那 5.5 秒的来源；一轮扫描里**只在第一次**做
    （之后走 _state["value_box"]，只认那一小块）。失败时返回 (None, 原因)。
    """
    ocr_node = str(param.get("ocr_node") or DEFAULT_SAMPLE_OCR_NODE)
    roi = param.get("roi")
    anchor_texts = _anchor_texts(param)

    # 要找「离某个词最近的数值」就必须做全屏检测+识别，
    # 只识别（only_rec）会把整块 ROI 当成一段文字，是找不到单个数值的。
    only_rec_param = param.get("only_rec")
    only_rec = (not anchor_texts) if only_rec_param is None else bool(only_rec_param)
    if anchor_texts and only_rec:
        print("[laser_sweep]   提示：配了 anchor_text 又开 only_rec，建议把 only_rec 设为 false")

    hits = _ocr_hits(context, image, ocr_node, [], roi, only_rec=only_rec)
    if not hits:
        return None, {"error": "OCR 未识别到内容"}

    pattern = re.compile(str(param.get("pattern") or DEFAULT_PATTERN))
    value_max_chars = int(param.get("value_max_chars", DEFAULT_VALUE_MAX_CHARS))
    min_digit_ratio = float(param.get("min_digit_ratio", DEFAULT_MIN_DIGIT_RATIO))

    # 候选 = 既能解析出数字、又「长得像数值」的 OCR 结果。
    # 光有数字是不够的：B 软件下方日志区那行
    #   "code=-1073807265, VISA Write in MY TSL-510_Initialize.vi->..."
    # 也含数字，而且紧贴着上一行（边缘间距只有 1px），
    # 不把这种长句子挡掉，它就会以 1px 的优势抢走真正的 -79.720dBm。
    candidates: list[tuple[tuple, str, float]] = []
    skipped: list[str] = []
    for box, text in hits:
        match = pattern.search(text)
        if match is None:
            continue
        stripped = text.strip()
        if len(stripped) > value_max_chars or _digit_ratio(stripped) < min_digit_ratio:
            skipped.append(stripped)
            continue
        candidates.append((box, text, float(match.group())))

    if skipped:
        print(
            f"[laser_sweep]   按「≤{value_max_chars} 字 且 数字占比 ≥{min_digit_ratio:.2f}」"
            f"滤掉 {len(skipped)} 条不像数值的文本"
        )

    if not candidates:
        return None, {
            "error": f"识别到的内容里没有像数值的文本: {[t for _, t in hits][:12]}",
        }

    if not anchor_texts:
        box, text, value = candidates[0]
        return (
            _Reading(value=value, box=tuple(box), text=text),
            {"mode": "ocr", "no_anchor": True},
        )

    # ---- 找 anchor_text 旁边的那个数值 ----
    # 锚点同样只认短文本：B 软件日志区的
    #   "Start Wavelength 1548.000000rm not in range[...],please check!"
    # 含 "Wavelength"，不挡掉就会冒充成锚点。
    anchor_max_chars = int(param.get("anchor_max_chars", DEFAULT_ANCHOR_MAX_CHARS))
    anchors = _short_hits(_match_text(hits, anchor_texts), anchor_max_chars, "锚点")
    if not anchors:
        return None, {
            "error": f"没找到 {anchor_texts!r}",
            "ocr_texts": [t for _, t in hits],
        }

    print(
        "[laser_sweep]   锚点候选："
        + "；".join(f"{text!r}@({box[0]},{box[1]})" for box, text in anchors[:8])
    )

    # 位置感知的挑选：先「锚点右侧同一行」，再「锚点正下方同一列」，
    # 两条都不成立才退回「中心距离最近」。
    #
    # 为什么不能只用中心距离：锚点文字自己可能含数字。B 软件的标签是
    # "功率CH1"，宽松正则在**标签本身**上就能匹配出 "1"，中心距离 = 0，
    # 于是每轮都选中标签自己，测量值恒定 = 1（2026-09-22 换电脑后实测踩到）。
    prefer = str(param.get("prefer") or "auto").lower()
    layouts: list[str] = []
    if prefer in ("auto", "right"):
        layouts.append("right")
    if prefer in ("auto", "below"):
        layouts.append("below")

    # 两种布局的候选放一起、用「边缘最短距离」统一比远近。
    # 不能「右侧整层优先」：旧 B 软件（GaussianBeam）数值在正下方，
    # 但它右边同一行也可能有别的小数字，整层优先就会挑错。
    scored: list[tuple[float, tuple, str, tuple, str, float, str]] = []
    for layout in layouts:
        for a_box, a_label in anchors:
            picked = (
                _pick_right_side(a_box, candidates)
                if layout == "right"
                else _pick_below(a_box, candidates)
            )
            for _, box, text, value in picked:
                gap = _edge_gap(a_box, box)
                scored.append((gap, a_box, a_label, box, text, value, layout))
                print(
                    f"[laser_sweep]   {layout} 布局候选：{a_label!r} @ {a_box}"
                    f" → {text!r} @ {box}（边缘间距 {gap:.0f}px）"
                )

    best = min(scored, key=lambda item: item[0]) if scored else None

    if best is not None:
        gap, anchor_box, label, box, text, value, layout = best
        side = "右侧" if layout == "right" else "下方"
        print(
            f"[laser_sweep]   B 软件：{label!r} {side}的数值 {text!r}"
            f" → 读数 {_fmt(value)}（相邻 {gap:.0f}px）"
        )
        return (
            _Reading(
                value=value,
                box=tuple(box),
                text=text,
                label=label,
                layout=layout,
                gap=gap,
            ),
            {
                "mode": "ocr",
                "anchor": label,
                "anchor_box": list(anchor_box),
                "layout": layout,
                "gap": round(gap, 1),
            },
        )

    # ---- 兜底：位置规则全落空时才退回老写法，并明确告警 ----
    print(
        "[laser_sweep]   ⚠️ 没找到「在锚点旁边」的数值，退回按中心距离挑最近的一个，"
        "结果不一定准；建议检查 anchor_text 是否写对"
    )
    fallback: Optional[tuple[float, tuple, str, tuple, str, float]] = None
    for a_box, a_label in anchors:
        for box, text, value in candidates:
            dist = _distance_sq(a_box, box)
            if fallback is None or dist < fallback[0]:
                fallback = (dist, a_box, a_label, box, text, value)

    assert fallback is not None
    dist, anchor_box, label, box, text, value = fallback
    print(
        f"[laser_sweep]   B 软件：离 {label!r} 最近的数值 {text!r}"
        f" → 读数 {_fmt(value)}  (距离²={dist:.0f})"
    )
    return (
        _Reading(value=value, box=tuple(box), text=text, label=label, layout="nearest"),
        {
            "mode": "ocr",
            "anchor": label,
            "anchor_box": list(anchor_box),
            "distance_sq": round(dist, 1),
            "fallback": True,
        },
    )


def _note_sample_failure(reason: str) -> None:
    """记下「这一次采样没读到数」，聚合成一条日志。

    为什么不能逐次 print：采样窗里每秒要试十几次，每次都打一行会把日志刷爆，
    真有问题反而淹没在噪声里。所以只累计计数，收尾时用一行说明
    「试了 N 次、成功 M 次、失败原因 Top1」。

    ⚠️ 这个函数的存在本身就是一次事故的产物（2026-10-27 实机运行）：
        日志显示「1 秒采样：1 次（1.03 秒，约 61 毫秒/次 ≈ 16.5 次/秒）」——
        attempts 是 17，成功却只有 1 次（那个 1 还是定位时顺带拿到的 seed）。
        也就是说 **16 次采样全部失败，却一个字都没打**，日志看起来完全正常。
        用户拿到的 CSV 里每行都是单点采样值，而他以为做了 1 秒平均。
        这类"静默降级"是最难发现的，所以失败必须留痕。
    """
    counter = _state.get("sample_failures")
    if not isinstance(counter, dict):
        counter = {}
        _state["sample_failures"] = counter
    key = reason[:60]
    counter[key] = int(counter.get(key) or 0) + 1


def _sample_loop(
    reader: Any,
    *,
    window_sec: float,
    max_count: int,
    slow_sec: float,
    seed: Optional[float] = None,
) -> tuple[list[float], dict[str, Any]]:
    """在一个时间窗里反复调 reader()，采到多少个就算多少个。

    「一秒读几次」不写死的原因：这台机器 OCR 到底多快，只有跑起来才知道
    （同一份代码在工控机和开发机上能差 5 倍）。所以判据是**时间**不是**次数**：
    只要窗还没到点、次数没到上限，就一直读；读得慢自然就少几次。

    三条边界：
        · 至少读 1 次（窗再短、OCR 再慢，也得有一个数）
        · 单次读数只要花了 slow_sec 以上，立刻收手 —— 说明这台机器的 OCR
          比窗还慢，再凑次数只会把这一轮从 1 秒拖成十几秒
        · 次数上限 max_count 是防呆：参数被误填成 0 或很大时不至于失控

    seed 是「窗开始之前刚读到的那一次」（定位时顺手拿到的），有就计入样本 ——
    它读的是同一个框、同一时刻附近的值，白扔一次没有意义。
    """
    samples: list[float] = []
    if seed is not None:
        samples.append(float(seed))

    durations: list[float] = []
    attempts = 0
    slow = False
    started = time.monotonic()

    while attempts < max_count:
        # ⚠️ 时间判断放在**这次采样开始之前**，而不是之后：
        #    放在之后的话，一次 0.9 秒的采样结束后 elapsed 才 0.9 < 1.0，
        #    于是又读一次、总共 1.8 秒 —— 窗口被拖成了两倍。
        #    放在之前，语义就变成"只在这个窗内开始新的采样"：
        #    最后一次可能略微超出，但总时长不会成倍超出去。
        if attempts and time.monotonic() - started >= window_sec:
            break

        t0 = time.monotonic()
        try:
            value = reader()
        except Exception as exc:  # noqa: BLE001 - 单次采样失败不该中断整轮
            print(f"[laser_sweep]   ⚠️ 一次采样出错，已跳过: {exc}")
            value = None
        took = time.monotonic() - t0
        durations.append(took)
        attempts += 1

        if value is not None:
            samples.append(float(value))

        if took >= slow_sec:
            slow = True
            break

    elapsed = time.monotonic() - started
    stats = {
        "attempts": attempts,
        "seconds": elapsed,
        "per_sample": (sum(durations) / len(durations)) if durations else 0.0,
        "samples": list(samples),
        "slow": slow,
        "seeded": seed is not None,
        "rate": (attempts / elapsed) if elapsed > 0 else 0.0,
    }
    return samples, stats


@AgentServer.custom_recognition("laser_read_value_b")
class LaserReadValueB(CustomRecognition):
    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> CustomRecognition.AnalyzeResult:
        param = _param(argv.custom_recognition_param)
        # 折叠分组时子 option 的 pipeline_override 不会合并进来；
        # begin 已从任务配置里读到采样开关/时长，存在 _state["sample_param"] 里。
        sample_state = _state.get("sample_param")
        if isinstance(sample_state, dict):
            for key in ("sample_enabled", "sample_window_sec"):
                if sample_state.get(key) is not None:
                    param[key] = sample_state[key]
        mode = str(param.get("mode", "fixed")).lower()

        if mode == "fixed":
            value = float(param.get("fixed_value", 0.0))
            _remember_measured(value)
            print(f"[laser_sweep]   B 软件未接入，使用占位测量值 = {_fmt(value)}")
            return CustomRecognition.AnalyzeResult(
                box=tuple(argv.roi),
                detail={"mode": "fixed", "measured": value},
            )

        if mode != "ocr":
            return CustomRecognition.AnalyzeResult(
                box=None,
                detail={"error": f"未知的 mode: {mode}（只支持 fixed / ocr）"},
            )

        # 优先单独截 B 软件的画面；没配 window_regex 就退回主控制器的截图（也就是 A 的画面）
        image = argv.image
        window = _find_window(param)
        if window is not None:
            hwnd, title, _class_name = window
            shot = _screencap_window(hwnd)
            if shot is None:
                return CustomRecognition.AnalyzeResult(
                    box=None,
                    detail={"error": f"B 软件窗口截图失败: {title!r}"},
                )
            image = shot
            # 这里按**窗口客户区尺寸**算 size（而不是 shot 的尺寸）：
            # 采样快路径会只截数值框那一小块，此时 shot 的尺寸远小于窗口，
            # 而数值框缓存要用窗口尺寸判断"窗口有没有被挪动/缩放"。
            client = _client_size(hwnd)
            size = client or _image_size_of(shot)
            print(
                f"[laser_sweep]   B 软件窗口 {title!r} 截图 "
                f"{shot.shape[1]}x{shot.shape[0]}（客户区 {size[0]}x{size[1]}）"
            )

            def capture(region: Optional[tuple[int, int, int, int]] = None) -> Any:
                """采样期间每帧重新抓 B 窗口，拿的才是最新画面。

                region 给了就只渲染那一块（客户区坐标）—— 这是采样快路径的关键：
                PrintWindow 的绘制量和随后的 GetDIBits 都只跟这一小块成正比，
                而不是每秒十几次把整个 1440x829 重画并搬 3.6MB。
                """
                return _screencap_window(hwnd, region)

            def frame_size() -> Optional[tuple[int, int]]:
                return size

            # 自建 GDI 截图支持按区域渲染 → 允许走快路径
            can_region_grab = True

        else:

            def capture(region: Optional[tuple[int, int, int, int]] = None) -> Any:
                """没配 window_regex 时退回主控制器：每帧重新截一张。

                ⚠️ 主控制器的截图是**整屏**的，没法只渲一块 ——
                所以这个分支的采样只能整屏 OCR，一秒通常只读 1 次。
                想让采样起作用，就把 window_regex 配上（B 软件窗口标题）。

                ⚠️⚠️ 这个函数**忽略 region 参数**，返回的永远是整屏图。
                2026-10-27 审查发现的问题：上层 reader 会把"capture(region) 的返回值"
                当成"已裁好的小图"、以 region=None 交给 _read_region，于是对整屏做
                only_rec 并取第一个数字 —— 读到的是屏幕上任意位置的值（实测拿到 23.5，
                而正确的读数是 -79.72），聚合后把整点数据污染成 23.5。
                修法见 can_region_grab 标志：这个分支明确声明"我不能局部截图"，
                上层据此关掉快路径，改走整屏 OCR + 数值框坐标。
                """
                return _fresh_screencap(context)

            def frame_size() -> Optional[tuple[int, int]]:
                return _image_size_of(image)

            # 主控制器只能整屏截图 → 不允许走"只渲染数值框"的快路径
            can_region_grab = False

        return self._analyze_by_ocr(
            context, param, image, capture, frame_size, can_region_grab
        )

    def _relocate(
        self,
        context: Context,
        param: dict[str, Any],
        image: Any,
        size: Optional[tuple[int, int]],
        want_sample: bool,
        capture: Any,
    ) -> tuple[Optional[_Reading], dict[str, Any]]:
        """定位数值框：先用上次位置附近的小区域试，认不到才整屏 OCR。

        为什么要分两档：整屏 OCR 实测中位 5.5 秒，是整条读数链路里最贵的一步。
        而"缓存失效"绝大多数情况下窗口根本没动（只是尺寸判断触发、或某一次
        没认到数字），控件还在原处 —— 这时认一小块只要零点几秒。

        这和 A 软件那边 `hint_roi_pad` 是同一套思路（见 LaserFindNearestSetting），
        只是 B 软件这条路径之前一直没接上。
        """
        stale = _state.get("value_box")
        hint_pad = int(_number(param.get("hint_roi_pad"), DEFAULT_HINT_ROI_PAD))

        if (
            isinstance(stale, dict)
            and hint_pad > 0
            and isinstance(stale.get("box"), (list, tuple))
            and len(stale["box"]) == 4
        ):
            hint = _pad_box(stale["box"], hint_pad, size)
            if hint is not None:
                print(
                    f"[laser_sweep]   先按上次位置外扩 {hint_pad}px 重试："
                    f"ROI {hint}（省掉一次整屏 OCR）"
                )
                # ① 先按"这一块就是数值框"认（only_rec，最快）
                one, one_detail = _read_region(context, param, image, hint)
                if one is not None:
                    # 小区域里认到了。
                    #
                    # ⚠️ 但**不能**直接把 stale.box 当成新框（2026-10-27 审查发现）：
                    #    hint 命中只证明"这个区域里有个数字"，不证明"它还在老位置"。
                    #    窗口尺寸变化导致缓存失效时，布局往往也变了；如果顺手把
                    #    旧坐标连同新尺寸重新缓存，接下来整轮采样都会认错位置。
                    #
                    #    正确做法：把 OCR 实际返回的框（one.box，图内坐标）换算回
                    #    客户区坐标，作为新的数值框 —— 这才是"它在哪"的证据。
                    hit_box = tuple(int(v) for v in one.box[:4]) if one.box else None
                    if hit_box and hit_box[2] > 0 and hit_box[3] > 0:
                        # one.box 是相对图内原点（= hint 左上角）的坐标，平移回去
                        new_box = [
                            hit_box[0] + hint[0],
                            hit_box[1] + hint[1],
                            hit_box[2],
                            hit_box[3],
                        ]
                        print(
                            f"[laser_sweep]   ✓ 小区域里认到了 {one.text!r}，"
                            f"新数值框 {new_box}（不必整屏 OCR）"
                        )
                    else:
                        new_box = list(stale["box"])
                        print(
                            f"[laser_sweep]   ✓ 小区域里认到了 {one.text!r}，"
                            f"但 OCR 没给出框，沿用上次的 {new_box}"
                        )
                    return (
                        _Reading(
                            value=one.value,
                            box=tuple(new_box),
                            text=one.text,
                            label=one.label,
                            layout=one.layout,
                        ),
                        {**one_detail, "hinted": True, "value_box": new_box},
                    )

                # ② 小区域里也认不到，才退回整屏（只在真的变了布局时才付这 5.5 秒）
                print("[laser_sweep]   小区域没认到，退回整屏 OCR")

        return _read_full_screen(context, param, image)

    def _analyze_by_ocr(
        self,
        context: Context,
        param: dict[str, Any],
        image: Any,
        capture: Any,
        frame_size: Any = None,
        can_region_grab: bool = True,
    ) -> CustomRecognition.AnalyzeResult:
        """先定位数值框，再在时间窗里反复读它，最后取平均。

        frame_size() 返回**窗口客户区尺寸**（可能为 None）。它和 image 的尺寸
        不一定相同：采样快路径只截数值框那一小块，图很小、窗口没变。
        数值框缓存的失效判断必须用窗口尺寸，否则"只截小块"会被误判成
        "窗口缩小了 → 缓存作废 → 每秒重新整屏定位一次"（那就白优化了）。

        can_region_grab 表示 capture 是否**真的**会按 region 裁剪：
            · 自建 GDI 截图（配了 window_regex）→ True，可走"只渲染数值框"快路径
            · 主控制器截图 → False，它只会返回整屏图。
              2026-10-27 审查发现：这个分支若仍走快路径，reader 会把整屏图
              当成"已裁好的小图"、以 region=None 交给 _read_region，
              于是对**整屏**做 only_rec 并取第一个数字 ——
              实测读到屏幕上别处的 23.5（正确值 -79.72），整点数据被污染。
              所以这里必须显式关掉快路径。
        """
        size = (frame_size() if callable(frame_size) else None) or _image_size_of(image)

        want_sample = _flag(param.get("sample_enabled"), True)
        window_sec = _number(param.get("sample_window_sec"), DEFAULT_SAMPLE_WINDOW_SEC)
        if window_sec < 0:
            print(f"[laser_sweep]   ⚠️ 采样时长不能为负（{param.get('sample_window_sec')!r}），已按 0 处理")
            window_sec = 0.0
        max_count = int(_number(param.get("sample_max_count"), DEFAULT_SAMPLE_MAX_COUNT))
        if max_count < 1:
            print(f"[laser_sweep]   ⚠️ 采样次数上限至少为 1（收到 {param.get('sample_max_count')!r}），已按 1 处理")
            max_count = 1
        slow_sec = _number(param.get("sample_slow_sec"), DEFAULT_SAMPLE_SLOW_SEC)
        if slow_sec <= 0:
            slow_sec = DEFAULT_SAMPLE_SLOW_SEC

        # ── ① 先读一次：数值框缓存命中就只认那一小块，否则定位 ──────────────
        box_info = _value_box_cached(size, param) if want_sample else None
        reading: Optional[_Reading] = None
        detail: dict[str, Any] = {}

        if box_info is not None:
            reading, detail = _read_region(context, param, image, box_info["box"])
            if reading is not None:
                detail["value_box"] = list(box_info["box"])
            else:
                print("[laser_sweep]   数值框缓存这次没认到数字，丢掉缓存重新整屏定位")
                _forget_value_box("数值框里没认到数字")
                box_info = None

        # ── ① 先读一次：数值框缓存命中就只认那一小块，否则定位 ──────────────
        # 定位分两档，贵的那个尽量不碰：
        #   · 有"上次的框"（缓存因尺寸/配置变化作废）→ 先用它外扩一圈的小区域试一次
        #     （hint_roi_pad，默认 240px）。窗口没动的话控件还在老位置附近，
        #     认一小块比认整屏快一个数量级（整屏 5.5 秒 vs 小区域零点几秒）。
        #   · 小区域也认不到，才退回整屏 OCR。
        # 这和 A 软件那边的 hint 策略是同一套思路，只是之前没接到 B 软件上。
        box_info = _value_box_cached(size, param) if want_sample else None
        reading: Optional[_Reading] = None
        detail: dict[str, Any] = {}

        if box_info is not None:
            reading, detail = _read_region(context, param, image, box_info["box"])
            if reading is not None:
                detail["value_box"] = list(box_info["box"])
            else:
                print("[laser_sweep]   数值框缓存这次没认到数字，丢掉缓存重新定位")
                _forget_value_box("数值框里没认到数字")
                box_info = None

        if reading is None:
            reading, detail = self._relocate(
                context, param, image, size, want_sample, capture
            )
            if reading is None:
                return CustomRecognition.AnalyzeResult(box=None, detail=detail)
            if want_sample:
                # fast 由框的大小自动判断（见 _is_narrow_box），不依赖配没配锚点词
                box_info = _remember_value_box(reading.box, detail, size, param)

        # ── ② 不采样：老行为，读到什么记什么 ────────────────────────────────
        if not want_sample or window_sec <= 0:
            _remember_measured(reading.value)
            print(f"[laser_sweep]   B 软件读数 = {_fmt(reading.value)}（未开启 1 秒多点采样）")
            detail.update(mode="ocr", measured=reading.value, samples=[reading.value])
            return CustomRecognition.AnalyzeResult(box=reading.box, detail=detail)

        # ── ③ 时间窗里反复读同一个框 ───────────────────────────────────────
        # fast=True  → 每帧只渲染数值框那一小块再 OCR（几十毫秒一次，一秒读好几次）
        # fast=False → 框几乎就是整屏（OCR 把整屏当成一段文字返回），
        #              只能每次整屏 OCR，一秒通常读 1 次 —— 行为与旧版一致，不会更差。
        #
        # ⚠️ can_region_grab 是硬前提：capture 不能真按 region 裁剪时，
        #    "fast" 就无从谈起 —— 它只会返回整屏图，被当成小图去认就会读错位置。
        #    2026-10-27 审查实测：那个分支读到屏幕上别处的 23.5。
        fast = bool(box_info and box_info.get("fast")) and bool(can_region_grab)
        box = list(box_info["box"]) if box_info else None

        # 数值框外扩一圈再抓：和 OCR 用的 ROI 保持一致，免得"抓图不含外扩、
        # OCR 又往外扩"时边缘被判成越界。外扩后裁进窗口客户区。
        pad = int(_number(param.get("sample_roi_pad"), DEFAULT_SAMPLE_ROI_PAD))
        grab_region: Optional[tuple[int, int, int, int]] = None
        if fast and box is not None:
            padded = _pad_box(box, pad, size)
            if padded is not None:
                grab_region = (padded[0], padded[1], padded[2], padded[3])

        def reader() -> Optional[float]:
            nonlocal image
            if grab_region is not None:
                # 快路径：只渲染数值框那一块。抓回来的图很小，
                # 里面的坐标从 (0,0) 起算，所以 OCR 时不再传 roi。
                fresh = capture(grab_region)
                if fresh is None:
                    _note_sample_failure("抓图返回空")
                    return None
                image = fresh
                # ⚠️ roi 传 None 而不是 (0,0,w,h)：_read_region 内部还会按
                #    sample_roi_pad 再外扩一次，而这张图**已经**是外扩过的，
                #    再扩就会把坐标推到图外（裁回图内虽然不报错，但会白算一遍）。
                #    传 None 表示"整张图就是目标区域"，_read_region 会直接用整图。
                one, diag = _read_region(context, param, image, None)
                if one is None:
                    _note_sample_failure(str(diag.get("error") or "未知原因"))
                return None if one is None else one.value

            # 慢路径：整屏抓图 + 整屏 OCR（没配 window_regex，或框大到等于整屏）
            fresh = capture()
            if fresh is not None:
                image = fresh
            one, diag = _read_full_screen(context, param, image)
            if one is None:
                _note_sample_failure(str(diag.get("error") or "未知原因"))
            return None if one is None else one.value

        samples, stats = _sample_loop(
            reader,
            window_sec=window_sec,
            max_count=max_count,
            slow_sec=slow_sec,
            seed=reading.value,
        )

        # 整窗一个数都没采到：多半是布局变了 / 窗口挪了，丢掉缓存按老路整屏重来一次。
        # 这一步只在快路径上做 —— 慢路径本来每次都是整屏，重来一次结果也一样。
        if not samples and fast:
            print("[laser_sweep]   ⚠️ 这一窗一个数都没采到，丢掉数值框缓存，整屏重读一次")
            _forget_value_box("整窗采样全失败")
            whole = capture()
            if whole is not None:
                image = whole
            again, again_detail = _read_full_screen(context, param, image)
            if again is not None:
                reading = again
                detail = again_detail
                box_info = _remember_value_box(
                    again.box, again_detail, _image_size_of(image), param, True
                )
                samples = [again.value]
                stats["rescued"] = True

        # ── ④ 聚合：剔掉明显误读后取平均 ───────────────────────────────────
        abs_tol = _number(param.get("sample_outlier_abs"), DEFAULT_SAMPLE_OUTLIER_ABS)
        pct_tol = _percent_value(
            param.get("sample_outlier_pct"), DEFAULT_SAMPLE_OUTLIER_PCT
        )
        mean, agg = _aggregate_samples(samples, abs_tol, pct_tol)
        if mean is None:
            _forget_value_box("采样结果为空")
            return CustomRecognition.AnalyzeResult(
                box=None,
                detail={"error": "1 秒采样一个数都没读到", "sample_stats": stats},
            )

        _remember_measured(mean)
        # 采样统计留一份：收尾摘要/排查时能看出「这一台机器一秒到底能读几次」
        history = _state.get("sample_stats")
        if not isinstance(history, list):
            history = []
            _state["sample_stats"] = history
        history.append({**stats, **{k: v for k, v in agg.items() if k != "kept"}})
        del history[:-50]  # 只留最近 50 轮，别让 _state 无限长大

        per = stats["per_sample"]
        rate = stats["rate"]
        extra = ""
        if agg["dropped"]:
            extra = (
                f"；剔除 {len(agg['dropped'])} 个疑似误读 "
                + "、".join(_fmt(v) for v in agg["dropped"][:4])
            )
        # 把「每帧到底渲染了多大」也报出来：现场一眼就能确认优化有没有生效
        # （数字远小于客户区 = 走的是"只渲数值框"快路径）
        grab_note = ""
        if grab_region is not None:
            gw, gh = grab_region[2], grab_region[3]
            grab_note = f"；每帧只渲染 {gw}x{gh} 像素"
        elif fast is False:
            grab_note = "；整窗 OCR（未配 window_regex 或框太大）"
        print(
            f"[laser_sweep]   B 软件 1 秒采样：{len(samples)} 次"
            f"（{stats['seconds']:.2f} 秒，约 {per * 1000:.0f} 毫秒/次 ≈ {rate:.1f} 次/秒）"
            f"，范围 {_fmt(agg['minimum'])} ~ {_fmt(agg['maximum'])}"
            f"，平均 = {_fmt(mean)}{extra}{grab_note}"
        )

        # ── ⭐ 采样失败必须显式报出来（2026-10-27 实机事故）───────────────
        # 事故现场：日志写「1 秒采样：1 次（1.03 秒，约 61 毫秒/次 ≈ 16.5 次/秒）」，
        # 看着完全正常，其实 attempts=17、成功只有 1 次，16 次全部静默失败。
        # 用户以为做了 1 秒平均，实际每行都是单点值 —— 这类"静默降级"最坑。
        # 现在：只要失败次数 > 0 就单独打一行，并且把失败原因说清楚。
        failures = _state.get("sample_failures")
        attempts = int(stats.get("attempts") or 0)
        ok_count = len(samples) - (1 if stats.get("seeded") else 0)
        failed = max(0, attempts - ok_count)
        if failed > 0:
            top = ""
            if isinstance(failures, dict) and failures:
                worst = max(failures.items(), key=lambda kv: kv[1])
                top = f"；最常见原因：{worst[0]}（{worst[1]} 次）"
            ratio = failed / attempts * 100 if attempts else 0
            print(
                f"[laser_sweep]   ⚠️ 这一窗尝试 {attempts} 次，成功 {ok_count} 次、"
                f"失败 {failed} 次（{ratio:.0f}%）{top}"
            )
            if ok_count <= 1:
                print(
                    "[laser_sweep]   ⚠️ 采样基本没成功，CSV 记的是单点值（不是平均值）。"
                    "排查方向：数值框是否准（看上面「数值框已记住」那行的坐标）、"
                    "B 软件是否被遮挡导致截图是空白"
                )
            # 计数别跨轮累积，否则下一轮会被历史的失败次数误导
            if isinstance(failures, dict):
                failures.clear()
        if stats.get("slow"):
            print(
                "[laser_sweep]   （这台机器的 OCR 单次就超过 "
                f"{_fmt(slow_sec)} 秒，一秒里只能读 1 次；数据照常记录）"
            )

        detail.update(
            mode="ocr",
            measured=mean,
            samples=[float(v) for v in samples],
            sample_count=len(samples),
            sample_seconds=round(stats["seconds"], 3),
            sample_per_sec=round(rate, 2),
            sample_dropped=agg["dropped"],
            value_box=list(box_info["box"]) if box_info else None,
        )
        return CustomRecognition.AnalyzeResult(box=reading.box, detail=detail)

# --------------------------------------------------------------------------- #
# ④b 读数守卫：这个数到底可不可信？
#
# 它在流水线里的位置：LaserSweepReadValueB → **LaserSweepGuard** → 三条出口之一
#
#     record_node  → 可信，正常写 CSV（零额外开销）
#     retry_node   → 存疑，等 retry_wait 秒后重读**同一个点**
#     stop_node    → 连续 retry_max 次都不可信，终止扫描并发告警
#
# 三个出口都用 context.override_next 现改，写法和 LaserSweepAdvance 改 done_node
# 一样。⚠️ 出口一定要 return True：返回 False 会走节点的 on_error
# （也就是 LaserSweepRecordFailed），那不是「终止」，而是「写个空行继续跑下一组」——
# 恰恰是这一层要拦的事。
# --------------------------------------------------------------------------- #
@AgentServer.custom_action("laser_sweep_guard")
class LaserSweepGuard(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        # 参数来源两处，节点参数优先：
        #   ① 本节点的 custom_action_param（正常情况；界面经 pipeline_override 传下来）
        #   ② laser_sweep_begin 在开跑时解析并存下的那份（兜底）
        #
        # 为什么要有 ②：pipeline_override 对对象是**整体替换**，少写一个字段就等于
        # 把它从原 pipeline 里抹掉。真出现这种情况时，节点参数里就没有阈值了，
        # 而 _resolve_guard_param 会静默退回默认值 —— 用户在界面上改的阈值看着
        # 像生效、实际没生效，是最难查的一类问题。begin 那份是直接从 UI 参数
        # 解析来的，用它补齐缺的字段，界面就真正说了算。
        raw = _param(argv.custom_action_param)
        fallback = _state.get("guard_param")
        if isinstance(fallback, dict) and fallback:
            # begin 已从任务配置读到折叠分组参数时，以它为准（折叠只是隐藏 UI，
            # 不能让子 option 不参与 pipeline 合并后退回节点默认值）。
            if _state.get("scan_settings_from_task"):
                merged = dict(raw)
                merged.update({k: v for k, v in fallback.items() if v is not None})
                raw = merged
            else:
                merged = dict(fallback)
                merged.update({k: v for k, v in raw.items() if v is not None})
                raw = merged
        param = _resolve_guard_param(raw)
        node = str(getattr(argv, "node_name", "") or "LaserSweepGuard")
        record_node = param["record_node"]

        # ── 出口一：校验被关掉 ────────────────────────────────────────────
        # 排查「是不是守卫误判」时用。关掉之后行为和旧版完全一致，
        # 不比较、不重测，读到什么记什么。
        if not param["enabled"]:
            context.override_next(node, [record_node])
            print("[laser_sweep] 读数校验已关闭，直接记录")
            return True

        # ── 出口一（同上）：没读到值 ──────────────────────────────────────
        # 识别失败由流水线的 on_error → LaserSweepRecordFailed 处理，轮不到这一层。
        # 真走到这里说明识别"成功"了却没有值，属于状态异常，交给 Record 写空值占位
        # 比在这里自作主张更安全 —— 保护层绝不能制造新的故障点。
        measured = _state.get("measured")
        if measured is None:
            context.override_next(node, [record_node])
            print("[laser_sweep] 本轮没有读数，交给 Record 节点按空值处理")
            return True

        current = float(measured)
        values = list(_state.get("values") or [])
        index = int(_state.get("index") or 0)
        total = len(values)
        set_value = values[index] if 0 <= index < total else "?"

        guard = _state.get("guard")
        if not isinstance(guard, dict):
            guard = {}
            _state["guard"] = guard
        # strikes = 本轮**连续**异常次数；正常一次就归零（见下面的出口二）
        strikes = int(guard.get("strikes") or 0)
        abnormal: list[float] = guard.get("abnormal") if isinstance(guard.get("abnormal"), list) else []

        # ── 基准值：只有「判定通过过的读数」才有资格当基准 ────────────────
        # ⚠️ 这里有个**必须绕开的坑**（2026-10-26 实测发现）：
        #    识别层每次读数都会调 _remember_measured()，它把 measured 和
        #    last_measured 一起写成**当前这个值**。所以「优先用 last_measured 当基准」
        #    在真实流程里等于「拿当前值跟它自己比」—— 差值恒为 0，守卫永远放行，
        #    加了等于没加。自检里之所以没暴露，是因为自检是手工把 last_measured
        #    填成"上一组的值"再调用的，跟生产路径不一样。
        #    所以顺序改成：guard["reference"]（唯一可信来源）
        #      → last_measured（**仅当它确实不同于当前值时**才算历史值）
        #      → history 最后一个（上一次真正记录下来的正常值）。
        reference = guard.get("reference")
        if reference is None:
            last = _state.get("last_measured")
            if last is not None:
                try:
                    last_value = float(last)
                except (TypeError, ValueError):
                    last_value = None
                # 与当前值相同 = 它是"这一次读到的"，不是"上一次的"，不能当基准
                if last_value is not None and last_value != current:
                    reference = last_value
            if reference is None:
                history_raw = _state.get("history")
                if isinstance(history_raw, list) and history_raw:
                    try:
                        reference = float(history_raw[-1])
                    except (TypeError, ValueError):
                        reference = None

        if reference is None:
            # 首次读数：没有基准，直接当基准并放行
            guard["reference"] = current
            guard["strikes"] = 0
            dict_abnormal = abnormal  # 保留引用，仅用于下面的 clear
            dict_abnormal.clear()
            history = _state.get("history")
            if not isinstance(history, list):
                history = []
                _state["history"] = history
            history.append(current)
            context.override_next(node, [record_node])
            print(
                f"[laser_sweep] 第 {index + 1}/{total} 组（设定 {set_value}）"
                f"首个读数 {_fmt(current)}，作为基准，先记录"
            )
            return True

        reference = float(reference)
        ok, abs_diff, rel_pct = _tolerance_ok(
            current, reference, param["tolerance_abs"], param["tolerance_pct"]
        )

        def describe() -> str:
            rel = "∞" if rel_pct == float("inf") else f"{rel_pct:.2f}%"
            return f"基准 {_fmt(reference)}，当前 {_fmt(current)}，绝对差 {_fmt(abs_diff)}，相对差 {rel}"

        # ── 出口二：正常 ──────────────────────────────────────────────────
        if ok:
            strikes = 0
            guard["strikes"] = 0
            guard["reference"] = current
            abnormal.clear()
            history = _state.get("history")
            if not isinstance(history, list):
                history = []
                _state["history"] = history
            history.append(current)
            context.override_next(node, [record_node])
            print(
                f"[laser_sweep] ✅ 第 {index + 1}/{total} 组（设定 {set_value}）读数正常："
                f"{describe()}"
            )
            return True

        # ── 出口三：异常，但还没到上限 → 去重测 ──────────────────────────
        strikes += 1
        guard["strikes"] = strikes
        guard["reference"] = reference  # 基准不动：仍拿上一个可信值比
        abnormal.append(current)
        guard["abnormal"] = abnormal
        # 只保留最近若干个别值，避免异常很多时日志/邮件被刷屏
        if len(abnormal) > 32:
            del abnormal[:-32]

        # ── ⭐ 先判「这是不是真实的跳变」（2026-10-27 新增，用真实数据换来的）──
        #
        # 为什么需要这一层：阈值只能判断"和上一个点差多少"，判断不了
        # "这个新值是真的还是读错了"。实测 201 组扫描里，第 176/177 两组
        # 的读数被判异常并**整组判废**，但它们的 4~5 次重测彼此高度一致
        # （跨度只有 0.23 和 0.10），而且下一组（178）就回到了基准附近 ——
        # 这是设备真实的一次跳变，不是 OCR 误读。结果两个点被白白丢掉。
        #
        # 判据（只看"重测之间稳不稳"，不引入任何拟合）：
        #     连续 retry_max 次读数，如果它们**彼此**很接近（跨度 ≤ 允许带），
        #     就说明读到的不是随机噪声 —— 噪声不会连着 5 次落在同一处。
        #     这时接受这个新值作为基准，并如实记录（因为设备真的在那儿）。
        #
        # 反过来说，如果是 OCR 误读 / 通信抖动，5 次读数会各说各话，跨度很大，
        # 这一条就不会触发，保护层照旧起作用。
        #
        # ⚠️ 只看**最近 retry_max 个**读数，不看整个 abnormal 列表：
        #    abnormal 是"这一组从第一次失败起"累积的，里面可能混着更早的
        #    真正噪声（那几次已经各自去重测了）。拿全量去判"稳不稳"，
        #    等于让早先的噪声污染当前的判断 —— 2026-10-27 自检里就撞到这个：
        #    序列 -95/-797/-8/-85/-14 的**最后 5 个**恰好都不同，却能凑出
        #    一个"跨度很小"的假象。取窗口才是对语义的正确实现。
        recent = abnormal[-int(param["retry_max"]):]
        if strikes >= param["retry_max"] and _stable_readings(
            recent,
            param.get("jump_abs_tol", DEFAULT_JUMP_ABS_TOL),
            param.get("jump_pct_tol", DEFAULT_JUMP_PCT_TOL),
        ):
            newest = float(abnormal[-1])
            print(
                f"[laser_sweep] ⚠️ 第 {index + 1}/{total} 组（设定 {set_value}）"
                f"连续 {strikes} 次读数都偏离基准，但它们彼此一致"
                f"（跨度 {_fmt(max(recent) - min(recent))}）"
            )
            print(
                f"[laser_sweep]   → 判为**稳定但待核验的跳变**：接受 {_fmt(newest)} 作为新基准并记录"
            )
            # 诚实说明这条判据的效力边界（2026-10-27 审查意见）：
            #   同一 OCR、同一框位的 5 次结果**不是 5 份独立证据**，
            #   它们只能证明"可重复"，不能证明"真实"。
            #   如果 OCR 把同一个错值稳定地读错（比如小数点稳定错位），
            #   这里也会接受它 —— 所以这个点会被标进 CSV「备注」列和完成摘要，
            #   让人知道"这一行不是常规读数"。
            print(
                "[laser_sweep]   ⚠️ 注意：这只证明读数「可重复」，不证明它「真实」。"
                "该点已在 CSV 备注列与完成摘要中标记为需复核"
            )
            guard["strikes"] = 0
            guard["abnormal"] = []
            guard["reference"] = newest
            _state["guard"] = guard
            history = _state.get("history")
            if not isinstance(history, list):
                history = []
                _state["history"] = history
            history.append(newest)
            _state["measured"] = newest
            _state["last_note"] = f"连续{strikes}次偏离但彼此一致，判为真实跳变"
            # 记一笔"这次是被判为跳变的"，收尾摘要里可以看到
            jumped = _state.get("jumps")
            if not isinstance(jumped, list):
                jumped = []
                _state["jumps"] = jumped
            jumped.append({"index": index + 1, "set": set_value, "value": newest,
                           "from": reference, "strikes": strikes})
            context.override_next(node, [record_node])
            return True

        if strikes < param["retry_max"]:
            retry_node = param["retry_node"]
            # 重测等待：把秒数写进重测节点的 post_delay（毫秒）。
            # 覆写失败不算致命 —— 节点自己有个默认 post_delay，
            # 大不了等的是默认值；绝不能让"改延时失败"变成流程失败。
            millis = int(round(float(param["retry_wait"]) * 1000))
            # ⚠️ millis == 0 也要覆盖（2026-10-27 审查发现）：
            #    之前只有 millis > 0 才 override_pipeline，填 0 时只是打印
            #    "立即重测"，实际节点仍保留默认 3000 毫秒（或上一次写进去的延时）——
            #    用户想"不等待、立刻重读"却白等了 3 秒，而且现象是"重测怎么这么慢"，
            #    很难联想到是这里。
            #    明确写 0 才是"立即重测"。
            if context.override_pipeline({retry_node: {"post_delay": millis}}):
                wait_text = (
                    f"等 {_fmt(param['retry_wait'])} 秒后重测" if millis > 0 else "立即重测（重测等待为 0）"
                )
            else:
                print(
                    f"[laser_sweep] ⚠️ 覆盖 {retry_node}.post_delay 失败，"
                    "沿用该节点现有的等待时长"
                )
                wait_text = "按节点现有延时重测"

            context.override_next(node, [retry_node])
            print(
                f"[laser_sweep] ⚠️ 第 {index + 1}/{total} 组（设定 {set_value}）读数异常"
                f"（第 {strikes}/{param['retry_max']} 次）：{describe()} → {wait_text}"
            )
            return True

        # ── 出口三（到顶）：连续异常 N 次 → 发告警，然后按设置决定停还是继续 ──
        #
        # 2026-10-27 起默认**不再终止**（主人的要求）：
        #   「异常出现了邮件发给我，但我未必有时间处理，最合适的是继续测试」
        # 所以这里先照常发告警邮件，再根据 continue_on_abnormal 分流：
        #   True  → 当前这组记空值、继续扫下一个点（整轮扫描能跑完）
        #   False → 走原来的终止节点（旧行为，界面上可以选回去）
        # 两种都会留下告警邮件，区别只在要不要把扫描停在半路。
        continue_on_abnormal = bool(param.get("continue_on_abnormal", DEFAULT_CONTINUE_ON_ABNORMAL))
        tail = "继续扫描下一个点" if continue_on_abnormal else "准备终止扫描"
        print(
            f"[laser_sweep] ❌ 第 {index + 1}/{total} 组（设定 {set_value}）"
            f"连续 {strikes} 次读数异常，达到上限 {param['retry_max']}，{tail}"
        )
        print(f"[laser_sweep]   {describe()}")

        elapsed = _elapsed_seconds()
        info = _compose_alert(
            values=values,
            index=index,
            history=list(_state.get("history") or []),
            abnormal=list(abnormal),
            reference=reference,
            tolerance_abs=param["tolerance_abs"],
            tolerance_pct=param["tolerance_pct"],
            elapsed_sec=elapsed or 0.0,
            csv_path=_state.get("csv_path"),
            started_at=str(_state.get("started_at") or ""),
            retry_max=param["retry_max"],
            continuing=continue_on_abnormal,
        )
        info["guard"] = {
            "strikes": strikes,
            "retry_max": param["retry_max"],
            "reference": reference,
            "abnormal": list(abnormal),
        }
        info["elapsed_sec"] = elapsed
        info["index"] = index
        info["set_value"] = set_value
        info["continue_on_abnormal"] = continue_on_abnormal

        # 主任务里的「读数异常发送邮件」开关关掉时，就只写日志、不发信。
        # 注意这里**不是**静默跳过：日志里要明确说「已按设置不发」，否则
        # 用户看着「异常了却没收到信」会以为邮件通道坏了。
        #
        # ⚠️ 邮件参数**在这里才解析**（而不是每轮都解析）：
        #    扫描是逐点循环的，把 SMTP 配置解析塞进每轮正常路径，
        #    就等于为一年可能只用一次的告警功能、每轮都付一次读盘的钱。
        #    主人明确要求「没异常就迅速进入下一步」，所以这笔钱留到真出事时再付。
        if not param.get("notify_enabled", True):
            print("[laser_sweep] 已按设置跳过告警邮件（主任务的「读数异常发送邮件」是关的）")
            result = {"ok": False, "channel": "off", "error": "notify_enabled=false"}
        else:
            # ⚠️ 邮件参数不能直接用 Guard 自己的 param：
            #    Guard 节点的 custom_action_param 里只有阈值/次数/开关，
            #    **没有** smtp_* / mail_to（2026-10-27 重构时全部搬到「邮件设置」了）。
            #    2026-10-27 实测事故：这里直接传 param，导致
            #        「缺少配置：smtp_host、smtp_user、mail_to，通知降级为 off」
            #    告警静默不发 —— 而同一轮扫描结束时的「完成通知」却发得出去
            #    （那条路径拿到的是「邮件设置」的完整参数），症状极难自查。
            #
            #    所以这里显式把「邮件设置」那一份合进来：
            #    优先用本次扫描开始时存下的（begin 阶段解析的完整参数），
            #    没有就退回本地 config 文件（_load_mail_config），
            #    两者都没有才会真的判"缺配置"。
            mail_param = _resolve_mail_param(_alert_mail_param(raw))
            result = _send_alert(mail_param, info)

        info.update(result)
        # 把这一轮的校验参数也留一份，终止节点/界面要展示
        info["tolerance_abs"] = param["tolerance_abs"]
        info["tolerance_pct"] = param["tolerance_pct"]
        # notify_mode 由 _send_alert 的返回值带回（channel/ok/error），
        # 这里不再从 param 取 —— 邮件参数是刚才按需解析出来的，不在 param 里
        info.setdefault("notify_mode", "off")
        _state["alert"] = info

        if continue_on_abnormal:
            # ── 继续：这一组记空值，跳到下一个参数点 ────────────────────
            # 为什么要在这里就写 CSV，而不是交给 LaserSweepRecord：
            #   Guard 的出口是三选一，而要"补一行空值"这一次不能再经过 Record，
            #   否则会写两行（2026-10-27 实测事故：第 176/177 行各写了两次，
            #   CSV 从 201 行变成 203 行，事后核对行号与设定参数全对不上）。
            #   而「行号必须与设定参数一一对应」是本项目的硬约定（事后翻表才能
            #   看出第几组废了），所以必须就地补一行，然后**跳过 Record 直接推进**。
            _state["last_note"] = f"连续{strikes}次异常，按设置继续扫描"
            measured_before = _state.get("measured")
            _state["measured"] = None  # Record 节点靠它，这里也顺手对齐
            written = _append_row(None)
            if measured_before is not None:
                print(f"[laser_sweep]   本组读数 {_fmt(float(measured_before))} 已判废，"
                      "CSV 写空值占位；原值保留在告警邮件里")
            if not written:
                print("[laser_sweep]   ⚠️ 空值占位没写进去，行号可能与参数错位")

            # 清掉本轮状态，让下一个点从干净的基准开始 ——
            # 不清的话，下一个点的读数会继续拿这个坏点当基准，连锁误判。
            guard["strikes"] = 0
            guard["abnormal"] = []
            guard["reference"] = None
            _state["guard"] = guard

            # ⚠️ 这里**必须跳到推进节点**，不能跳 record_node：
            #    空值行刚才已经写过了，再走一次 Record 就是重复写。
            #    跳过 Record、直接 Advance 的效果与"Record 之后正常推进"完全一致
            #    （Advance 只负责 index+1 与收尾判断，不碰 CSV）。
            advance_node = str(param.get("advance_node") or DEFAULT_ADVANCE_NODE)
            print(
                f"[laser_sweep] → 按设置继续扫描下一个点"
                f"（可在「扫描设置」里改成出现异常就停止）；空值行已写，跳过 Record 直接推进"
            )
            context.override_next(node, [advance_node])
            return True

        context.override_next(node, [param["stop_node"]])
        print(f"[laser_sweep] → 转到终止节点 {param['stop_node']}")
        return True


# --------------------------------------------------------------------------- #
# ④c 连续异常终止：写空值占位 + 摘要 + 停止整个任务
# --------------------------------------------------------------------------- #
@AgentServer.custom_action("laser_sweep_abnormal_stop")
class LaserSweepAbnormalStop(CustomAction):
    """把「这一组废了」这件事落成数据，然后停下来。

    为什么不用 return False 结束：返回 False 会走节点的 on_error，
    而这里的 on_error 兜的是 LaserSweepRecordFailed —— 那条路会继续扫下一组，
    与"设备已不可信、必须停"的判断正好相反。所以这里一律 return True，
    靠 context.tasker.post_stop() 把整个任务停掉。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        param = _param(argv.custom_action_param)
        guard_state = _state.get("guard_param") if isinstance(_state.get("guard_param"), dict) else {}
        retry_max = int(_number(param.get("retry_max"), guard_state.get("retry_max", DEFAULT_RETRY_MAX)))
        if retry_max < 1:
            retry_max = DEFAULT_RETRY_MAX

        alert = _state.get("alert") if isinstance(_state.get("alert"), dict) else {}
        guard = _state.get("guard") if isinstance(_state.get("guard"), dict) else {}
        strikes = int(guard.get("strikes") or alert.get("guard", {}).get("strikes") or retry_max)
        abnormal = guard.get("abnormal") if isinstance(guard.get("abnormal"), list) else []
        if not abnormal:
            abnormal = alert.get("abnormal") if isinstance(alert.get("abnormal"), list) else []
        reference = guard.get("reference", alert.get("reference"))

        values = list(_state.get("values") or [])
        index = int(_state.get("index") or 0)
        total = len(values)
        set_value = values[index] if 0 <= index < total else "?"
        csv_path = _state.get("csv_path")

        print("[laser_sweep] ✗ 读数连续异常，扫描在" f"第 {min(index + 1, total) if total else index + 1}/{total} 组终止")

        # ① 当前这组写空值占位：保证 CSV 行号与参数序列一一对应 ——
        #    这样事后翻表能明确看出「第 7 组是空的 = 那组读数出了问题」，
        #    而不是行错位、把后面的值全对应错。
        _state["last_note"] = f"连续{strikes}次异常，已停止扫描"
        measured_before = _state.get("measured")
        # 这一组不写"可疑的读数"，只写空值；原值留在日志和告警里备查。
        # _append_row 读的就是 _state["measured"]，所以这里临时置空、写完立刻还原，
        # 免得把"上一组的实测值"从状态里抹掉（收尾/告警还要用它）。
        _state["measured"] = None
        wrote = _append_row(None)
        _state["measured"] = measured_before
        if not wrote:
            print("[laser_sweep]   ⚠️ 空值占位没写进 CSV（详见上面的原因），请手工核对行数")

        # ② 终止摘要
        elapsed = _elapsed_seconds()
        print("[laser_sweep] ── 终止摘要 ──────────────────────────────")
        print(
            f"[laser_sweep]   进度：第 {min(index + 1, total) if total else index + 1}/{total} 组"
            f"（设定参数 {set_value}，已完成 {min(index, total)} 组）"
        )
        print(f"[laser_sweep]   总耗时：{_fmt_elapsed(elapsed)}")
        if abnormal:
            print(
                f"[laser_sweep]   异常读数（{len(abnormal)} 次）："
                + "、".join(_fmt(float(v)) for v in abnormal[-10:])
            )
        else:
            print("[laser_sweep]   异常读数：见上方守卫日志")
        expected_text = str(alert.get("expected") or "").strip()
        if expected_text:
            print(f"[laser_sweep]   期望值范围：{expected_text}")
        if alert:
            channel = "email" if alert.get("channel") == "email" else "off"
            if alert.get("ok"):
                print(f"[laser_sweep]   通知：已发出（{channel}）")
            else:
                print(f"[laser_sweep]   通知：未发出（{channel}；{alert.get('error') or '原因见上方日志'}）")
        else:
            print("[laser_sweep]   通知：未经过守卫（没有告警内容可发）")
        print(f"[laser_sweep]   结果文件：{csv_path}")
        print("[laser_sweep]   已按空值占位写入当前组，行号与参数序列保持一致")

        # ③ 停整个任务
        print("[laser_sweep] 扫描已终止")
        try:
            tasker = getattr(context, "tasker", None)
            stop_call = getattr(tasker, "post_stop", None) if tasker is not None else None
            if callable(stop_call):
                job = stop_call()
                # MaaFramework 的 post_* 返回一个 Job；不 wait 的话
                # 后续日志可能和框架的收尾交错输出，看不出到底停没停。
                waiter = getattr(job, "wait", None)
                if callable(waiter):
                    waiter()
                print("[laser_sweep]   已请求框架停止任务")
            else:
                # 拿不到 post_stop（版本差异 / 桩环境）不算失败：
                # 本节点在流水线里没有 next，执行完流程本来就会停。
                print("[laser_sweep]   当前环境没有 tasker.post_stop()，依赖节点无 next 自然结束")
        except Exception as exc:  # noqa: BLE001 - 停止动作失败也要把摘要打完
            print(f"[laser_sweep]   ⚠️ 请求停止任务失败（{type(exc).__name__}: {exc}），流程将自然结束")
        return True


# --------------------------------------------------------------------------- #
# ④d 独立的「邮件通知」任务（2026-10-27 新增）
#
# 为什么要拆成独立任务，而不是继续塞在扫描任务里：
#   ① 扫描任务的 Guard 节点参数越多，被 pipeline_override「整体替换」漏字段
#      的风险越大（这个坑本项目踩过）；拆出去之后 Guard 只剩判定相关的几项；
#   ② 主人想「扫完自动发一封信报平安」，这和「读数异常告警」是两件事，
#      混在一起会导致「配置读死了只做告警」；
#   ③ 邮件参数（账号/授权码/收件人）集中在一个地方填，改一处就全生效 ——
#      扫描任务的异常告警读的也是同一份配置（见 _resolve_mail_param）。
# --------------------------------------------------------------------------- #
def _compose_summary(
    *,
    state: dict[str, Any],
    node: str = "邮件通知",
) -> dict[str, Any]:
    """组装「任务完成」通知正文：这次扫了什么、结果怎样、文件在哪。

    和告警信的区别：告警信是「出事了，快来看」；这封是「跑完了，看一眼就行」。
    所以正文以**结果摘要**为主：扫了多少组、成功几组、有没有空值行、耗时多久。
    它读的是 _state 里那次扫描留下的记录 —— 扫描任务的收尾节点会把这些填好。
    """
    values = list(state.get("values") or [])
    total = len(values)
    csv_path = state.get("csv_path")
    out_root = state.get("out_root")
    elapsed = _elapsed_seconds()
    alert = state.get("alert") or {}

    # 尝试从 CSV 里统计真实写入的行数：比只看 _state 可靠，
    # 因为它反映的是「真的落盘了什么」，而不是「程序觉得该写什么」。
    rows: list[list[str]] = []
    try:
        if csv_path is not None and Path(csv_path).is_file():
            with Path(csv_path).open("r", newline="", encoding="utf-8-sig") as f:
                rows = [row for row in csv.reader(f) if row]
    except OSError as exc:
        print(f"[laser_sweep]   ⚠️ 读结果文件失败，跳过行数统计：{exc}")

    data_rows = rows[1:] if rows else []
    recorded = len(data_rows)
    empty_rows = [r for r in data_rows if len(r) > 2 and not str(r[2]).strip()]
    filled = recorded - len(empty_rows)

    if total and not recorded:
        progress = f"计划 {total} 组，结果文件里还没有数据行"
    elif total:
        progress = f"计划 {total} 组，已记录 {recorded} 行（有效 {filled} 行"
        progress += f"，空值 {len(empty_rows)} 行）" if empty_rows else "）"
    else:
        progress = f"结果文件里记录了 {recorded} 行"

    subject = f"任务完成：{progress}"
    if alert.get("ok") is False and alert:
        subject = "任务结束（含异常）：" + progress

    body_lines = [
        "激光参数扫描 —— 任务结束通知",
        "=" * 46,
        "",
        "【结果】",
        f"  {progress}",
    ]
    if values:
        body_lines.append(f"  参数区间：{values[0]} → {values[-1]}")
    if empty_rows:
        body_lines.append(
            "  空值行位置："
            + "、".join(str(r[0]) for r in empty_rows[:10] if r)
            + ("…" if len(empty_rows) > 10 else "")
        )
        body_lines.append("  （空值 = 该组读数失败或异常被拦下，行号仍与设定参数一一对应）")

    # ── 跳变留痕（2026-10-27 审查要求）──────────────────────────────────
    # 为什么必须写进摘要：这类行**是有效数据**，但可信度低于普通读数 ——
    #   连续多次重测都偏离上一个可信值，只是"彼此一致"才被接受。
    # 只在日志里提一句的话，收邮件的人只会看到"全部成功"，
    # 根本不知道有几个点是这么来的。审查指出"摘要承诺了却没实现"，这里补上。
    jumps = state.get("jumps")
    if isinstance(jumps, list) and jumps:
        body_lines.append("")
        body_lines.append("【需复核的跳变】")
        body_lines.append(
            f"  共 {len(jumps)} 个点：连续重测都偏离上一个可信值，"
            "因彼此一致而被接受（不是常规读数）"
        )
        for item in jumps[:10]:
            body_lines.append(
                "    第 %s 组 设定 %s：%s → %s"
                % (
                    item.get("index"),
                    item.get("set"),
                    _fmt(float(item.get("from") or 0)),
                    _fmt(float(item.get("value") or 0)),
                )
            )
        if len(jumps) > 10:
            body_lines.append(f"    …另有 {len(jumps) - 10} 个，见 CSV 的「备注」列")
        body_lines.append("  建议：对照设备实际状态复核这几个点，或调大偏差阈值后重扫")

    body_lines += [
        "",
        "【耗时】",
        f"  {_fmt_elapsed(elapsed)}",
        f"  开始时间：{state.get('started_at') or '(未知)'}",
    ]

    body_lines += ["", "【文件】"]
    body_lines.append(f"  结果文件：{csv_path if csv_path else '(本次没有生成)'}")
    if out_root:
        body_lines.append(f"  输出目录：{out_root}")

    # 有异常告警过就顺带说一句，免得两封信对不上
    if alert:
        body_lines += ["", "【读数异常】"]
        if alert.get("ok"):
            body_lines.append("  本次触发过读数异常告警，告警邮件已单独发出。")
        else:
            body_lines.append(
                f"  本次触发过读数异常，但告警邮件未发出（{alert.get('error') or '原因见日志'}）。"
            )
        abnormal = alert.get("abnormal") or []
        if abnormal:
            body_lines.append(
                "  异常读数：" + "、".join(_fmt(float(v)) for v in abnormal[:10])
            )

    body_lines += [
        "",
        "—— 本邮件由激光参数扫描程序的「邮件通知」任务自动发送，请勿直接回复。",
    ]

    return {
        "subject": subject,
        "body": "\n".join(body_lines),
        "expected": "",
        "abnormal": list(alert.get("abnormal") or []),
        "summary": progress,
    }


@AgentServer.custom_action("laser_sweep_send_summary")
class LaserSweepSendSummary(CustomAction):
    """「邮件通知」任务的主体：给这次扫描的结果发一封汇总信。

    它**不依赖扫描是否刚刚结束**：只要 _state 里还有上一次扫描留下的记录
    （同一个 agent 进程内），或者结果文件还在，就能发。
    所以既可以挂在扫描收尾之后自动跑，也可以单独点一下手动补发。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        param = _param(argv.custom_action_param)
        print("[laser_sweep] ── 邮件通知任务 ──")

        # 「发送完成通知」开关：正常由「邮件设置」option 传进来；
        # 若节点参数里没有（例如以后又拆掉 option），就从「邮件通知」任务保存的
        # 设置里读，保证这个开关永远只有一个真源。
        enabled_raw = param.get("enabled")
        if enabled_raw is None:
            enabled_raw = _load_mail_settings_from_email_task().get("enabled")
        if not _flag(enabled_raw, True):
            print("[laser_sweep] 「发送完成通知」是关的，跳过（任务正常结束）")
            return True

        mail = _resolve_mail_param(param)
        print(
            f"[laser_sweep]   发信配置：{mail['smtp_host'] or '(未填)'}:{mail['smtp_port']}"
            f"，账号 {mail['smtp_user'] or '(未填)'}"
            f"，收件人 {mail['mail_to'] or '(未填)'}"
            f"，授权码{'已填' if mail['smtp_password'] else '未填'}"
        )

        info = _compose_summary(state=_state, node=str(getattr(argv, "node_name", "") or "邮件通知"))
        print(f"[laser_sweep]   主题：{mail['mail_subject_prefix']}{info['subject']}")
        print(f"[laser_sweep]   {info['summary']}")

        # count_key="mail_done_count"：完成通知有自己的计数键，
        # 不吃「告警限额」的名额 —— 先点过「邮件测试」也照样发得出去。
        result = _send_alert(mail, info, count_key="mail_done_count")
        _state["mail_result"] = result
        if result.get("ok"):
            print("[laser_sweep] ✅ 完成通知已发出")
        else:
            # 发信失败不该让任务显示失败 —— 这只是一封通知。
            # 但要把原因说清楚，否则用户只会觉得「按钮没反应」。
            print(f"[laser_sweep] ⚠️ 完成通知没发出去：{result.get('error')}")
            print("[laser_sweep]   请到「邮件设置」里检查账号/授权码/收件人，或先用「邮件测试」验证")
        return True


@AgentServer.custom_action("laser_sweep_mail_test")
class LaserSweepMailTest(CustomAction):
    """「邮件测试」任务：立刻发一封测试信，用来验证配置对不对。

    为什么要单独有它：邮件配错时，只有等到「连续 5 次读数异常」那一刻才会暴露 ——
    而那正是最不该发现「原来邮件一直发不出去」的时候。
    所以给一个随时可点、立刻给结论的自检入口。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        param = _param(argv.custom_action_param)
        print("[laser_sweep] ── 邮件测试 ──")

        mail = _resolve_mail_param(param)
        print(
            f"[laser_sweep]   服务器 {mail['smtp_host'] or '(未填)'}:{mail['smtp_port']}"
            f"（{'SSL' if mail['smtp_ssl'] else '非 SSL'}）"
        )
        print(f"[laser_sweep]   发件人 {mail['smtp_user'] or '(未填)'} → 收件人 {mail['mail_to'] or '(未填)'}")

        machine = "未知"
        try:
            machine = socket.gethostname()
        except OSError:
            pass
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        info = {
            "subject": "测试邮件：告警通道自检",
            "body": "\n".join(
                [
                    "这是一封测试邮件，用于确认「邮件通知」的配置可用。",
                    "",
                    "收到它说明：",
                    "  · SMTP 服务器、端口、账号、授权码都填对了；",
                    "  · 从这台机器能连出去；",
                    "  · 收件地址能正常投递。",
                    "",
                    f"机器名：{machine}",
                    f"发送时间：{now}",
                    "",
                    "之后如果扫描中连续多次读数异常，会自动收到同格式的告警邮件。",
                    "",
                    "—— 本邮件由激光参数扫描程序自动发送，请勿直接回复。",
                ]
            ),
        }

        # count_key="" —— 测试信不计数、也不受任何上限限制。
        # 用户就是来验证通道的，被限额挡住就失去意义了。
        result = _send_alert(mail, info, count_key="")
        _state["mail_result"] = result
        if result.get("ok"):
            print(f"[laser_sweep] ✅ 测试邮件已发出 → {mail['mail_to']}")
            print("[laser_sweep]   没收到的话先翻一下垃圾箱/被拦截邮件")
        else:
            print(f"[laser_sweep] ❌ 测试失败：{result.get('error')}")
            print("[laser_sweep]   常见原因：授权码填成了登录密码、公司网络封了 465 端口")
        return True


# --------------------------------------------------------------------------- #
# ⑤ 写 CSV# --------------------------------------------------------------------------- #
@AgentServer.custom_action("laser_sweep_record")
class LaserSweepRecord(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        return _append_row(_state.get("measured"))


@AgentServer.custom_action("laser_sweep_record_failed")
class LaserSweepRecordFailed(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        print("[laser_sweep]   本轮读数失败，写入空值占位以保证行数对齐")
        return _append_row(None)


# --------------------------------------------------------------------------- #
# ⑥ 推进 / 收尾 / 中止
# --------------------------------------------------------------------------- #
@AgentServer.custom_action("laser_sweep_advance")
class LaserSweepAdvance(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        param = _param(argv.custom_action_param)
        _state["index"] += 1
        total = len(_state["values"])

        if _state["index"] >= total:
            done_node = str(param.get("done_node") or "LaserSweepDone")
            # 把本节点的 next 改写成收尾节点，循环就此结束
            context.override_next(argv.node_name, [done_node])
            print(f"[laser_sweep] 已完成全部 {total} 组扫描")

        return True


@AgentServer.custom_action("laser_sweep_finish")
class LaserSweepFinish(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        param = _param(argv.custom_action_param)
        csv_path = _state.get("csv_path")
        print(f"[laser_sweep] 扫描结束，结果文件：{csv_path}")

        # 跑完自动打开 output 总目录（UI 上可以关掉）。
        # 折叠分组的设置由 begin 从任务配置读到 _state；优先用它，避免折叠时
        # 子 option 不参与 pipeline 合并而退回节点默认 true。
        if _state.get("scan_settings_from_task"):
            open_it = bool(_state.get("open_when_done", True))
        else:
            want = param.get("open_when_done")
            open_it = _flag(want, True) if want is not None else bool(_state.get("open_when_done", True))
        if open_it:
            root = _state.get("out_root")
            if root is None and csv_path is not None:
                # 兜底：没记下总目录就现场推一个（csv 在 output/<本次>/ 里）
                root = Path(csv_path).parent.parent
                print("[laser_sweep]   没记下 output 总目录，按结果路径上推两级")
            if root is not None:
                _open_folder(Path(root))
        else:
            print("[laser_sweep] 已按设置跳过「自动打开文件夹」")
        return True


@AgentServer.custom_action("laser_sweep_open_output")
class LaserSweepOpenOutput(CustomAction):
    """随时打开 output 总目录 —— 对应界面上那个「打开输出文件夹」任务。

    它不依赖扫描状态：没跑过扫描也能用（文件夹不存在就现建一个空的），
    所以既能在扫描后看结果，也能在扫描前先确认路径对不对。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        param = _merge_scan_settings_from_task(_param(argv.custom_action_param))

        # 依次尝试三个来源：本次会话 → 落盘的记录 → 默认 output/
        root = _state.get("out_root")
        if root is not None:
            print("[laser_sweep] 用本次扫描记下的输出目录")
        else:
            root = _recall_output_root()
            if root is not None:
                print("[laser_sweep] 用上次扫描记录下来的输出目录")
            else:
                root = _resolve_output_paths(param)[1]
                print("[laser_sweep] 还没跑过扫描，打开默认输出目录")

        print(f"[laser_sweep] 打开输出文件夹：{root}")
        if not _open_folder(Path(root)):
            return False
        return True


# --------------------------------------------------------------------------- #
# ④e 独立的「绘制曲线图」任务（2026-10-28 新增）
#
# 为什么是独立任务：
#   主人要求「该功能需要独立于主要任务」。绘图是**事后消费**结果的动作，
#   不该混进扫描的流水线 —— 扫描要一两个小时，绘图只在一两秒内完成；
#   把绘图挂到扫描收尾会让「主任务失败 = 图也出不来」，而实际上只要 CSV 还在，
#   随时都可以重新画。
#
# 默认画哪一份 CSV（主人要求「默认选择最近导出的单个 CSV」）：
#   ① 同一个 agent 进程里刚跑完扫描 → _state["csv_path"]；
#   ② 否则读 laser_sweep_output.json 里的 last_csv（扫描 begin 时写的）；
#   ③ 再否则在 output 总目录里找修改时间最新的一个 .csv。
#   用户也可以在「绘图设置」里填一个具体 CSV（或目录，取目录里最新的）。
# --------------------------------------------------------------------------- #
def _resolve_plot_csv(param: dict[str, Any]) -> Optional[Path]:
    """决定「绘制曲线图」任务要画哪一份 CSV。

    返回 None 表示找不到；调用方打印的日志已经说明了该去哪里补。
    """
    raw = str(param.get("source_csv") or "").strip().strip('"').strip("'")
    if raw:
        target = _resolve(raw)
        if target.is_dir():
            from plot_curve import find_latest_csv

            latest = find_latest_csv(target)
            if latest is None:
                print(f"[laser_sweep] 指定的目录里没找到任何 CSV：{target}")
                return None
            print(f"[laser_sweep] 指定的是目录，自动选里面最新的 CSV：{latest}")
            return latest
        if not target.is_file():
            print(f"[laser_sweep] 指定的 CSV 不存在：{target}")
            return None
        print(f"[laser_sweep] 使用界面指定的 CSV：{target}")
        return target

    # ① 本进程刚扫完（最准确：就是这次 begin 算出来的那个路径）
    session_csv = _state.get("csv_path")
    if isinstance(session_csv, Path) and session_csv.is_file():
        print(f"[laser_sweep] 使用本次扫描的 CSV：{session_csv}")
        return session_csv

    # ② 落盘记录的上一次扫描
    last_csv = _recall_last_csv()
    if last_csv is not None and last_csv.is_file():
        print(f"[laser_sweep] 使用上次扫描记录的 CSV：{last_csv}")
        return last_csv

    # ③ 在可能的 output 总目录里找最新的 CSV
    from plot_curve import find_latest_csv

    roots: list[Path] = []
    remembered = _recall_output_root()
    if remembered is not None:
        roots.append(remembered)
    try:
        settings = _load_scan_settings_from_task()
    except Exception:  # noqa: BLE001 - 纯兜底路径，读不到就用默认值
        settings = {}
    root_raw = str((settings or {}).get("output_root") or "").strip()
    if root_raw:
        roots.append(_resolve(root_raw))
    roots.append(_resolve(OUTPUT_ROOT_NAME))

    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        latest = find_latest_csv(root)
        if latest is not None:
            print(f"[laser_sweep] 自动选择 output 目录里最新的 CSV：{latest}")
            return latest

    print("[laser_sweep] 找不到可绘制的 CSV：")
    print(f"[laser_sweep]   看过的目录：{'、'.join(str(p) for p in roots)}")
    print("[laser_sweep]   请先在「激光参数扫描」任务里跑一次，或在「绘图设置」里填写 CSV 路径")
    return None


@AgentServer.custom_action("laser_sweep_plot_curve")
class LaserSweepPlotCurve(CustomAction):
    """「绘制曲线图」任务：CSV → 温度-输出功率曲线 PNG（存在 CSV 同目录）。

    它完全不依赖扫描流程是否正在跑：只要磁盘上有一份扫描导出的 CSV 就能用。
    真正的绘图逻辑在 agent/plot_curve.py（纯 stdlib + Windows GDI+），
    这里只负责「选文件、调绘图、打日志、按需打开图片」。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        param = _param(argv.custom_action_param)
        print("[laser_sweep] ── 绘制曲线图任务 ──")

        csv_path = _resolve_plot_csv(param)
        if csv_path is None:
            return False

        # 延迟导入：绘图模块缺失时只让这一个任务失败，不拖垮整个 agent
        # （热修复包漏发 agent/plot_curve.py 时的兜底提示就靠这里）。
        try:
            import plot_curve
        except Exception as exc:  # noqa: BLE001 - 任何导入错误都按任务失败处理
            print(f"[laser_sweep] ✗ 绘图模块加载失败：{type(exc).__name__}: {exc}")
            print("[laser_sweep]   热修复包需要包含 agent/plot_curve.py（和 laser_sweep.py 同目录）")
            return False

        # 图片文件名：留空 = <CSV 文件名>_曲线.png；填了就照填的来（仍强制同目录）
        image_name = str(param.get("image_name") or "").strip().strip('"').strip("'")
        image_path: Optional[Path] = None
        if image_name:
            safe_name = _sanitize_name(_expand_time_format(image_name), "")
            if not safe_name:
                safe_name = f"{csv_path.stem}_曲线"
            if not safe_name.lower().endswith(".png"):
                safe_name += ".png"
            image_path = csv_path.parent / safe_name

        try:
            stat = csv_path.stat()
            print(
                f"[laser_sweep] CSV：{csv_path}"
                f"（{stat.st_size} 字节，改于 {datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')}）"
            )
            result = plot_curve.plot_csv(csv_path, image_path)
        except plot_curve.PlotError as exc:
            print(f"[laser_sweep] ✗ 绘制失败：{exc}")
            return False
        except Exception as exc:  # noqa: BLE001 - 绘图任务不能把整个进程带崩
            print(f"[laser_sweep] ✗ 绘制时出现意外错误：{type(exc).__name__}: {exc}")
            return False

        print(
            f"[laser_sweep] 数据：X = 「{result['x_header']}」，Y = 「{result['y_header']}」，"
            f"有效点 {result['points']} 个"
            + (f"，跳过空值 {result['skipped']} 行" if result["skipped"] else "")
        )
        print(
            f"[laser_sweep] 范围：温度 {_fmt(result['x_min'])} ~ {_fmt(result['x_max'])}，"
            f"功率 {_fmt(result['y_min'])} ~ {_fmt(result['y_max'])}"
        )
        print(
            f"[laser_sweep] ✅ 曲线图已保存：{result['image_path']}"
            f"（{max(1, result['size'] // 1024)} KB）"
        )

        if _flag(param.get("open_when_done"), True):
            if plot_curve.open_image(result["image_path"]):
                print("[laser_sweep] 已用默认看图软件打开图片")
            else:
                print("[laser_sweep] 提示：没能自动打开图片，请到上面的路径手动查看")
        return True


@AgentServer.custom_action("laser_sweep_abort")
class LaserSweepAbort(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        total = len(_state["values"])
        print("[laser_sweep] ✗ 界面定位失败，扫描提前中止")
        print(f"[laser_sweep]   进度：第 {min(_state['index'] + 1, total)}/{total} 组")
        print(f"[laser_sweep]   已完成的数据已写入：{_state.get('csv_path')}")
        print("[laser_sweep]   排查建议：先在 VS Code 里单独跑 LaserSweepFindTemperature，")
        print("[laser_sweep]   确认它能不能框到 Temperature 这个词。")
        return True
