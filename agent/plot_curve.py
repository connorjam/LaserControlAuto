"""把扫描导出的 CSV 画成曲线图 —— 「绘制曲线图」独立任务的后端。

任务语义（2026-10-28 新增）
--------------------------
· 输入：一次扫描导出的 CSV（默认自动挑**最近导出的那一个**）
· 横轴：温度（CSV 的「设定参数」列）
· 纵轴：B 软件读数 / 输出功率（CSV 的「B软件测量值」列）
· 输出：PNG 图片，**保存在 CSV 同级目录**，文件名默认 `<CSV 文件名>_曲线.png`

为什么不用 matplotlib
--------------------
发布包用的是 Python embeddable（见 tools/prepare_embedded_python.py），
随包只带 maafw / numpy 两个第三方依赖。matplotlib 及其依赖（pillow、fonttools、
kiwisolver、contourpy……）会把发布包撑大几十 MB；更关键的是，热修复包只覆盖
agent/pipeline/interface 几个文件，用户更新后本机仍然没有 matplotlib ——
一执行这个任务就会 ImportError。

所以这里只依赖 **Windows 自带的 GDI+**（gdiplus.dll，从 XP 起就有）：
线、圆、文字、PNG 编码全部由系统组件完成，不新增任何第三方依赖，
热修复包依旧只有几十 KB。项目本身就是 Windows-only（Win32 控制器 + 桌面软件），
这个前提成立。

单独成文件（而不是塞进 laser_sweep.py）的理由
--------------------------------------------
1. 绘图和扫描是两件事：扫描逻辑已经 5000 多行，再混进 GDI+ 的 ctypes 声明
   只会让两边都难维护；
2. 这个模块是**纯 stdlib**，可以脱离 MaaFramework 单独跑自检（tools/check_plot.py）；
3. 「绘制曲线图」任务与主任务完全独立 —— 即使这个模块出问题，也只影响绘图任务，
   laser_sweep.py 是在动作里延迟导入它的，不会拖垮扫描。
"""

from __future__ import annotations

import csv
import ctypes
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from ctypes import wintypes

# --------------------------------------------------------------------------- #
# GDI+ 常量与 ctypes 声明
#
# 全部按 Gdiplusflat.h 的真实签名声明。GDI+ 的函数返回 Status（0 = Ok），
# 这里统一用 _check() 检查；某个调用失败就抛 PlotError，由任务层打印成一行
# 可读的原因，而不是留下一张空白图让人猜。
# --------------------------------------------------------------------------- #
PIXEL_FORMAT_32BPP_ARGB = 0x0026200A
SMOOTHING_MODE_HIGH_QUALITY = 2
TEXT_RENDERING_HINT_ANTIALIAS = 4
PIXEL_OFFSET_MODE_HIGH_QUALITY = 2
UNIT_PIXEL = 2
FONT_STYLE_REGULAR = 0
FONT_STYLE_BOLD = 1
STRING_ALIGN_NEAR = 0
STRING_ALIGN_CENTER = 1
STRING_ALIGN_FAR = 2
MATRIX_ORDER_APPEND = 1


class PlotError(Exception):
    """绘图任务的「可预期失败」：参数不对、CSV 没数据、GDI+ 不可用等。

    与真正的程序错误分开：任务层只需要把它打印成日志并返回 False，
    不需要抛 traceback 吓人（使用者不是开发者）。
    """


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", ctypes.c_uint32),
        ("DebugEventCallback", ctypes.c_void_p),
        ("SuppressBackgroundThread", wintypes.BOOL),
        ("SuppressExternalCodecs", wintypes.BOOL),
    ]


class _RectF(ctypes.Structure):
    _fields_ = [
        ("X", ctypes.c_float),
        ("Y", ctypes.c_float),
        ("Width", ctypes.c_float),
        ("Height", ctypes.c_float),
    ]


