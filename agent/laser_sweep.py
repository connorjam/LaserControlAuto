"""激光控制自动化 —— 参数扫描的自定义识别与自定义动作。

对应流水线：assets/resource/pipeline/my_task.json 里的 LaserSweep* 节点。

流程：
    laser_sweep_begin        生成参数序列（20, 21, ... 30）并建好 CSV 表头
    laser_sweep_apply_param  把本轮参数值注入输入框节点的 input_text
    （点击 / 全选 / 输入 / 点 Setting 由流水线节点完成，不在本文件里）
    laser_find_nearest_setting  找出离 "Temperature" 最近的 "Setting" 并交给 Click
    laser_read_value_b       读取 B 软件测量值
                             · 配了 window_regex → 单独建 Win32 控制器截 B 的图
                             · 没配            → 用主控制器的截图（也就是 A 的画面）
                             · mode=fixed      → 直接返回占位值，不识别
    laser_sweep_record       把「序号, 设定值, 测量值, 时间」追加进 CSV
    laser_sweep_record_failed   识别失败时写入空值占位，保证行数对齐
    laser_sweep_advance      推进到下一个参数，跑完则跳去收尾节点
    laser_sweep_finish       打印结果文件路径，收尾
    laser_sweep_abort        定位失败时中止并打印原因

依赖：pip install MaaFw
"""

from __future__ import annotations

import csv
import ctypes
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

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
DEFAULT_HEADER = ["序号", "设定参数", "B软件测量值", "时间"]
DEFAULT_PATTERN = r"[-+]?\d+(?:\.\d+)?"

# 标签和数值都应该是「短文本」。界面上的日志/提示行动辄几十个字，
# 它们只要含有关键词就会冒充成标签，把整条定位链带偏（见 _short_hits）。
DEFAULT_ANCHOR_MAX_CHARS = 16  # "Temperature:"=12、"功率CH1"=6、"Wavelength"=10 都在内
DEFAULT_TARGET_MAX_CHARS = 16  # 按钮文字同样是短文本："Setting"=7、"è Setting"=9
DEFAULT_VALUE_MAX_CHARS = 24  # "-79.720dBm"=10、"1061 nm"=7 都在内
DEFAULT_MIN_DIGIT_RATIO = 0.3  # 数值文本里数字字符的最低占比

