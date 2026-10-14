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
       （这条是用假 Context 数 run_recognition 调用次数来验的，不连软件也能跑）。
"""

from __future__ import annotations

import importlib.util
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
print(f"结果：{'全部通过 🎉' if failures == 0 else f'{failures} 项失败'}")
print("=" * 78)
sys.exit(1 if failures else 0)