class _ImageCodecInfo(ctypes.Structure):
    _fields_ = [
        ("Clsid", _GUID),
        ("FormatID", _GUID),
        ("CodecName", ctypes.c_wchar_p),
        ("DllName", ctypes.c_wchar_p),
        ("FormatDescription", ctypes.c_wchar_p),
        ("FilenameExtension", ctypes.c_wchar_p),
        ("MimeType", ctypes.c_wchar_p),
        ("Flags", ctypes.c_uint32),
        ("Version", ctypes.c_uint32),
        ("SigCount", ctypes.c_uint32),
        ("SigSize", ctypes.c_uint32),
        ("SigPattern", ctypes.POINTER(ctypes.c_ubyte)),
        ("SigMask", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _load_gdiplus() -> Any:
    """加载 gdiplus.dll；非 Windows 返回 None（任务会给出可读的失败原因）。"""
    if sys.platform != "win32":
        return None
    try:
        return ctypes.WinDLL("gdiplus")
    except OSError:
        return None


_gdiplus = _load_gdiplus()


def _declare(name: str, argtypes: list[Any], restype: Any = ctypes.c_int) -> Any:
    """声明一个 GDI+ 函数；gdiplus 不存在时返回 None，调用方不会走到这里。"""
    if _gdiplus is None:
        return None
    fn = getattr(_gdiplus, name)
    fn.argtypes = argtypes
    fn.restype = restype
    return fn


# 生命周期
_GdiplusStartup = _declare(
    "GdiplusStartup",
    [ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(_GdiplusStartupInput), ctypes.c_void_p],
)
_GdiplusShutdown = _declare("GdiplusShutdown", [ctypes.c_ulong], None)

# 位图 / 画布
_GdipCreateBitmapFromScan0 = _declare(
    "GdipCreateBitmapFromScan0",
    [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
)
_GdipGetImageGraphicsContext = _declare(
    "GdipGetImageGraphicsContext", [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
)
_GdipGraphicsClear = _declare("GdipGraphicsClear", [ctypes.c_void_p, ctypes.c_uint32])
_GdipSetSmoothingMode = _declare("GdipSetSmoothingMode", [ctypes.c_void_p, ctypes.c_int])
_GdipSetTextRenderingHint = _declare("GdipSetTextRenderingHint", [ctypes.c_void_p, ctypes.c_int])
_GdipSetPixelOffsetMode = _declare("GdipSetPixelOffsetMode", [ctypes.c_void_p, ctypes.c_int])

# 画笔 / 画刷 / 字体
_GdipCreatePen1 = _declare(
    "GdipCreatePen1",
    [ctypes.c_uint32, ctypes.c_float, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)],
)
_GdipCreateSolidFill = _declare("GdipCreateSolidFill", [ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)])
_GdipCreateFontFamilyFromName = _declare(
    "GdipCreateFontFamilyFromName",
    [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
)
_GdipCreateFont = _declare(
    "GdipCreateFont",
    [ctypes.c_void_p, ctypes.c_float, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)],
)
_GdipCreateStringFormat = _declare(
    "GdipCreateStringFormat", [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
)
_GdipSetStringFormatAlign = _declare("GdipSetStringFormatAlign", [ctypes.c_void_p, ctypes.c_int])
_GdipSetStringFormatLineAlign = _declare("GdipSetStringFormatLineAlign", [ctypes.c_void_p, ctypes.c_int])

# 绘制
_GdipDrawLine = _declare(
    "GdipDrawLine",
    [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float],
)
_GdipDrawEllipse = _declare(
    "GdipDrawEllipse",
    [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float],
)
_GdipFillEllipse = _declare(
    "GdipFillEllipse",
    [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float],
)
_GdipFillRectangle = _declare(
    "GdipFillRectangle",
    [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float],
)
_GdipDrawString = _declare(
    "GdipDrawString",
    [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.POINTER(_RectF),
        ctypes.c_void_p,
        ctypes.c_void_p,
    ],
)
_GdipMeasureString = _declare(
    "GdipMeasureString",
    [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.POINTER(_RectF),
        ctypes.c_void_p,
        ctypes.POINTER(_RectF),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
    ],
)

# 变换（纵轴标题要旋转 90°）
_GdipSaveGraphics = _declare("GdipSaveGraphics", [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)])
_GdipRestoreGraphics = _declare("GdipRestoreGraphics", [ctypes.c_void_p, ctypes.c_uint])
_GdipRotateWorldTransform = _declare(
    "GdipRotateWorldTransform", [ctypes.c_void_p, ctypes.c_float, ctypes.c_int]
)
_GdipTranslateWorldTransform = _declare(
    "GdipTranslateWorldTransform",
    [ctypes.c_void_p, ctypes.c_float, ctypes.c_float, ctypes.c_int],
)

# PNG 编码与资源释放
_GdipGetImageEncodersSize = _declare(
    "GdipGetImageEncodersSize", [ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
)
_GdipGetImageEncoders = _declare(
    "GdipGetImageEncoders", [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
)
_GdipSaveImageToFile = _declare(
    "GdipSaveImageToFile",
    [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.POINTER(_GUID), ctypes.c_void_p],
)
_GdipDisposeImage = _declare("GdipDisposeImage", [ctypes.c_void_p])
_GdipDeleteGraphics = _declare("GdipDeleteGraphics", [ctypes.c_void_p])
_GdipDeletePen = _declare("GdipDeletePen", [ctypes.c_void_p])
_GdipDeleteBrush = _declare("GdipDeleteBrush", [ctypes.c_void_p])
_GdipDeleteFont = _declare("GdipDeleteFont", [ctypes.c_void_p])
_GdipDeleteFontFamily = _declare("GdipDeleteFontFamily", [ctypes.c_void_p])
_GdipDeleteStringFormat = _declare("GdipDeleteStringFormat", [ctypes.c_void_p])


def _check(status: int, what: str) -> None:
    """GDI+ 的每个调用都返回 Status；非 0 就是失败。"""
    if status != 0:
        raise PlotError(f"GDI+ 绘图失败：{what}（Status={status}）")


def _argb(r: int, g: int, b: int, a: int = 255) -> int:
    """打包成 GDI+ 需要的 0xAARRGGBB（注意不是 Win32 的 COLORREF / ABGR）。"""
    return ((a & 0xFF) << 24) | ((r & 0xFF) << 16) | ((g & 0xFF) << 8) | (b & 0xFF)


# --------------------------------------------------------------------------- #
# CSV 解析
# --------------------------------------------------------------------------- #
@dataclass
class CurveData:
    """一张曲线图的全部数据与来源信息。"""

    csv_path: Path
    x_values: list[float]
    y_values: list[float]
    x_header: str
    y_header: str
    skipped: int  # 空值 / 无法解析而跳过的数据行数


_X_KEYWORDS = [
    ("设定参数",),
    ("温度",),
    ("设定值",),
    ("设定",),
    ("temperature",),
    ("temp",),
    ("setpoint",),
    ("set",),
]
_Y_KEYWORDS = [
    ("b软件测量值",),
    ("b软件",),
    ("测量值",),
    ("测量",),
    ("功率",),
    ("读数",),
    ("outputpower",),
    ("power",),
    ("reading",),
    ("measured",),
]

_ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "cp936", "latin-1")
_NUMBER_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _read_text(path: Path) -> str:
    """按常见编码依次尝试读取 CSV。

    Windows 上 Excel/仪器导出的 CSV 可能是 GBK；本项目自己写的是 utf-8-sig。
    全都失败才报错，而不是一上来就 UnicodeDecodeError。
    """
    errors: list[str] = []
    raw = path.read_bytes()
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError) as exc:
            errors.append(f"{encoding}: {exc}")
    raise PlotError(f"CSV 编码无法识别（尝试过 {', '.join(_ENCODINGS)}）：{path}")


def _to_float(text: Any) -> Optional[float]:
    """从单元格里取出第一个数字。

    容忍 `-79.72dBm`、`1061 nm`、`20.5 °C` 这类带单位的写法，
    也容忍 `1,234.5` 这种千分位。取不到返回 None（空值行会被跳过）。
    """
    if text is None:
        return None
    value = str(text).strip()
    if not value or value in ("-", "—", "--", "N/A", "n/a", "NA", "null", "None"):
        return None
    # 千分位：只把「数字之间、且后面还是 3 位数字」的逗号去掉
    value = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", value)
    match = _NUMBER_RE.search(value)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _pick_column(header: list[str], keyword_groups: list[tuple[str, ...]]) -> Optional[int]:
    """按关键词优先级找列；找不到返回 None（交给位置兜底）。

    先做完全匹配，再做包含匹配 —— 例如「B软件测量值」精确命中，
    不会因为另一列叫「B软件测量值备注」而选错。
    """
    normalized = [str(cell).strip().lower().replace(" ", "") for cell in header]
    for keywords in keyword_groups:
        for index, cell in enumerate(normalized):
            if all(cell == keyword for keyword in keywords):
                return index
    for keywords in keyword_groups:
        for index, cell in enumerate(normalized):
            if all(keyword in cell for keyword in keywords):
                return index
    return None


def _first_row_looks_numeric(row: list[str]) -> bool:
    """第一行是不是数据行（无表头 CSV）。"""
    numeric = sum(1 for cell in row if _to_float(cell) is not None)
    return numeric >= max(2, len(row) // 2) and _to_float(row[0] if row else "") is not None


def read_curve_csv(csv_path: Path) -> CurveData:
    """读取 CSV，抽出温度（X）与 B 软件读数（Y）两列。

    兼容：
      · 本项目默认表头：`序号,设定参数,B软件测量值,时间,备注`
      · 老版本 4 列 CSV：`序号,设定参数,B软件测量值,时间`
      · 自定义表头：含「温度」「功率」「读数」「测量」等关键词就能认出来
      · 无表头 CSV：按第 2、3 列（序号、X、Y）兜底
    """
    csv_path = Path(csv_path)
    if not csv_path.is_file():
        raise PlotError(f"CSV 文件不存在：{csv_path}")

    text = _read_text(csv_path)
    sample = "\n".join(text.splitlines()[:20])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel

    rows = [
        [str(cell).strip() for cell in row]
        for row in csv.reader(text.splitlines(), dialect)
        if any(str(cell).strip() for cell in row)
    ]
    if not rows:
        raise PlotError(f"CSV 是空的：{csv_path}")

    first = rows[0]
    has_header = not _first_row_looks_numeric(first)
    if has_header:
        header = first
        data_rows = rows[1:]
        x_index = _pick_column(header, _X_KEYWORDS)
        y_index = _pick_column(header, _Y_KEYWORDS)
    else:
        header = []
        data_rows = rows
        x_index = y_index = None

    # 位置兜底：有表头但没认出来 → 用「序号之后的前两列」；
    # 无表头时同理（第 0 列通常是序号）。
    if x_index is None:
        x_index = 1 if len(first) >= 3 else 0
    if y_index is None:
        y_index = 2 if len(first) >= 3 else 1
    if x_index == y_index:
        y_index = x_index + 1

    x_header = header[x_index] if has_header and x_index < len(header) else f"第{x_index + 1}列"
    y_header = header[y_index] if has_header and y_index < len(header) else f"第{y_index + 1}列"

    x_values: list[float] = []
    y_values: list[float] = []
    skipped = 0
    for row in data_rows:
        need = max(x_index, y_index)
        if len(row) <= need:
            skipped += 1
            continue
        x = _to_float(row[x_index])
        y = _to_float(row[y_index])
        if x is None or y is None or not math.isfinite(x) or not math.isfinite(y):
            skipped += 1
            continue
        x_values.append(x)
        y_values.append(y)

    if not x_values:
        raise PlotError(
            f"CSV 里没有可绘制的数据行（X 列「{x_header}」/ Y 列「{y_header}」）"
            f"，跳过了 {skipped} 行：{csv_path}"
        )

    # 按温度升序画，这样即使扫描方向是「高温→低温」，曲线也符合直觉。
    pairs = sorted(zip(x_values, y_values), key=lambda item: item[0])
    x_values = [item[0] for item in pairs]
    y_values = [item[1] for item in pairs]
    return CurveData(csv_path, x_values, y_values, x_header, y_header, skipped)


# --------------------------------------------------------------------------- #
# 找「最近导出的单个 CSV」
# --------------------------------------------------------------------------- #
def find_latest_csv(root: Path, max_depth: int = 6, max_dirs: int = 3000) -> Optional[Path]:
    """在 output 目录下找修改时间最新的一个 .csv。

    为什么不用 `rglob`：用户可能把「输出总目录」填成 D:\\ 甚至网络盘，
    rglob 会把整块盘扫一遍。这里用有界 BFS（深度 ≤ 6、目录数 ≤ 3000），
    找不到就正常返回 None，任务再退回更明确的提示。
    """
    root = Path(root)
    if not root.is_dir():
        return None

    best: Optional[Path] = None
    best_key: Optional[tuple[int, str]] = None
    queue: list[tuple[Path, int]] = [(root, 0)]
    visited = 0

    while queue:
        current, depth = queue.pop(0)
        visited += 1
        if visited > max_dirs:
            break
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    name = entry.name
                    # 跳过回收站/隐藏目录这类明显不是结果的目录
                    if name.startswith(".") or name.lower() in ("$recycle.bin", "system volume information"):
                        continue
                    if depth < max_depth:
                        queue.append((Path(entry.path), depth + 1))
                    continue
                if not entry.name.lower().endswith(".csv"):
                    continue
                stat = entry.stat()
                key = (stat.st_mtime_ns, entry.name.lower())
                if best_key is None or key > best_key:
                    best_key = key
                    best = Path(entry.path)
            except OSError:
                continue
    return best


# --------------------------------------------------------------------------- #
# 画图（GDI+）
# --------------------------------------------------------------------------- #
# 画布与边距（像素）
CANVAS_WIDTH = 1600
CANVAS_HEIGHT = 1000
PLOT_LEFT = 170
PLOT_RIGHT = 80
PLOT_TOP = 130
PLOT_BOTTOM = 130

# 配色：白底、蓝线、灰轴 —— 打印和贴报告都清楚
COLOR_BG = (255, 255, 255)
COLOR_TITLE = (17, 24, 39)
COLOR_SUBTITLE = (107, 114, 128)
COLOR_AXIS = (107, 114, 128)
COLOR_GRID = (229, 234, 241)
COLOR_TICK_TEXT = (75, 85, 99)
COLOR_LINE = (31, 111, 235)
COLOR_POINT = (37, 99, 235)
COLOR_POINT_RING = (255, 255, 255)
COLOR_FOOTER = (156, 163, 175)


def _nice_step(span: float, target_ticks: int) -> float:
    """把「数据跨度」取整成人类习惯的刻度间隔（1/2/2.5/5 × 10^n）。"""
    if not math.isfinite(span) or span <= 0:
        return 1.0
    raw = span / max(2, target_ticks)
    magnitude = 10 ** math.floor(math.log10(raw))
    for multiplier in (1.0, 2.0, 2.5, 5.0, 10.0):
        step = multiplier * magnitude
        if step >= raw * 0.999999:
            return step
    return 10.0 * magnitude


def _tick_values(low: float, high: float, step: float) -> list[float]:
    """生成 [low, high] 内的刻度值（含端点附近）。"""
    if step <= 0:
        return [low, high]
    start = math.ceil(low / step - 1e-9) * step
    values: list[float] = []
    value = start
    guard = 0
    while value <= high + step * 1e-6 and guard < 1000:
        values.append(round(value, 12))
        value += step
        guard += 1
    if not values:
        values = [low, high]
    return values


def _decimals_for_step(step: float) -> int:
    """刻度标签要几位小数：0.25 → 2，0.2 → 1，5 → 0。"""
    decimals = 0
    while decimals < 8 and abs(round(step, decimals) - step) > 1e-9:
        decimals += 1
    return decimals


def _fmt_tick(value: float, decimals: int) -> str:
    if abs(value) < 10 ** (-decimals) / 2:
        value = 0.0
    return f"{value:.{decimals}f}"


def _fmt_num(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _axis_range(values: list[float]) -> tuple[float, float, float]:
    """返回 (low, high, step)：两端留 4% 余量，并算出刻度间隔。"""
    low, high = min(values), max(values)
    if high - low < 1e-12:
        pad = max(abs(high) * 0.01, 1.0)
        low -= pad
        high += pad
    else:
        pad = (high - low) * 0.04
        low -= pad
        high += pad
    step = _nice_step(high - low, 8)
    return low, high, step


class _Canvas:
    """一张 GDI+ 位图 + 常用绘图操作的薄封装。"""

    def __init__(self, width: int, height: int) -> None:
        if _gdiplus is None:
            raise PlotError("当前系统没有可用的 GDI+（gdiplus.dll），无法绘图")
        self.width = width
        self.height = height

        self._token = ctypes.c_ulong()
        startup = _GdiplusStartupInput(1, None, False, False)
        _check(_GdiplusStartup(ctypes.byref(self._token), ctypes.byref(startup), None), "GdiplusStartup")

        self._image = ctypes.c_void_p()
        _check(
            _GdipCreateBitmapFromScan0(
                width, height, width * 4, PIXEL_FORMAT_32BPP_ARGB, None, ctypes.byref(self._image)
            ),
            "GdipCreateBitmapFromScan0",
        )
        self._graphics = ctypes.c_void_p()
        _check(_GdipGetImageGraphicsContext(self._image, ctypes.byref(self._graphics)), "GdipGetImageGraphicsContext")
        _check(_GdipGraphicsClear(self._graphics, _argb(*COLOR_BG)), "GdipGraphicsClear")
        _GdipSetSmoothingMode(self._graphics, SMOOTHING_MODE_HIGH_QUALITY)
        _GdipSetTextRenderingHint(self._graphics, TEXT_RENDERING_HINT_ANTIALIAS)
        _GdipSetPixelOffsetMode(self._graphics, PIXEL_OFFSET_MODE_HIGH_QUALITY)

        self._family = self._create_font_family()
        self._fonts: dict[tuple[int, bool], ctypes.c_void_p] = {}
        self._formats: dict[tuple[int, int], ctypes.c_void_p] = {}
        self._pens: dict[tuple[int, float], ctypes.c_void_p] = {}
        self._brushes: dict[int, ctypes.c_void_p] = {}

    @staticmethod
    def _create_font_family() -> ctypes.c_void_p:
        """挑一个能显示中文的系统字体；都没有就用 GDI+ 默认字体。"""
        for name in ("Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "SimSun"):
            family = ctypes.c_void_p()
            if _GdipCreateFontFamilyFromName(name, None, ctypes.byref(family)) == 0:
                return family
        family = ctypes.c_void_p()
        _check(_GdipCreateFontFamilyFromName("Arial", None, ctypes.byref(family)), "GdipCreateFontFamilyFromName")
        return family

    def _font(self, size: int, bold: bool) -> ctypes.c_void_p:
        key = (size, bold)
        if key not in self._fonts:
            font = ctypes.c_void_p()
            _check(
                _GdipCreateFont(
                    self._family,
                    float(size),
                    FONT_STYLE_BOLD if bold else FONT_STYLE_REGULAR,
                    UNIT_PIXEL,
                    ctypes.byref(font),
                ),
                "GdipCreateFont",
            )
            self._fonts[key] = font
        return self._fonts[key]

    def _format(self, align: int, line_align: int) -> ctypes.c_void_p:
        key = (align, line_align)
        if key not in self._formats:
            fmt = ctypes.c_void_p()
            _check(_GdipCreateStringFormat(0, 0, ctypes.byref(fmt)), "GdipCreateStringFormat")
            _GdipSetStringFormatAlign(fmt, align)
            _GdipSetStringFormatLineAlign(fmt, line_align)
            self._formats[key] = fmt
        return self._formats[key]

    def _pen(self, color: tuple[int, int, int], width: float) -> ctypes.c_void_p:
        key = (_argb(*color), float(width))
        if key not in self._pens:
            pen = ctypes.c_void_p()
            _check(_GdipCreatePen1(key[0], key[1], UNIT_PIXEL, ctypes.byref(pen)), "GdipCreatePen1")
            self._pens[key] = pen
        return self._pens[key]

    def _brush(self, color: tuple[int, int, int]) -> ctypes.c_void_p:
        key = _argb(*color)
        if key not in self._brushes:
            brush = ctypes.c_void_p()
            _check(_GdipCreateSolidFill(key, ctypes.byref(brush)), "GdipCreateSolidFill")
            self._brushes[key] = brush
        return self._brushes[key]

    def line(self, x1: float, y1: float, x2: float, y2: float, color: tuple[int, int, int], width: float = 1.0) -> None:
        _check(_GdipDrawLine(self._graphics, self._pen(color, width), x1, y1, x2, y2), "GdipDrawLine")

    def rect_fill(self, x: float, y: float, w: float, h: float, color: tuple[int, int, int]) -> None:
        _check(_GdipFillRectangle(self._graphics, self._brush(color), x, y, w, h), "GdipFillRectangle")

    def ellipse(self, cx: float, cy: float, radius: float, fill: tuple[int, int, int], ring: Optional[tuple[int, int, int]] = None) -> None:
        if ring is not None:
            _check(
                _GdipDrawEllipse(
                    self._graphics,
                    self._pen(ring, 2.0),
                    cx - radius,
                    cy - radius,
                    radius * 2,
                    radius * 2,
                ),
                "GdipDrawEllipse",
            )
        _check(
            _GdipFillEllipse(
                self._graphics, self._brush(fill), cx - radius, cy - radius, radius * 2, radius * 2
            ),
            "GdipFillEllipse",
        )

    def measure_text(self, text: str, size: int, bold: bool = False) -> tuple[float, float]:
        font = self._font(size, bold)
        fmt = self._format(STRING_ALIGN_NEAR, STRING_ALIGN_NEAR)
        layout = _RectF(0, 0, 4000, 400)
        bounding = _RectF()
        fitted = ctypes.c_int()
        lines = ctypes.c_int()
        _check(
            _GdipMeasureString(
                self._graphics,
                text,
                -1,
                font,
                ctypes.byref(layout),
                fmt,
                ctypes.byref(bounding),
                ctypes.byref(fitted),
                ctypes.byref(lines),
            ),
            "GdipMeasureString",
        )
        return float(bounding.Width), float(bounding.Height)

    def text(
        self,
        text: str,
        x: float,
        y: float,
        size: int,
        color: tuple[int, int, int],
        bold: bool = False,
        align: str = "left",
        valign: str = "top",
        box_width: float = 3000,
    ) -> None:
        """在 (x, y) 处写一行字。

        align / valign 指的是「(x, y) 是文本的哪一点」：
        left/center/right 与 top/center/bottom。这样调用方不用自己量文字宽度。
        """
        align_map = {"left": STRING_ALIGN_NEAR, "center": STRING_ALIGN_CENTER, "right": STRING_ALIGN_FAR}
        valign_map = {"top": STRING_ALIGN_NEAR, "center": STRING_ALIGN_CENTER, "bottom": STRING_ALIGN_FAR}
        horizontal = align_map.get(align, STRING_ALIGN_NEAR)
        vertical = valign_map.get(valign, STRING_ALIGN_NEAR)

        if align == "left":
            rect_x = x
        elif align == "center":
            rect_x = x - box_width / 2
        else:
            rect_x = x - box_width

        if valign == "top":
            rect_y, rect_h = y, 400.0
        elif valign == "center":
            rect_y, rect_h = y - 200.0, 400.0
        else:
            rect_y, rect_h = y - 400.0, 400.0

        rect = _RectF(rect_x, rect_y, box_width, rect_h)
        _check(
            _GdipDrawString(
                self._graphics,
                text,
                -1,
                self._font(size, bold),
                ctypes.byref(rect),
                self._format(horizontal, vertical),
                self._brush(color),
            ),
            "GdipDrawString",
        )

    def rotated_text(
        self,
        text: str,
        x: float,
        center_y: float,
        size: int,
        color: tuple[int, int, int],
        bold: bool = False,
    ) -> None:
        """逆时针旋转 90° 的纵轴标题（从下往上读）。

        先旋转再平移：局部 +x 方向变成屏幕上的「向上」，
        所以文字从 (x, center_y) 往上长。宽度用于把整行居中到 center_y。
        """
        width, _height = self.measure_text(text, size, bold)
        state = ctypes.c_uint()
        _check(_GdipSaveGraphics(self._graphics, ctypes.byref(state)), "GdipSaveGraphics")
        try:
            _check(
                _GdipRotateWorldTransform(self._graphics, -90.0, MATRIX_ORDER_APPEND),
                "GdipRotateWorldTransform",
            )
            _check(
                _GdipTranslateWorldTransform(self._graphics, float(x), float(center_y + width / 2), MATRIX_ORDER_APPEND),
                "GdipTranslateWorldTransform",
            )
            self.text(text, 0, 0, size, color, bold=bold)
        finally:
            _GdipRestoreGraphics(self._graphics, state)

    def save_png(self, path: Path) -> None:
        """用 GDI+ 自带的 PNG 编码器保存（不依赖 PIL）。"""
        num_encoders = ctypes.c_uint(0)
        size = ctypes.c_uint(0)
        _check(_GdipGetImageEncodersSize(ctypes.byref(num_encoders), ctypes.byref(size)), "GdipGetImageEncodersSize")
        if size.value == 0:
            raise PlotError("GDI+ 没有可用的图片编码器")
        buffer = ctypes.create_string_buffer(size.value)
        _check(_GdipGetImageEncoders(num_encoders.value, size.value, buffer), "GdipGetImageEncoders")
        encoders = ctypes.cast(buffer, ctypes.POINTER(_ImageCodecInfo))
        png_clsid: Optional[_GUID] = None
        for index in range(num_encoders.value):
            if encoders[index].MimeType == "image/png":
                png_clsid = encoders[index].Clsid
                break
        if png_clsid is None:
            raise PlotError("GDI+ 找不到 PNG 编码器")
        path.parent.mkdir(parents=True, exist_ok=True)
        _check(
            _GdipSaveImageToFile(self._image, str(path), ctypes.byref(png_clsid), None),
            f"GdipSaveImageToFile({path})",
        )

    def close(self) -> None:
        for font in self._fonts.values():
            _GdipDeleteFont(font)
        for fmt in self._formats.values():
            _GdipDeleteStringFormat(fmt)
        for pen in self._pens.values():
            _GdipDeletePen(pen)
        for brush in self._brushes.values():
            _GdipDeleteBrush(brush)
        if self._family:
            _GdipDeleteFontFamily(self._family)
        if self._graphics:
            _GdipDeleteGraphics(self._graphics)
        if self._image:
            _GdipDisposeImage(self._image)
        _GdiplusShutdown(self._token)

    def __enter__(self) -> "_Canvas":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def _render_curve(data: CurveData, image_path: Path) -> None:
    """把 CurveData 画到 image_path（PNG）。"""
    x_low, x_high, x_step = _axis_range(data.x_values)
    y_low, y_high, y_step = _axis_range(data.y_values)

    x0, x1 = PLOT_LEFT, CANVAS_WIDTH - PLOT_RIGHT
    y0, y1 = PLOT_TOP, CANVAS_HEIGHT - PLOT_BOTTOM
    plot_w, plot_h = x1 - x0, y1 - y0

    def sx(value: float) -> float:
        return x0 + (value - x_low) / (x_high - x_low) * plot_w

    def sy(value: float) -> float:
        return y1 - (value - y_low) / (y_high - y_low) * plot_h

    with _Canvas(CANVAS_WIDTH, CANVAS_HEIGHT) as canvas:
        # ── 标题 / 副标题 ──────────────────────────────────────────────
        canvas.text("温度 — 输出功率曲线", 70, 30, 36, COLOR_TITLE, bold=True)
        skipped_note = f" · 跳过 {data.skipped} 行空值" if data.skipped else ""
        subtitle = (
            f"{data.csv_path.name} · 有效点 {len(data.x_values)} 个"
            f" · 温度 {_fmt_num(min(data.x_values))} ~ {_fmt_num(max(data.x_values))}"
            f" · 功率 {_fmt_num(min(data.y_values))} ~ {_fmt_num(max(data.y_values))}"
            f"{skipped_note}"
        )
        canvas.text(subtitle, 70, 86, 16, COLOR_SUBTITLE)

        # ── 网格与刻度 ────────────────────────────────────────────────
        x_ticks = _tick_values(x_low, x_high, x_step)
        y_ticks = _tick_values(y_low, y_high, y_step)
        x_decimals = _decimals_for_step(x_step)
        y_decimals = _decimals_for_step(y_step)

        for tick in x_ticks:
            px = sx(tick)
            canvas.line(px, y0, px, y1, COLOR_GRID, 1.0)
            canvas.line(px, y1, px, y1 + 6, COLOR_AXIS, 1.2)
            canvas.text(
                _fmt_tick(tick, x_decimals), px, y1 + 12, 15, COLOR_TICK_TEXT, align="center"
            )
        for tick in y_ticks:
            py = sy(tick)
            canvas.line(x0, py, x1, py, COLOR_GRID, 1.0)
            canvas.line(x0 - 6, py, x0, py, COLOR_AXIS, 1.2)
            canvas.text(
                _fmt_tick(tick, y_decimals), x0 - 12, py, 15, COLOR_TICK_TEXT, align="right", valign="center"
            )

        # ── 坐标轴边框 ────────────────────────────────────────────────
        canvas.line(x0, y0, x1, y0, COLOR_AXIS, 1.2)
        canvas.line(x0, y1, x1, y1, COLOR_AXIS, 1.6)
        canvas.line(x0, y0, x0, y1, COLOR_AXIS, 1.6)
        canvas.line(x1, y0, x1, y1, COLOR_AXIS, 1.2)

        # ── 数据曲线 ──────────────────────────────────────────────────
        points = [(sx(x), sy(y)) for x, y in zip(data.x_values, data.y_values)]
        if len(points) > 1:
            for (ax, ay), (bx, by) in zip(points, points[1:]):
                canvas.line(ax, ay, bx, by, COLOR_LINE, 2.6)
        if len(points) <= 200:
            for px, py in points:
                canvas.ellipse(px, py, 4.0, COLOR_POINT, COLOR_POINT_RING)
        else:
            # 点太多时只画线，避免一堆圆点糊成粗线
            pass

        # ── 轴标题 ────────────────────────────────────────────────────
        canvas.text("温度（°C）", (x0 + x1) / 2, 915, 22, COLOR_AXIS, align="center")
        canvas.rotated_text("B 软件读数（输出功率）", 58, (y0 + y1) / 2, 22, COLOR_AXIS)

        # ── 页脚（便于追溯是哪份 CSV / 什么时候画的）──────────────────
        footer = f"LaserControlAuto · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · {data.csv_path.name}"
        canvas.text(footer, CANVAS_WIDTH - 45, 960, 13, COLOR_FOOTER, align="right")

        canvas.save_png(image_path)


def default_image_path(csv_path: Path) -> Path:
    """默认图片路径：CSV 同目录、同名 + `_曲线.png`。"""
    return Path(csv_path).with_name(f"{Path(csv_path).stem}_曲线.png")


def plot_csv(csv_path: Path, image_path: Optional[Path] = None) -> dict[str, Any]:
    """读取 CSV → 画图 → 保存，返回一份统计信息（供任务日志打印）。

    参数：
        csv_path    要画的 CSV
        image_path  目标图片路径；None = CSV 同目录下的 `<同名>_曲线.png`
                    （只取文件名，目录永远是 CSV 所在目录 —— 这是任务约定）
    """
    csv_path = Path(csv_path)
    data = read_curve_csv(csv_path)

    if image_path is None:
        image_path = default_image_path(csv_path)
    else:
        image_path = Path(image_path)
        # 强制「和 CSV 同级」：用户填了子目录也只取文件名，避免图片跑丢
        image_path = csv_path.parent / image_path.name
        if image_path.suffix.lower() != ".png":
            image_path = image_path.with_suffix(".png")

    _render_curve(data, image_path)
    if not image_path.is_file() or image_path.stat().st_size <= 0:
        raise PlotError(f"图片没有成功写入：{image_path}")

    return {
        "image_path": image_path,
        "points": len(data.x_values),
        "skipped": data.skipped,
        "x_header": data.x_header,
        "y_header": data.y_header,
        "x_min": min(data.x_values),
        "x_max": max(data.x_values),
        "y_min": min(data.y_values),
        "y_max": max(data.y_values),
        "size": image_path.stat().st_size,
    }


def open_image(path: Path) -> bool:
    """用系统默认看图软件打开图片；失败只返回 False，不影响任务结果。"""
    target = str(Path(path))
    try:
        if sys.platform == "win32":
            os.startfile(target)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
    except OSError:
        return False
    return True


# 给自检脚本 / 其它模块用的公开别名（避免下划线开头的内部名字散出去）
__all__ = [
    "CurveData",
    "PlotError",
    "default_image_path",
    "find_latest_csv",
    "open_image",
    "plot_csv",
    "read_curve_csv",
]