# 跨节点共享的运行状态；每次 laser_sweep_begin 都会整体重置
_state: dict[str, Any] = {
    "values": [],  # ["20", "21", ..., "30"]
    "index": 0,  # 当前跑到第几组
    "csv_path": None,  # 绝对路径 Path
    "measured": None,  # 本轮读到的测量值（float），None 表示没读到
    "image_size": None,  # 最近一次识别用的截图尺寸 (w, h)，用于按比例算偏移
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


def _resolve_csv_path(param: dict[str, Any]) -> Path:
    """决定结果 CSV 写到哪儿。

    支持两种写法：
      csv_dir  （推荐，UI 上让用户填文件夹）→ 自动拼上固定文件名
      csv_path （旧写法，直接给完整文件路径）

    用户可能填得很随意，所以这里一并兜底：
      · 空字符串 / 只有空格     → 用默认文件夹 output
      · 带引号 "D:\\x"          → 去掉引号
      · 以 .csv 结尾            → 当成完整文件路径
      · 绝对路径                → 原样使用
    """
    raw = str(param.get("csv_dir") or "").strip().strip('"').strip("'")
    if raw:
        candidate = Path(raw)
        target = candidate if candidate.suffix.lower() == ".csv" else candidate / "laser_sweep_result.csv"
        return _resolve(str(target))

    legacy = str(param.get("csv_path") or "").strip()
    if legacy:
        return _resolve(legacy)

    # 两个都没给 → 用默认文件夹 + 固定文件名（注意要带上文件名）
    return _resolve(str(Path(DEFAULT_CSV_DIR) / DEFAULT_CSV_NAME))


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


def _build_values(start: float, end: float, step: float) -> list[str]:
    """按 start / end / step 生成参数序列，用整数计数避免浮点累加误差。"""
    count = int(round((end - start) / step)) + 1
    return [_fmt(start + i * step) for i in range(max(count, 0))]


def _center(box: tuple[int, int, int, int]) -> tuple[float, float]:
    """矩形中心点。"""
    x, y, w, h = box
    return (x + w / 2.0, y + h / 2.0)


def _remember_image_size(image: Any) -> None:
    """记下当前截图的尺寸，供「按比例算偏移」使用。

    直接取识别回调收到的 argv.image 是最省事的——它一定存在，
    不用去依赖 controller.cached_image 之类的接口。
    """
    shape = getattr(image, "shape", None)
    if shape is not None and len(shape) >= 2:
        _state["image_size"] = (int(shape[1]), int(shape[0]))


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
    """把当前这一组结果追加写入 CSV。"""
    csv_path = _state.get("csv_path")
    values, index = _state["values"], _state["index"]

    if csv_path is None or index >= len(values):
        print("[laser_sweep] 状态未初始化或索引越界，无法写入 CSV")
        return False

    row = [
        index + 1,
        values[index],
        "" if measured is None else _fmt(measured),
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    ]

    try:
        # utf-8-sig 让 Excel 打开中文表头不乱码
        with Path(csv_path).open("a", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(row)
    except OSError as exc:
        print(f"[laser_sweep] 写入 CSV 失败: {exc}")
        return False

    shown = row[2] if row[2] != "" else "(空)"
    print(f"[laser_sweep] 已记录第 {row[0]} 行：设定={row[1]}, 测量={shown}")
    return True


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

    user32 = ctypes.WinDLL("user32", use_last_error=True)

    wndenumproc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows.argtypes = [wndenumproc, ctypes.c_void_p]
    user32.EnumWindows.restype = ctypes.c_bool
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.IsWindowVisible.restype = ctypes.c_bool
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int

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


def _screencap_window(hwnd: int) -> Any:
    """用 PrintWindow 抓取指定窗口的客户区，返回 BGR 的 numpy 数组。

    为什么不用 MaaFramework 的 Win32Controller：
        MaaAgentServer.dll 里的 MaaWin32ControllerCreate 只是个空壳，调用时
        会打印 "MaaAgentServer Not implement this API, Please use MaaFramework"
        并返回空句柄。也就是说 agent 进程里根本建不了第二个控制器。
        所以这里直接走 Win32 GDI，纯 ctypes，不依赖框架。

    返回的是窗口**客户区**的原始像素尺寸（不含标题栏），
    因此给 B 软件配 roi 时要用 B 窗口客户区的原始坐标。
    """
    import numpy as np

    if sys.platform != "win32":
        print("[laser_sweep] 窗口截图目前只实现了 Windows")
        return None

    _ensure_dpi_aware()

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    # 64 位下句柄必须用 c_void_p，否则会被截断成 32 位
    user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
    user32.GetClientRect.restype = ctypes.c_bool
    user32.GetWindowDC.argtypes = [ctypes.c_void_p]
    user32.GetWindowDC.restype = ctypes.c_void_p
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    user32.PrintWindow.restype = ctypes.c_bool
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
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
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user32.GetDpiForWindow.argtypes = [ctypes.c_void_p]
    user32.GetDpiForWindow.restype = ctypes.c_uint

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
    dpi = user32.GetDpiForWindow(handle) if sys.platform == "win32" else 0
    if not dpi or dpi == 96:
        prev_ctx = user32.SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_UNAWARE)
        print(f"[laser_sweep] 目标窗口 DPI={dpi}（不感知 DPI），已切到虚拟化坐标系截图")

    try:
        return _screencap_in_current_dpi_context(user32, gdi32, handle, hwnd, np)
    finally:
        if prev_ctx:
            user32.SetThreadDpiAwarenessContext(prev_ctx)


def _screencap_in_current_dpi_context(user32, gdi32, handle, hwnd, np):
    """在当前线程 DPI 上下文里完成「量客户区 → PrintWindow → 取像素」。"""
    rect = _RECT()
    if not user32.GetClientRect(handle, ctypes.byref(rect)):
        print(f"[laser_sweep] GetClientRect 失败 (hwnd={hwnd})")
        return None

    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        print(f"[laser_sweep] 客户区尺寸异常: {width}x{height}")
        return None

    window_dc = user32.GetWindowDC(handle)
    if not window_dc:
        print("[laser_sweep] GetWindowDC 失败")
        return None

    mem_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    old_bitmap = gdi32.SelectObject(mem_dc, bitmap)

    try:
        # PW_CLIENTONLY(1) | PW_RENDERFULLCONTENT(2)
        #   PW_CLIENTONLY 只抓客户区（不含标题栏），和框架对 A 软件的取景一致
        #   PW_RENDERFULLCONTENT 才能抓到 DirectComposition / 硬件加速的内容
        if not user32.PrintWindow(handle, mem_dc, 3):
            print(f"[laser_sweep] PrintWindow 失败 (hwnd={hwnd})")
            return None

        header = _BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height  # 负数 = 自顶向下，省得再翻转
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = 0  # BI_RGB

        buffer = ctypes.create_string_buffer(width * height * 4)
        if not gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0):
            print("[laser_sweep] GetDIBits 失败")
            return None

        # BGRA -> BGR（MaaFramework 的识别接口要 BGR）
        bgra = np.frombuffer(buffer, dtype=np.uint8).reshape(height, width, 4)
        return np.ascontiguousarray(bgra[:, :, :3])
    finally:
        gdi32.SelectObject(mem_dc, old_bitmap)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(handle, window_dc)


