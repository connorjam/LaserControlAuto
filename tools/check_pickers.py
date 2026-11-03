"""挑选逻辑自检：不连软件、不依赖 maa 包，单独验证「找数值 / 夹输入框」的规则。

背景：B 软件读数和 A 软件点击定位，都靠 `agent/laser_sweep.py` 里那几个纯函数
（`_pick_right_side` / `_pick_below` / `_input_box_between`）。
它们一旦被改坏，只有在真机上跑一整轮才会暴露，太慢了 —— 所以这里用桩模块顶掉
maa，直接调函数、拿真实日志里的坐标当样例比对：

    python tools/check_pickers.py

用例覆盖：

    ① 光器件耦合系统的「标签右边就是数值」，
       而且不能被标签自身含的那个 "1" 骗到（2026-09-22 换电脑后踩的坑）；
    ② GaussianBeam 的「数值在标签正下方」依旧能选中；
    ③ A 软件用「标签 + Setting 按钮」夹输入框（输入框空 / 已有数值两种情况）；
    ④ anchor_text 同时兼容字符串和数组两种写法；
    ⑨ A 软件控件定位缓存：第一次 OCR 之后每轮直接复用，
       并且过期 / 窗口尺寸变化 / use_cache=false / relocate_every 四条失效路径都要走对
       （这条是用假 Context 数 run_recognition 调用次数来验的，不连软件也能跑）；
    ⑪ 扫描方向（2026-10-30）：四种方向写法、正/反序列严格倒序、CSV 方向标记，
       以及「先正后反」跑完第一段后真的会切到第二段、建第二份 CSV、组号回到 1。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import time
import types
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent


def install_maa_stub() -> None:
    class _RunArg:
        pass

    class _AnalyzeArg:
        pass

    class _AnalyzeResult:
        def __init__(self, box=None, detail=None):
            self.box = box
            self.detail = detail

    class CustomAction:
        RunArg = _RunArg

    class CustomRecognition:
        AnalyzeArg = _AnalyzeArg
        AnalyzeResult = _AnalyzeResult

    class Context:  # noqa: D401
        pass

    class AgentServer:
        @staticmethod
        def custom_action(_name):
            def deco(obj):
                return obj

            return deco

        @staticmethod
        def custom_recognition(_name):
            def deco(obj):
                return obj

            return deco

    maa = types.ModuleType("maa")
    maa.__path__ = []
    agent = types.ModuleType("maa.agent")
    agent.__path__ = []
    agent_server = types.ModuleType("maa.agent.agent_server")
    agent_server.AgentServer = AgentServer
    context_mod = types.ModuleType("maa.context")
    context_mod.Context = Context
    custom_action = types.ModuleType("maa.custom_action")
    custom_action.CustomAction = CustomAction
    custom_recognition = types.ModuleType("maa.custom_recognition")
    custom_recognition.CustomRecognition = CustomRecognition

    for name, module in {
        "maa": maa,
        "maa.agent": agent,
        "maa.agent.agent_server": agent_server,
        "maa.context": context_mod,
        "maa.custom_action": custom_action,
        "maa.custom_recognition": custom_recognition,
    }.items():
        sys.modules[name] = module


install_maa_stub()

spec = importlib.util.spec_from_file_location(
    "laser_sweep", WORKSPACE / "agent" / "laser_sweep.py"
)
laser_sweep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(laser_sweep)

PASS, FAIL = "✅", "❌"
failures = 0


def check(title: str, condition: bool, detail: str = "") -> None:
    global failures
    mark = PASS if condition else FAIL
    if not condition:
        failures += 1
    print(f"{mark} {title}" + (f"  → {detail}" if detail else ""))


print("=" * 78)
print("① B 软件（新）：锚点 '功率CH1'，右侧就是 -79.720dBm —— 真实日志数据")
print("=" * 78)
anchor_new = (748, 83, 89, 25)
# 只摘录日志里出现过的候选，值用正则能抓到的部分
candidates_new = [
    ((748, 83, 89, 25), "功率CH1", 1.0),  # ← 罪魁祸首：标签自己含 "1"
    ((850, 84, 132, 24), "-79.720dBm", -79.72),  # ← 真正要的
    ((879, 146, 30, 17), "1550", 1550.0),
    ((797, 187, 101, 15), "000> Init Report", 0.0),
    ((757, 257, 273, 19), "配置文件TEST.xm1已被读取，各参数已更新。谢谢", 0.0),
    ((15, 125, 12, 15), "0", 0.0),
    ((333, 362, 43, 15), "4.0000", 4.0),
]
picked = laser_sweep._pick_right_side(anchor_new, candidates_new)
for gap, box, text, value in picked:
    print(f"    候选：{text!r} @ {box}  相邻 {gap:.0f}px")
check("右侧布局选中了 -79.720dBm", bool(picked) and picked[0][2] == "-79.720dBm",
      picked[0][2] if picked else "一个都没选中")
check("自匹配 '功率CH1' 被排除", all(t != "功率CH1" for _, _, t, _ in picked))
check("下一行的 '1550' 被排除", all(t != "1550" for _, _, t, _ in picked))

print()
print("=" * 78)
print("② B 软件（旧）：锚点 'Wavelength'，数值在正下方 '1061 nm'")
print("=" * 78)
anchor_old = (600, 300, 110, 18)
candidates_old = [
    ((600, 300, 110, 18), "Wavelength", 0.0),  # 标签自己（不含数字，但保险起见也测）
    ((640, 322, 70, 18), "1061 nm", 1061.0),  # ← 正下方
    ((600, 200, 90, 18), "Power", 0.0),
    ((900, 300, 60, 18), "1200", 1200.0),
]
picked_below = laser_sweep._pick_below(anchor_old, candidates_old)
check("下方布局选中了 1061 nm", bool(picked_below) and picked_below[0][2] == "1061 nm",
      picked_below[0][2] if picked_below else "一个都没选中")
right_only = laser_sweep._pick_right_side(anchor_old, candidates_old)
gap_below = laser_sweep._edge_gap(anchor_old, picked_below[0][1]) if picked_below else 1e9
gap_right = laser_sweep._edge_gap(anchor_old, right_only[0][1]) if right_only else 1e9
check("统一评分后下方候选更近（右侧那个同行的数字抢不走）", gap_below < gap_right,
      f"下方 {gap_below:.0f}px vs 右侧 {gap_right:.0f}px")

print()
print("=" * 78)
print("③ A 软件：用「标签框 + 按钮框」夹出输入框（真实日志坐标）")
print("=" * 78)
label = (497, 293, 95, 16)  # 'Temperature:'  右边界 592
button = (726, 292, 61, 20)  # 'è Setting'     左边界 726
box_empty = laser_sweep._input_box_between(label, button, [])
cx = box_empty[0] + box_empty[2] / 2
check("空输入框取中点（592~726 之间）", 592 <= cx <= 726, f"中点 x = {cx:.0f}")
check("长度 = 标签与按钮的间距", box_empty[2] == 726 - 592, f"w = {box_empty[2]}")

box_with_text = laser_sweep._input_box_between(
    label, button, [((620, 295, 48, 14), "25.0")],  # 输入框里已经有值
)
check("夹到数字时直接用那块数字的框", box_with_text == (620, 295, 48, 14), str(box_with_text))

box_with_other = laser_sweep._input_box_between(
    label, button, [((620, 295, 40, 14), "PID")],  # 中间是文字而不是数字
)
check("中间是纯文字时不误用（退回中点）", box_with_other == box_empty, str(box_with_other))

print()
print("=" * 78)
print("④ anchor_text 支持字符串 / 数组")
print("=" * 78)
check("字符串", laser_sweep._anchor_texts({"anchor_text": "功率CH1"}) == ["功率CH1"])
check("数组", laser_sweep._anchor_texts({"anchor_text": ["功率CH1", "Wavelength"]})
      == ["功率CH1", "Wavelength"])
check("空值", laser_sweep._anchor_texts({}) == [])

print()
print("=" * 78)
print("⑤ 长文本必须被挡掉（A 软件的 Tips 行 / B 软件的日志行 —— 真实日志数据）")
print("=" * 78)
tips_line = "[2026.09.22-15:49:35] Tips：Setting TEC Temperature Successfully!"
label_hits = [
    ((40, 417, 97, 23), "Temperature"),
    ((497, 293, 95, 16), "Temperature:"),
    ((280, 410, 456, 20), tips_line),  # ← 冒充成 Temperature 标签的那行提示
]
matched = laser_sweep._match_text(label_hits, ["Temperature"])
check("裸匹配确实会把 Tips 行也算进来（说明这道护栏有必要）", len(matched) == 3)
kept = laser_sweep._short_hits(matched, laser_sweep.DEFAULT_ANCHOR_MAX_CHARS)
check("按长度过滤后只剩两个真标签", len(kept) == 2 and all(t != tips_line for _, t in kept),
      str([t for _, t in kept]))

wavelength_line = "Start Wavelength 1548.000000rm not in range[0.000000,0.000000],please check!"
wl_matched = laser_sweep._match_text([((745, 297, 473, 14), wavelength_line)], ["Wavelength"])
check("B 软件日志行也会冒充 'Wavelength'", len(wl_matched) == 1)
check("同样被长度过滤剔除",
      laser_sweep._short_hits(wl_matched, laser_sweep.DEFAULT_ANCHOR_MAX_CHARS) == [])

print()
print("=" * 78)
print("⑥ 数值候选必须「长得像数值」（真实日志数据）")
print("=" * 78)
value_samples = [
    ((850, 84, 132, 24), "-79.720dBm", True),
    ((640, 322, 70, 18), "1061 nm", True),
    ((742, 312, 554, 20),
     "code=-1073807265, VISA Write in MY TSL-510_Initialize.vi->my TSL-550 init combine.vi->TSL", False),
    ((745, 297, 473, 14), wavelength_line, False),
]
for _box, text, should_keep in value_samples:
    kept_ok = (
        len(text.strip()) <= laser_sweep.DEFAULT_VALUE_MAX_CHARS
        and laser_sweep._digit_ratio(text) >= laser_sweep.DEFAULT_MIN_DIGIT_RATIO
    )
    check(
        f"{'保留' if should_keep else '剔除'} {text[:32]!r}",
        kept_ok == should_keep,
        f"长度 {len(text.strip())}，数字占比 {laser_sweep._digit_ratio(text):.2f}",
    )

print()
print("=" * 78)
print("⑦ near_text：A 软件「左侧板」vs「Module Config 面板」（真实界面坐标）")
print("=" * 78)
left_panel_temp = ((40, 417, 97, 23), "Temperature")  # 左侧板 Laser → Temperature
module_cfg_temp = ((497, 293, 95, 16), "Temperature:")  # Module Config 面板里的那个
setting_button = (726, 292, 61, 20)  # 面板里的 Setting 按钮
module_config_hint = ((500, 40, 140, 18), "Module Config")  # 面板标题

both = [left_panel_temp, module_cfg_temp]
picked = laser_sweep._choose_anchor_by_hint(both, [module_config_hint])
check("near_text 选中 Module Config 面板里的那个", picked[1] == "Temperature:",
      f"{picked[1]!r} @ {picked[0]}")

# 就算 near_text 没命中（比如 OCR 没认出面板标题），配对逻辑也该指向同一个
nearest_by_button = min(
    both, key=lambda item: laser_sweep._distance_sq(item[0], setting_button)
)
check("兜底：与 Setting 按钮配对同样指向它", nearest_by_button[1] == "Temperature:",
      f"{nearest_by_button[1]!r} @ {nearest_by_button[0]}")

print()
print("=" * 78)
print("⑧ 按钮候选也必须挡长文本（真实日志：Tips 行同时冒充标签和按钮）")
print("=" * 78)
raw_hits = [
    ((726, 292, 61, 20), "è Setting"),  # 真按钮
    ((280, 410, 456, 20), tips_line),  # 冒充者：既含 Temperature 又含 Setting
    ((40, 417, 97, 23), "Temperature"),
    ((497, 293, 95, 16), "Temperature:"),
]
buttons = laser_sweep._short_hits(
    laser_sweep._match_text(raw_hits, ["Setting"]), laser_sweep.DEFAULT_TARGET_MAX_CHARS
)
check("按钮候选只剩真按钮", len(buttons) == 1 and buttons[0][1] == "è Setting",
      str([t for _, t in buttons]))

best_row = min(
    [left_panel_temp, module_cfg_temp],
    key=lambda item: laser_sweep._distance_sq(item[0], buttons[0][0]),
)
check("配对选中 Module Config 面板那一行", best_row[1] == "Temperature:",
      f"{best_row[1]!r} @ {best_row[0]}")

fitted = laser_sweep._input_box_between(module_cfg_temp[0], buttons[0][0], [])
mid_x = fitted[0] + fitted[2] / 2
check("夹出的输入框落在标签与按钮之间", 592 <= mid_x <= 726, f"中点 x = {mid_x:.0f}")
check("不再退化成 (228,420) 那种位置", abs(mid_x - 228) > 100, f"中点 x = {mid_x:.0f}")

print()
print("=" * 78)
print("⑨ 定位缓存：第一次全屏 OCR，之后每轮直接复用（2026-10-12 新增）")
print("=" * 78)


class _FakeHit:
    def __init__(self, box, text):
        self.box, self.text = box, text


class _FakeReco:
    """仿 maa 的识别结果对象：hit / all_results / filtered_results。"""

    def __init__(self, hits):
        self.hit = bool(hits)
        self.all_results = [_FakeHit(box, text) for box, text in hits]
        self.filtered_results = self.all_results
        self.best_result = self.all_results[0] if self.all_results else None


class _FakeImage:
    """只用到 shape，numpy 的排法是 (高, 宽, 通道)。"""

    def __init__(self, width=1331, height=720):
        self.shape = (height, width, 3)


class _FakeContext:
    """数一数 run_recognition 被调了几次 —— 次数的减少就是省下的 OCR。"""

    def __init__(self, hits):
        self.hits, self.calls = hits, 0

    def run_recognition(self, _node, _image, pipeline_override=None):
        self.calls += 1
        return _FakeReco(self.hits)


class _FakeArgv:
    def __init__(self, param, image):
        self.custom_recognition_param = param
        self.image = image
        self.roi = [0, 0, image.shape[1], image.shape[0]]


# 真实界面上的文字（含左侧板那个同名 Temperature 和面板标题）
screen_hits = [
    ((40, 417, 97, 23), "Temperature"),  # 左侧板：干扰项
    ((497, 293, 95, 16), "Temperature:"),  # Module Config 面板里要用的
    ((726, 292, 61, 20), "è Setting"),  # 真按钮
    ((500, 40, 140, 18), "Module Config"),  # near_text 参照词
    ((280, 410, 456, 20), tips_line),  # 冒充者：又含 Temperature 又含 Setting
]

base_param = {
    "anchor_text": "Temperature",
    "target_text": "Setting",
    "near_text": ["Module Config", "ModuleConfig"],
    "roi": [0, 0, 0, 0],
    "ocr_node": "LaserSweepOcrSetting",
}
image = _FakeImage()
expected_input = laser_sweep._input_box_between((497, 293, 95, 16), (726, 292, 61, 20), screen_hits)

laser_sweep._forget_controls()
ctx = _FakeContext(screen_hits)
reco = laser_sweep.LaserFindNearestSetting()

first = reco.analyze(ctx, _FakeArgv({**base_param, "return_box": "input"}, image))
check("第一轮：识别出输入框", tuple(first.box) == expected_input,
      f"{first.box} vs 期望 {expected_input}")
check("第一轮确实跑了 OCR", ctx.calls == 1, f"调用 {ctx.calls} 次")
check("第一轮写入了缓存", laser_sweep._controls_snapshot() is not None)

second = reco.analyze(ctx, _FakeArgv({**base_param, "return_box": "target"}, image))
check("第二轮（点 Setting）：直接吃缓存，没再 OCR", ctx.calls == 1, f"调用 {ctx.calls} 次")
check("第二轮拿到的是按钮框", tuple(second.box) == (726, 292, 61, 20), str(second.box))
check("第二轮的 detail 标了 cached=True", second.detail.get("cached") is True)

third = reco.analyze(ctx, _FakeArgv({**base_param, "return_box": "input"}, image))
check("第三轮（下一组的输入框）：仍然没 OCR", ctx.calls == 1, f"调用 {ctx.calls} 次")
check("第三轮拿到的是缓存的输入框", tuple(third.box) == expected_input, str(third.box))
check("复用次数在累加", laser_sweep._controls_snapshot()["hits"] == 2,
      str(laser_sweep._controls_snapshot()["hits"]))

# 缓存过期 → 必须重新 OCR
laser_sweep._controls_snapshot()["stamp"] = time.monotonic() - 9999
stale = reco.analyze(ctx, _FakeArgv({**base_param, "return_box": "input"}, image))
check("缓存过期后重新 OCR", ctx.calls == 2, f"调用 {ctx.calls} 次")
check("重新定位的结果依旧正确", tuple(stale.box) == expected_input, str(stale.box))

# 窗口尺寸变了 → 老坐标立刻作废
bigger = _FakeImage(width=1920, height=1080)
reco.analyze(ctx, _FakeArgv({**base_param, "return_box": "input"}, bigger))
check("截图尺寸变了就重新 OCR", ctx.calls == 3, f"调用 {ctx.calls} 次")

# use_cache=false → 退回老行为，每次都 OCR
no_cache = {**base_param, "return_box": "input", "use_cache": False}
ctx2 = _FakeContext(screen_hits)
reco.analyze(ctx2, _FakeArgv(no_cache, image))
reco.analyze(ctx2, _FakeArgv(no_cache, image))
check("use_cache=false 时每次都 OCR（老行为还在）", ctx2.calls == 2, f"调用 {ctx2.calls} 次")

# relocate_every：长跑防跑偏，每 N 次复用强制重认一次
relocate = {**base_param, "return_box": "input", "relocate_every": 2}
laser_sweep._forget_controls()  # 从干净状态开始数，免得吃到上一段的缓存
ctx3 = _FakeContext(screen_hits)
reco.analyze(ctx3, _FakeArgv(relocate, image))  # 第 1 次：OCR 建缓存（hits=0）
reco.analyze(ctx3, _FakeArgv(relocate, image))  # 复用 1
reco.analyze(ctx3, _FakeArgv(relocate, image))  # 复用 2 → 达到上限
reco.analyze(ctx3, _FakeArgv(relocate, image))  # 强制重认
check("relocate_every=2 时每 2 次复用重认一遍", ctx3.calls == 2, f"调用 {ctx3.calls} 次")

# 缓存失效后重新定位：先用「上次位置外扩」的小 ROI 试，认到了就不必全屏
laser_sweep._forget_controls()
laser_sweep._remember_controls((497, 293, 95, 16), (726, 292, 61, 20), expected_input, (1331, 720))
laser_sweep._controls_snapshot()["stamp"] = time.monotonic() - 9999  # 只让它过期，坐标还准
hint_roi = laser_sweep._pad_roi(
    list(laser_sweep._controls_snapshot()["boxes"].values()), laser_sweep.DEFAULT_HINT_ROI_PAD, (1331, 720)
)
check("提示 ROI 把三个框都圈住了并裁进屏幕",
      hint_roi[0] >= 0 and hint_roi[1] >= 0 and hint_roi[0] + hint_roi[2] <= 1331
      and hint_roi[1] + hint_roi[3] <= 720 and hint_roi[2] < 1331 and hint_roi[3] < 720,
      f"roi={hint_roi}")
check("提示 ROI 比全屏小得多（这才是它省时间的原因）",
      hint_roi[2] * hint_roi[3] < 1331 * 720 * 0.5, f"{hint_roi[2]}x{hint_roi[3]}")

# 参数解析：字符串/布尔混着写也不能翻车
check("_flag 认 'false'/'0'/'off'", not laser_sweep._flag("false") and not laser_sweep._flag("0")
      and not laser_sweep._flag("off") and laser_sweep._flag("true") and laser_sweep._flag(None))
check("_number 认不出来就退回默认值",
      laser_sweep._number("abc", 7.0) == 7.0 and laser_sweep._number("1.5", 7.0) == 1.5
      and laser_sweep._number(None, 7.0) == 7.0)

laser_sweep._forget_controls()

print()
print("=" * 78)
print("⑩ 扫描点数：填 0 走步长，填 >0 以点数优先（2026-10-14 新增）")
print("=" * 78)


def values_of(**param):
    """照 laser_sweep_begin 的算法走一遍：解析 count → 生成序列。"""
    count_list = laser_sweep._resolve_count(param)
    count = count_list[0] if count_list else None
    return laser_sweep._build_values(
        float(param["start"]), float(param["end"]), float(param.get("step", 1)), count
    )


# ── 老行为必须一字不差（老配置 / 热修复包不能受影响） ──────────────────────
check("不传 count：20→30 步长 1 仍是 11 组",
      values_of(start=20, end=30, step=1) == [str(v) for v in range(20, 31)],
      str(values_of(start=20, end=30, step=1)))
check("count=0：与不传完全一致（退回按步长）",
      values_of(start=20, end=30, step=1, count=0) == values_of(start=20, end=30, step=1))
check("count 写空串 / 认不出的值：也退回按步长",
      values_of(start=20, end=30, step=1, count="") == values_of(start=20, end=30, step=1)
      and values_of(start=20, end=30, step=1, count="abc") == values_of(start=20, end=30, step=1))
check("步长 0.5 的小数序列没被破坏",
      values_of(start=20, end=21, step=0.5) == ["20", "20.5", "21"],
      str(values_of(start=20, end=21, step=0.5)))

# ── 点数模式 ────────────────────────────────────────────────────────────────
check("20→30 填 11 个点 = 步长 1（11 组）",
      values_of(start=20, end=30, step=1, count=11) == [str(v) for v in range(20, 31)],
      str(values_of(start=20, end=30, step=1, count=11)))
check("20→30 填 6 个点 = 步长 2",
      values_of(start=20, end=30, step=1, count=6) == ["20", "22", "24", "26", "28", "30"],
      str(values_of(start=20, end=30, step=1, count=6)))
check("点数优先：步长填 1 也拦不住（count=3 → 20,25,30）",
      values_of(start=20, end=30, step=1, count=3) == ["20", "25", "30"],
      str(values_of(start=20, end=30, step=1, count=3)))
check("count=1 = 只扫起始值那一个点（不是报错）",
      values_of(start=20, end=30, step=1, count=1) == ["20"])
check("count=2 = 起点和终点",
      values_of(start=20, end=30, step=1, count=2) == ["20", "30"])

# ── 除不尽：步长反算后照样把点数走满，终点吸附回结束值 ────────────────────
odd = values_of(start=20, end=30, step=1, count=4)  # 反算步长 10/3 ≈ 3.3333
check("除不尽时仍走满 4 个点", len(odd) == 4, str(odd))
check("数值正确：20 / 23.333333 / 26.666667 / 30",
      odd == ["20", "23.333333", "26.666667", "30"], str(odd))
check("最后一个点是干净的 30，不是 29.999999",
      odd[-1] == "30", odd[-1])
check("最后一个点没有越过结束值",
      all(float(v) <= 30 for v in odd), str(odd))

# 全长序列的收尾也要干净：步长 0.1 在二进制里除不尽，别收在 20.999999
tenth = values_of(start=20, end=21, step=0.1)
check("步长 0.1 收尾在干净的 21（不是 20.999999）",
      tenth[-1] == "21" and len(tenth) == 11, f"{tenth[-1]}，共 {len(tenth)} 个")

# ── 边界 ───────────────────────────────────────────────────────────────────
check("负区间 + 点数：-10→0 填 3 个点",
      values_of(start=-10, end=0, step=1, count=3) == ["-10", "-5", "0"],
      str(values_of(start=-10, end=0, step=1, count=3)))
check("负点数被拒绝（_resolve_count 返回负值，由 begin 拦下）",
      laser_sweep._resolve_count({"count": -3}) == [-3])
check("非整数点数：退回按步长而不是乱算",
      laser_sweep._resolve_count({"count": "2.5"}) == [])
check("字符串形式的正整数照样认",
      laser_sweep._resolve_count({"count": "11"}) == [11])

print()
print("=" * 78)
print("⑪ 扫描方向：四种方向、两段切换、CSV 方向标记（2026-10-30 新增）")
print("=" * 78)

# ── 方向解析：标准代码 / 中文 / 英文别名都要认 ─────────────────────────────
check("正向：标准代码、中文、英文别名都认",
      laser_sweep._normalize_direction("forward") == "forward"
      and laser_sweep._normalize_direction("正向") == "forward"
      and laser_sweep._normalize_direction("Ascending") == "forward"
      and laser_sweep._normalize_direction("从小到大") == "forward")
check("反向：标准代码、中文、英文别名都认",
      laser_sweep._normalize_direction("reverse") == "reverse"
      and laser_sweep._normalize_direction("反向") == "reverse"
      and laser_sweep._normalize_direction("从大到小") == "reverse")
check("两段方向也认中文与下划线写法",
      laser_sweep._normalize_direction("先正后反") == "forward_reverse"
      and laser_sweep._normalize_direction("先反后正") == "reverse_forward"
      and laser_sweep._normalize_direction("forward_reverse") == "forward_reverse"
      and laser_sweep._normalize_direction("Reverse-Forward") == "reverse_forward")
check("空值 / 认不出的写法返回 None（由调用方决定兜底）",
      laser_sweep._normalize_direction(None) is None
      and laser_sweep._normalize_direction("") is None
      and laser_sweep._normalize_direction("斜着扫") is None)

# ── 方向 → 分段顺序 ────────────────────────────────────────────────────────
check("正向 = 1 段（正向）", laser_sweep._direction_phases("forward") == ["forward"])
check("反向 = 1 段（反向）", laser_sweep._direction_phases("reverse") == ["reverse"])
check("先正后反 = 正向 → 反向",
      laser_sweep._direction_phases("forward_reverse") == ["forward", "reverse"])
check("先反后正 = 反向 → 正向",
      laser_sweep._direction_phases("reverse_forward") == ["reverse", "forward"])
check("认不出的方向按正向处理（有日志告警，不会静默乱跑）",
      laser_sweep._direction_phases("斜着扫") == ["forward"])

# ── 参数序列：反向必须与正向严格倒序（同一组点，只是顺序相反）──────────────
asc_values = laser_sweep._build_phase_values(20, 30, 1, None, "forward")
rev_values = laser_sweep._build_phase_values(20, 30, 1, None, "reverse")
check("正向序列从小到大", asc_values == [str(v) for v in range(20, 31)], str(asc_values))
check("反向序列从大到小", rev_values == list(reversed(asc_values)), str(rev_values))
check("正反两段是同一组点（严格一一对照）", list(reversed(rev_values)) == asc_values)
odd_asc = laser_sweep._build_phase_values(20, 30, 1, 4, "forward")
odd_rev = laser_sweep._build_phase_values(20, 30, 1, 4, "reverse")
check("点数模式（除不尽）反向也不改变点集",
      odd_rev == list(reversed(odd_asc)) and sorted(odd_rev) == sorted(odd_asc),
      f"{odd_asc} / {odd_rev}")

# ── 文件名方向标记 ────────────────────────────────────────────────────────
check("默认名加方向标记：…_正向.csv",
      laser_sweep._append_direction_marker("20261030_200000_20_30_11.csv", "forward")
      == "20261030_200000_20_30_11_正向.csv")
check("反向标记：…_反向.csv",
      laser_sweep._append_direction_marker("结果.csv", "reverse") == "结果_反向.csv")
check("没有方向时文件名不变（老调用方/自检兼容）",
      laser_sweep._append_direction_marker("结果.csv", None) == "结果.csv")
check("用户已经写了标记不会重复叠加",
      laser_sweep._append_direction_marker("结果_正向.csv", "forward") == "结果_正向.csv"
      and laser_sweep._append_direction_marker("结果_正向.csv", "reverse") == "结果_反向.csv")
check("拆标记：基础名 + 方向",
      laser_sweep._split_direction_marker("20261030_200000_20_30_11_反向")
      == ("20261030_200000_20_30_11", "reverse"))
check("没有标记时拆出 (原名, None)",
      laser_sweep._split_direction_marker("普通结果") == ("普通结果", None))

check("方向说明：单方向带区间",
      laser_sweep._direction_plan_text("reverse", 20.0, 30.0) == "反向（30 → 20）",
      laser_sweep._direction_plan_text("reverse", 20.0, 30.0))
check("方向说明：两段按执行顺序写清两段",
      "正向 20 → 30" in laser_sweep._direction_plan_text("forward_reverse", 20.0, 30.0)
      and "再反向 30 → 20" in laser_sweep._direction_plan_text("forward_reverse", 20.0, 30.0),
      laser_sweep._direction_plan_text("forward_reverse", 20.0, 30.0))


# ── 方向来源优先级：方向节点 > 节点参数 > 框架节点数据 > 默认正向 ──────────
class _DirCtx:
    """假的 Context.get_node_data。

    ⚠️ MaaFramework 5.x 实际返回的是 **V2 嵌套格式**（action.param...）；
    2026-10-30 之前的兜底只认 V1 扁平格式，实测永远读不到 —— 这里两种都造出来测。
    """

    def __init__(self, direction, v2=True):
        self.direction = direction
        self.v2 = v2

    def get_node_data(self, _name):
        if self.direction is None:
            return None
        if self.v2:
            return {
                "action": {
                    "type": "Custom",
                    "param": {
                        "custom_action": "laser_sweep_direction",
                        "custom_action_param": {"direction": self.direction},
                    },
                }
            }
        return {"custom_action_param": {"direction": self.direction}}


# _resolve_direction 的最后一层会读真实 MFA 配置；自检里桩掉保证可复现
_real_task_direction = laser_sweep._load_direction_from_task_config
laser_sweep._load_direction_from_task_config = lambda: None

laser_sweep._state.update(pending_direction="reverse")
check("方向节点暂存的值优先",
      laser_sweep._resolve_direction(_DirCtx("forward"), {"direction": "forward"}) == "reverse")
laser_sweep._state.update(pending_direction=None)
check("没有暂存时用本节点参数",
      laser_sweep._resolve_direction(_DirCtx("reverse"), {"direction": "forward"}) == "forward")
check("都没有时问框架要方向节点参数（V2 嵌套格式，MaaFramework 5.x 实际返回）",
      laser_sweep._resolve_direction(_DirCtx("reverse"), {}) == "reverse")
check("框架返回 V1 扁平格式也兼容（老框架 / 自检桩）",
      laser_sweep._resolve_direction(_DirCtx("forward", v2=False), {}) == "forward")
check("框架也读不到时默认正向（会打告警日志）",
      laser_sweep._resolve_direction(_DirCtx(None), {}) == "forward")
laser_sweep._load_direction_from_task_config = _real_task_direction

# 任务配置兜底：MFA 实例配置可能保存 select 的选中 case（老 entry 场景）
check("任务配置 selected_cases 能读出方向",
      laser_sweep._extract_direction_from_task(
          {"TaskItems": [{"name": "激光参数扫描", "option": [{"name": "扫描方向", "selected_cases": ["反向"]}]}]}
      ) == "reverse")
check("任务配置 data 字符串能读出方向",
      laser_sweep._extract_direction_from_task(
          {"task": [{"name": "激光参数扫描", "option": [{"name": "扫描方向", "data": "先正后反"}]}]}
      ) == "forward_reverse")
check("老版 option 字典布局也能读出方向",
      laser_sweep._extract_direction_from_task(
          {"task": [{"name": "激光参数扫描", "option": {"扫描方向": "反向"}}]}
      ) == "reverse")
check("「扫描方向」嵌在「扫描设置」的 sub_options 里也能读出",
      laser_sweep._extract_direction_from_task(
          {"TaskItems": [{"name": "激光参数扫描", "option": [
              {"name": "扫描设置", "sub_options": [
                  {"name": "扫描方向", "selected_cases": ["先反后正"]},
              ]}
          ]}]}
      ) == "reverse_forward")
_split_root = {
    "TaskItems": [{"name": "激光参数扫描", "option": [
        {"name": "扫描设置", "sub_options": [
            {"name": "扫描设置项", "data": {
                "起始值": "25", "结束值": "35", "步长": "2",
                "扫描点数": "0", "等待秒数": "8",
            }},
            {"name": "扫描输出项", "data": {
                "结果文件夹名": "myrun", "结果文件名": "r.csv",
                "跑完自动打开": "false", "输出总目录": "D:\\out",
            }},
        ]},
    ]}]
}
_split_settings = laser_sweep._extract_scan_task_settings(_split_root)
check("拆分后的「扫描范围项 + 扫描输出项」能一起从任务配置读出",
      _split_settings == [{
          "start": "25", "end": "35", "step": "2", "count": "0", "wait_sec": "8",
          "csv_dir": "myrun", "csv_name": "r.csv",
          "open_when_done": "false", "output_root": "D:\\out",
      }], str(_split_settings))

_saved_scan_loader = laser_sweep._load_scan_settings_from_task
laser_sweep._load_scan_settings_from_task = lambda: dict(_split_settings[0])
try:
    _merged_settings = laser_sweep._merge_scan_settings_from_task({"start": "20", "end": "21"})
finally:
    laser_sweep._load_scan_settings_from_task = _saved_scan_loader
check("begin 的合并逻辑能从拆分后的任务配置补回输出字段",
      _merged_settings.get("csv_dir") == "myrun"
      and _merged_settings.get("csv_name") == "r.csv"
      and _merged_settings.get("output_root") == "D:\\out"
      and _merged_settings.get("open_when_done") == "false",
      str(_merged_settings))

# ── 两段切换：真的建第二份 CSV、组号回到 1、第一份不丢 ─────────────────────
_TMP_DIR = WORKSPACE / "debug" / "_check_pickers_tmp"
_TMP_DIR.mkdir(parents=True, exist_ok=True)
# 切段时会通过 _remember_output_root 落盘运行状态；指到临时目录，
# 别把用户真实的 laser_sweep_output.json 覆盖成测试路径。
_real_state_file = laser_sweep._state_file
laser_sweep._state_file = lambda: _TMP_DIR / "laser_sweep_output.json"
_phase1_csv = _TMP_DIR / "case_1_正向.csv"
_phase2_csv = _TMP_DIR / "case_1_反向.csv"
_phase1_csv.write_text("序号,设定参数,B软件测量值,时间,备注\n1,20,-79.7,t,\n", encoding="utf-8-sig")
_phase2_csv.unlink(missing_ok=True)

laser_sweep._state.update(
    direction="forward_reverse",
    direction_label="先正后反",
    phases=[
        {"code": "forward", "label": "正向", "values": ["20", "21"], "csv_path": _phase1_csv, "started": True},
        {"code": "reverse", "label": "反向", "values": ["21", "20"], "csv_path": _phase2_csv, "started": False},
    ],
    phase_index=0,
    values=["20", "21"],
    index=1,
    csv_path=_phase1_csv,
    csv_paths=[_phase1_csv],
    csv_header=["序号", "设定参数", "B软件测量值", "时间", "备注"],
    out_root=_TMP_DIR,
    guard={"strikes": 3, "abnormal": [-95.0], "reference": -79.7},
    sample_failures={"ocr": 2},
    last_note="旧备注",
    measured=-79.7,
)

class _AdvCtx:
    def __init__(self):
        self.next_calls = []

    def override_next(self, node, next_list):
        self.next_calls.append((node, list(next_list)))
        return True


_adv_ctx = _AdvCtx()
_adv_argv = type("_Arg", (), {"node_name": "LaserSweepAdvance", "custom_action_param": {}})()
laser_sweep.LaserSweepAdvance().run(_adv_ctx, _adv_argv)
check("第一段跑完 → 切到第二段（不改道 Done）", _adv_ctx.next_calls == [], str(_adv_ctx.next_calls))
check("第二段 CSV 已建好且写了表头",
      _phase2_csv.is_file() and _phase2_csv.read_text(encoding="utf-8-sig").splitlines()[0].startswith("序号"),
      str(_phase2_csv))
check("组号回到第 1 组、当前文件切换到第二份",
      laser_sweep._state["index"] == 0
      and laser_sweep._state["phase_index"] == 1
      and laser_sweep._state["values"] == ["21", "20"]
      and laser_sweep._state["csv_path"] == _phase2_csv)
check("csv_paths 记下两份（绘图任务要用）",
      laser_sweep._state["csv_paths"] == [_phase1_csv, _phase2_csv],
      str(laser_sweep._state["csv_paths"]))
check("每段的重测计数清零、可信基准保留",
      laser_sweep._state["guard"]["strikes"] == 0
      and laser_sweep._state["guard"]["abnormal"] == []
      and laser_sweep._state["guard"]["reference"] == -79.7)
check("每段的备注/当轮读数被清干净",
      laser_sweep._state["last_note"] == "" and laser_sweep._state["measured"] is None)

# 第二段跑完 → 没有下一段，改道 Done
laser_sweep._state["index"] = len(laser_sweep._state["values"]) - 1
_adv_ctx2 = _AdvCtx()
laser_sweep.LaserSweepAdvance().run(_adv_ctx2, _adv_argv)
check("两段都跑完 → 改道 Done",
      _adv_ctx2.next_calls == [("LaserSweepAdvance", ["LaserSweepDone"])],
      str(_adv_ctx2.next_calls))

# 收尾日志要能看到两份文件（这里只验证状态字段齐全，日志由人工看）
check("收尾能拿到两份 CSV 与计划总数",
      len(laser_sweep._state["csv_paths"]) == 2 and laser_sweep._planned_total() == 4,
      f"{laser_sweep._state['csv_paths']} / {laser_sweep._planned_total()}")

# ── 第二段 CSV 建不出来：不能谎报"全部完成"（2026-10-30 审查指出）──────────
_blocked = _TMP_DIR / "blocked"
_blocked.write_text("这是一个文件，不是文件夹", encoding="utf-8")
laser_sweep._state.update(
    phases=[
        {"code": "forward", "label": "正向", "values": ["20"], "csv_path": _phase1_csv, "started": True},
        {"code": "reverse", "label": "反向", "values": ["20"], "csv_path": _blocked / "x.csv", "started": False},
    ],
    phase_index=0,
    values=["20"],
    index=0,
    csv_path=_phase1_csv,
    csv_paths=[_phase1_csv],
    csv_header=["序号", "设定参数", "B软件测量值", "时间", "备注"],
    out_root=_TMP_DIR,
    guard={"strikes": 0, "abnormal": [], "reference": None},
    sample_failures={},
    measured=None,
    last_note="",
    run_start=time.monotonic(),
    started_at="2026-10-30 22:00:00",
)
_ok_next = laser_sweep._start_next_phase()
check("第二段结果文件建不出来 → _start_next_phase 返回 False", _ok_next is False)
check("失败会记下 phase_failed（供收尾日志区分）",
      isinstance(laser_sweep._state.get("phase_failed"), dict),
      str(laser_sweep._state.get("phase_failed")))
check("失败时第二段没被标记为 started",
      laser_sweep._state["phases"][1]["started"] is False)
check("失败时 csv_paths 不混入未开始的文件",
      laser_sweep._state["csv_paths"] == [_phase1_csv],
      str(laser_sweep._state["csv_paths"]))

_fail_ctx = _AdvCtx()
_fail_out = io.StringIO()
with contextlib.redirect_stdout(_fail_out):
    laser_sweep.LaserSweepAdvance().run(_fail_ctx, _adv_argv)
_fail_text = _fail_out.getvalue()
check("失败收尾日志不谎报『已完成全部』",
      "已完成全部" not in _fail_text and "没能开始" in _fail_text, _fail_text[-200:])
check("失败收尾日志报出「已完成 1 组 / 计划 2 组」",
      "已完成 1 组 / 计划 2 组" in _fail_text, _fail_text[-200:])
check("失败后仍改道 Done",
      _fail_ctx.next_calls == [("LaserSweepAdvance", ["LaserSweepDone"])],
      str(_fail_ctx.next_calls))

_fail_summary = laser_sweep._compose_summary(state=laser_sweep._state)
check("摘要不会给未开始的段列参数区间",
      "参数区间（反向）" not in _fail_summary["body"], _fail_summary["body"][:200])
check("摘要明确标出「未开始的段」",
      "未开始的段" in _fail_summary["body"] and "反向" in _fail_summary["body"],
      _fail_summary["body"][:200])

# 恢复真实状态文件函数，再清理临时产物
laser_sweep._state_file = _real_state_file
for _path in (_phase1_csv, _phase2_csv, _blocked, _TMP_DIR / "laser_sweep_output.json"):
    _path.unlink(missing_ok=True)
try:
    _TMP_DIR.rmdir()
except OSError:
    pass

print()
print("=" * 78)
print("⑫ 整条开工链路：方向节点 → begin → 两段 → 收尾/摘要（2026-10-30）")
print("=" * 78)


class _BeginCtx:
    """够 begin / advance / finish 用的假 Context（不连框架、不动真文件）。"""

    class _Controller:
        pass

    class _Tasker:
        def __init__(self):
            self.controller = _BeginCtx._Controller()

    def __init__(self):
        self.tasker = self._Tasker()
        self.pipeline_calls = []
        self.next_calls = []

    def override_pipeline(self, data):
        self.pipeline_calls.append(data)
        return True

    def override_next(self, node, next_list):
        self.next_calls.append((node, list(next_list)))
        return True

    def get_node_data(self, _name):
        return None


class _BeginArg:
    def __init__(self, param, node="LaserSweepBegin"):
        self.custom_action_param = json.dumps(param, ensure_ascii=False)
        self.node_name = node


_BEGIN_TMP = WORKSPACE / "debug" / "_check_pickers_begin"
_BEGIN_TMP.mkdir(parents=True, exist_ok=True)
_saved_state = dict(laser_sweep._state)
_saved_funcs = {
    "_merge_scan_settings_from_task": laser_sweep._merge_scan_settings_from_task,
    "_load_mail_settings_from_email_task": laser_sweep._load_mail_settings_from_email_task,
    "_state_file": laser_sweep._state_file,
    "_open_folder": laser_sweep._open_folder,
}
try:
    # 隔离掉真实任务配置 / 真实状态文件 / 真实资源管理器窗口
    laser_sweep._merge_scan_settings_from_task = (
        lambda p: (laser_sweep._state.__setitem__("scan_settings_from_task", False) or p)
    )
    laser_sweep._load_mail_settings_from_email_task = lambda: {}
    laser_sweep._state_file = lambda: _BEGIN_TMP / "laser_sweep_output.json"
    laser_sweep._open_folder = lambda _path: True

    _dir_ctx = _BeginCtx()
    laser_sweep._state["pending_direction"] = None
    laser_sweep.LaserSweepDirection().run(
        _dir_ctx, _BeginArg({"direction": "forward_reverse"}, "LaserSweepDirection")
    )
    check("方向节点把选中的方向暂存进 _state",
          laser_sweep._state.get("pending_direction") == "forward_reverse",
          str(laser_sweep._state.get("pending_direction")))

    _begin_ctx = _BeginCtx()
    _begin_ok = laser_sweep.LaserSweepBegin().run(
        _begin_ctx,
        _BeginArg(
            {
                "start": 20,
                "end": 30,
                "step": 1,
                "count": 0,
                "wait_sec": 0,
                "csv_dir": "",
                "csv_name": "",
                "output_root": str(_BEGIN_TMP / "output"),
                "open_when_done": False,
            }
        ),
    )
    check("begin 成功（方向节点 → begin 链路通）", _begin_ok is True, str(_begin_ok))
    _phases = laser_sweep._state["phases"]
    check("先正后反 = 计划两段", len(_phases) == 2, str(len(_phases)))
    check("第一段 CSV 已建好并有表头",
          _phases[0]["csv_path"].is_file()
          and _phases[0]["csv_path"].read_text(encoding="utf-8-sig").startswith("序号"),
          str(_phases[0]["csv_path"]))
    check("第二段 CSV 先不创建（等第一段跑完）", not _phases[1]["csv_path"].exists())
    check("两段落在同一文件夹、基础名相同（只差方向标记）",
          _phases[0]["csv_path"].parent == _phases[1]["csv_path"].parent
          and _phases[0]["csv_path"].name
          == _phases[1]["csv_path"].name.replace("_反向", "_正向"),
          f"{_phases[0]['csv_path']} / {_phases[1]['csv_path']}")
    check("第一段序列正向 20 → 30",
          laser_sweep._state["values"][0] == "20"
          and laser_sweep._state["values"][-1] == "30",
          f"{laser_sweep._state['values'][0]}..{laser_sweep._state['values'][-1]}")
    check("计划总组数 = 11 + 11 = 22", laser_sweep._planned_total() == 22,
          str(laser_sweep._planned_total()))
    check("方向暂存已被 begin 消费掉（不留到下一次）",
          laser_sweep._state["pending_direction"] is None)
    _state_json = json.loads(laser_sweep._state_file().read_text(encoding="utf-8"))
    check("开工时状态文件记录第一段 CSV（绘图任务用）",
          _state_json.get("csv_paths") == [str(_phases[0]["csv_path"])],
          str(_state_json.get("csv_paths")))

    # 第一段：逐点写 11 行，然后推进 → 自动切第二段
    for _i in range(len(laser_sweep._state["values"])):
        laser_sweep._state["index"] = _i
        laser_sweep._append_row(-79.50 + _i * 0.01)
    laser_sweep.LaserSweepAdvance().run(
        _BeginCtx(), _BeginArg({}, "LaserSweepAdvance")
    )
    check("第一段跑完后自动切到第二段",
          laser_sweep._state["phase_index"] == 1
          and laser_sweep._state["index"] == 0
          and laser_sweep._state["csv_path"] == _phases[1]["csv_path"],
          f"phase={laser_sweep._state['phase_index']} index={laser_sweep._state['index']}")
    check("第二段序列反向 30 → 20",
          laser_sweep._state["values"][0] == "30"
          and laser_sweep._state["values"][-1] == "20",
          f"{laser_sweep._state['values'][0]}..{laser_sweep._state['values'][-1]}")
    check("两段 CSV 都进了 csv_paths",
          laser_sweep._state["csv_paths"] == [_phases[0]["csv_path"], _phases[1]["csv_path"]],
          str(laser_sweep._state["csv_paths"]))

    # 第二段：逐点写 11 行，然后推进 → 收尾
    for _i in range(len(laser_sweep._state["values"])):
        laser_sweep._state["index"] = _i
        laser_sweep._append_row(-79.60 + _i * 0.01)
    _finish_ctx = _BeginCtx()
    laser_sweep.LaserSweepAdvance().run(_finish_ctx, _BeginArg({}, "LaserSweepAdvance"))
    check("两段都跑完才改道 Done",
          _finish_ctx.next_calls == [("LaserSweepAdvance", ["LaserSweepDone"])],
          str(_finish_ctx.next_calls))

    # 两份 CSV 的行数 / 序号 / 设定值顺序都要对
    _rows1 = [
        line.split(",")
        for line in _phases[0]["csv_path"].read_text(encoding="utf-8-sig").strip().splitlines()
    ]
    _rows2 = [
        line.split(",")
        for line in _phases[1]["csv_path"].read_text(encoding="utf-8-sig").strip().splitlines()
    ]
    check("第一段 CSV = 表头 + 11 行", len(_rows1) == 12, str(len(_rows1)))
    check("第一段设定值 20 → 30",
          len(_rows1) == 12 and _rows1[1][1] == "20" and _rows1[-1][1] == "30")
    check("第二段 CSV = 表头 + 11 行", len(_rows2) == 12, str(len(_rows2)))
    check("第二段设定值 30 → 20",
          len(_rows2) == 12 and _rows2[1][1] == "30" and _rows2[-1][1] == "20")
    check("两份 CSV 各自从序号 1 开始",
          len(_rows1) == 12 and len(_rows2) == 12
          and _rows1[1][0] == "1" and _rows2[1][0] == "1")

    _finish_ok = laser_sweep.LaserSweepFinish().run(
        _BeginCtx(), _BeginArg({"open_when_done": False}, "LaserSweepDone")
    )
    check("收尾成功（open_when_done=false 时不开文件夹）", _finish_ok is True)
    _state_json = json.loads(laser_sweep._state_file().read_text(encoding="utf-8"))
    check("收尾后状态文件同时记着两份 CSV",
          _state_json.get("csv_paths") == [str(_phases[0]["csv_path"]), str(_phases[1]["csv_path"])]
          and _state_json.get("last_csv") == str(_phases[1]["csv_path"]),
          str(_state_json))

    # 完成摘要要能看出「两段 / 两份文件 / 总组数」
    _summary = laser_sweep._compose_summary(state=laser_sweep._state)
    check("完成摘要写明扫描方向",
          "扫描方向：先正后反" in _summary["body"], _summary["body"][:120])
    check("完成摘要列出两份结果文件",
          _phases[0]["csv_path"].name in _summary["body"]
          and _phases[1]["csv_path"].name in _summary["body"], "见摘要【文件】段")
    check("完成摘要按两段报进度（已记录 22 行）",
          "计划 22 组" in _summary["summary"] and "已记录 22 行" in _summary["summary"],
          _summary["summary"])

    # 用这次真跑出来的两份 CSV 走一遍绘图任务：两张图都要有
    # （check_pickers 没有把 agent/ 加进 sys.path，这里显式加载并注册绘图模块）
    _plot_spec = importlib.util.spec_from_file_location(
        "plot_curve", WORKSPACE / "agent" / "plot_curve.py"
    )
    _plot_mod = importlib.util.module_from_spec(_plot_spec)
    # 先注册再执行：plot_curve 里用了 @dataclass，dataclasses 会从 sys.modules
    # 里按 cls.__module__ 反查模块，没注册就会 AttributeError: 'NoneType'
    sys.modules["plot_curve"] = _plot_mod
    _plot_spec.loader.exec_module(_plot_mod)
    _plot_csvs = laser_sweep._resolve_plot_csvs({})
    check("绘图任务能从本次扫描拿到两份 CSV",
          _plot_csvs == [_phases[0]["csv_path"], _phases[1]["csv_path"]], str(_plot_csvs))
    _plot_ok = laser_sweep.LaserSweepPlotCurve().run(
        None,
        _BeginArg(
            {"source_csv": "", "image_name": "", "open_when_done": False},
            "LaserSweepPlotCurve",
        ),
    )
    _img1 = _plot_mod.default_image_path(_phases[0]["csv_path"])
    _img2 = _plot_mod.default_image_path(_phases[1]["csv_path"])
    check("两段扫描后，绘图任务成功", _plot_ok is True, str(_plot_ok))
    check("正/反两份 CSV 各生成一张图", _img1.is_file() and _img2.is_file(),
          f"{_img1.name} / {_img2.name}")

    # 再跑一次「反向」单方向：1 段、1 份 CSV、序列 30 → 20
    laser_sweep._state["pending_direction"] = None
    laser_sweep.LaserSweepDirection().run(
        _BeginCtx(), _BeginArg({"direction": "reverse"}, "LaserSweepDirection")
    )
    _rev_ok = laser_sweep.LaserSweepBegin().run(
        _BeginCtx(),
        _BeginArg(
            {
                "start": 20,
                "end": 30,
                "step": 1,
                "count": 0,
                "wait_sec": 0,
                "csv_dir": "single_reverse",
                "csv_name": "",
                "output_root": str(_BEGIN_TMP / "output"),
                "open_when_done": False,
            }
        ),
    )
    _rev_path = laser_sweep._state["phases"][0]["csv_path"]
    check("反向单扫：begin 成功且只计划 1 段",
          _rev_ok is True and len(laser_sweep._state["phases"]) == 1,
          str(len(laser_sweep._state["phases"])))
    check("反向单扫：文件名带 _反向",
          _rev_path.name.endswith("_反向.csv"), _rev_path.name)
    check("反向单扫：序列 30 → 20",
          laser_sweep._state["values"][0] == "30"
          and laser_sweep._state["values"][-1] == "20",
          f"{laser_sweep._state['values'][0]}..{laser_sweep._state['values'][-1]}")
    check("反向单扫：csv_paths 只有一份",
          laser_sweep._state["csv_paths"] == [_rev_path],
          str(laser_sweep._state["csv_paths"]))
finally:
    laser_sweep._state.clear()
    laser_sweep._state.update(_saved_state)
    for _name, _func in _saved_funcs.items():
        setattr(laser_sweep, _name, _func)
    import shutil as _shutil

    _shutil.rmtree(_BEGIN_TMP, ignore_errors=True)

print()
print("=" * 78)
print(f"结果：{'全部通过 🎉' if failures == 0 else f'{failures} 项失败'}")
print("=" * 78)
sys.exit(1 if failures else 0)
