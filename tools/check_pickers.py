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
    ④ anchor_text 同时兼容字符串和数组两种写法。
"""

from __future__ import annotations

import importlib.util
import sys
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
print(f"结果：{'全部通过 🎉' if failures == 0 else f'{failures} 项失败'}")
print("=" * 78)
sys.exit(1 if failures else 0)