# --------------------------------------------------------------------------- #
# ① 初始化
# --------------------------------------------------------------------------- #
@AgentServer.custom_action("laser_sweep_begin")
class LaserSweepBegin(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        param = _param(argv.custom_action_param)

        # 先把「这次跑的到底是哪份代码」写进日志。
        # 换机排查时最怕的就是「文件到底替换成功没有」—— 带上文件名、字节数和
        # 修改时间，一眼就能跟手头那份对上（2026-09-22 就为这个来回猜过一轮）。
        print(f"[laser_sweep] agent 文件：{_agent_fingerprint()}")

        start = float(param.get("start", 20))
        end = float(param.get("end", 30))
        step = float(param.get("step", 1))

        if step <= 0:
            print(f"[laser_sweep] step 必须为正数，当前为 {step}")
            return False
        if end < start:
            print(f"[laser_sweep] 区间不合法：start={_fmt(start)} > end={_fmt(end)}")
            return False

        values = _build_values(start, end, step)
        if not values:
            print("[laser_sweep] 参数序列为空")
            return False

        csv_path = _resolve_csv_path(param)
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

        # 上一轮找到的 B 软件窗口可能已经关了，清掉重新找
        _window_cache.clear()

        # 尽力声明后台受管键（修饰键）；失败也不影响主流程
        _configure_controller(context)

        # 每轮等待时长：UI 上填的是「秒」，流水线用的是毫秒，在这里换算一次
        if not _apply_wait_seconds(context, param):
            return False

        _state.update(values=values, index=0, csv_path=csv_path, measured=None)

        print(f"[laser_sweep] 参数序列共 {len(values)} 组：{values[0]} → {values[-1]}（步长 {_fmt(step)}）")
        print(f"[laser_sweep] 结果文件：{csv_path}")
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
        # "input" 返回「标签与按钮之间」那个输入框的位置（每次执行都重新 OCR 定位，
        # 不依赖任何写死的比例，换电脑/换分辨率都能自己跟上）。
        return_box = str(param.get("return_box") or "target").lower()

        # 记下当前截图的尺寸，仅作日志/兜底用；定位本身已经不依赖它了
        _remember_image_size(argv.image)

        # 把这次真正生效的定位配置打出来：万一流水线没换成新版，
        # 日志里一眼就能看出来（near_text 缺失 = pipeline 还是旧的）
        print(
            f"[laser_sweep]   定位参数：anchor_text={anchor_texts!r} target_text={target_text!r} "
            f"return_box={return_box} near_text={near_texts!r} anchor_max_chars={anchor_max_chars}"
        )

        # 这两个词都要做「检测 + 识别」，所以 only_rec 必须是 False
        raw = _ocr_hits(context, argv.image, ocr_node, [], roi, only_rec=False)
        if not raw:
            return CustomRecognition.AnalyzeResult(
                box=None,
                detail={"error": "这一屏没有 OCR 到任何文字"},
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

        if return_box == "input":
            # ★ 在这里现场折算输入框位置：用的是**这一屏**刚 OCR 出来的标签框与按钮框，
            #   所以窗口尺寸/DPI 怎么变都不用改配置。
            chosen_box = _input_box_between(anchor_box, best_box, raw)
            returned = "input"
        elif return_box == "anchor":
            chosen_box = anchor_box
            returned = "anchor"
            print(f"[laser_sweep]   按 return_box=anchor 返回锚点框 {chosen_box}")
        else:
            chosen_box = best_box
            returned = "target"

        return CustomRecognition.AnalyzeResult(
            box=chosen_box,
            detail={
                "anchor": anchor_text_used,
                "anchor_box": list(anchor_box),
                "chosen": best_text,
                "chosen_box": list(best_box),
                "returned": returned,
                "input_box": list(chosen_box) if returned == "input" else None,
                "distance_sq": round(dist, 1),
                "anchor_count": len(anchors),
                "candidate_boxes": [list(b) for b, _ in candidates],
            },
        )


# --------------------------------------------------------------------------- #
# ④ 读取 B 软件测量值
# --------------------------------------------------------------------------- #
@AgentServer.custom_recognition("laser_read_value_b")
class LaserReadValueB(CustomRecognition):
    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> CustomRecognition.AnalyzeResult:
        param = _param(argv.custom_recognition_param)
        mode = str(param.get("mode", "fixed")).lower()

        if mode == "fixed":
            value = float(param.get("fixed_value", 0.0))
            _state["measured"] = value
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
            print(f"[laser_sweep]   B 软件窗口 {title!r} 截图 {shot.shape[1]}x{shot.shape[0]}")

        return self._analyze_by_ocr(context, argv, param, image)

    def _analyze_by_ocr(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
        param: dict[str, Any],
        image: Any,
    ) -> CustomRecognition.AnalyzeResult:
        ocr_node = str(param.get("ocr_node") or "LaserSweepOcrValueB")
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
            return CustomRecognition.AnalyzeResult(
                box=None,
                detail={"error": "OCR 未识别到内容"},
            )

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
            return CustomRecognition.AnalyzeResult(
                box=None,
                detail={
                    "error": f"识别到的内容里没有像数值的文本: {[t for _, t in hits][:12]}",
                },
            )

        if not anchor_texts:
            box, text, value = candidates[0]
            _state["measured"] = value
            print(f"[laser_sweep]   B 软件 OCR 文本 {text!r} → 测量值 = {_fmt(value)}")
            return CustomRecognition.AnalyzeResult(
                box=box,
                detail={"mode": "ocr", "text": text, "measured": value},
            )

        # ---- 找 anchor_text 旁边的那个数值 ----
        # 锚点同样只认短文本：B 软件日志区的
        #   "Start Wavelength 1548.000000rm not in range[...],please check!"
        # 含 "Wavelength"，不挡掉就会冒充成锚点。
        anchor_max_chars = int(param.get("anchor_max_chars", DEFAULT_ANCHOR_MAX_CHARS))
        anchors = _short_hits(_match_text(hits, anchor_texts), anchor_max_chars, "锚点")
        if not anchors:
            return CustomRecognition.AnalyzeResult(
                box=None,
                detail={
                    "error": f"没找到 {anchor_texts!r}",
                    "ocr_texts": [t for _, t in hits],
                },
            )

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
            _state["measured"] = value
            side = "右侧" if layout == "right" else "下方"
            print(
                f"[laser_sweep]   B 软件：{label!r} {side}的数值 {text!r}"
                f" → 测量值 = {_fmt(value)}（相邻 {gap:.0f}px）"
            )
            return CustomRecognition.AnalyzeResult(
                box=box,
                detail={
                    "mode": "ocr",
                    "anchor": label,
                    "anchor_box": list(anchor_box),
                    "layout": layout,
                    "text": text,
                    "measured": value,
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
        _state["measured"] = value
        print(
            f"[laser_sweep]   B 软件：离 {label!r} 最近的数值 {text!r}"
            f" → 测量值 = {_fmt(value)}  (距离²={dist:.0f})"
        )
        return CustomRecognition.AnalyzeResult(
            box=box,
            detail={
                "mode": "ocr",
                "anchor": label,
                "anchor_box": list(anchor_box),
                "text": text,
                "measured": value,
                "distance_sq": round(dist, 1),
                "fallback": True,
            },
        )


# --------------------------------------------------------------------------- #
# ⑤ 写 CSV
# --------------------------------------------------------------------------- #
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
        print(f"[laser_sweep] 扫描结束，结果文件：{_state.get('csv_path')}")
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
