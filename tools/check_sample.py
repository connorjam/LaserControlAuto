"""读数采样自检：验证「1 秒窗口里反复读、取平均」这一层的判据。

背景（2026-10-27 主人要求）：
    原来每个参数点只读一次 B 软件（整屏 OCR，约 5.5 秒），记下来的只是某一瞬间
    的值。而 B 软件的读数本身在抖（同组内差 0.05 是常态，和组间差异同量级），
    单次采样等于随手抓了其中一个，曲线起伏有一半来自读数噪声。

    现在改成：先在整屏里定位一次数值框（贵，只做一次），然后在 **1 秒**的时间窗里
    反复读那一小块，采到几次算几次（由 OCR 实际速度决定），最后取平均数写进 CSV。

    python tools/check_sample.py

这套逻辑里只有两件事会真出问题，所以断言全压在它们身上：

    ① **算得对不对** —— 平均数、剔误读的"正常带"、样本为空 / 全是离群值时
       不能静默变成一个错数（那会直接写进 CSV，而且看起来像是正常数据）
    ② **跳转与状态对不对** —— 缓存命不命中、采样失败后要不要退回整屏、
       有没有把脏值记成 measured（后面读数守卫要拿它当基准）

为什么用桩而不是真跑：真跑要开 B 软件、要 OCR 模型，而且「OCR 到底多快」
在开发机上量到的数字没有参考价值（工控机和开发机能差 5 倍）。
这里把 reader 换成可控的假函数，专门测「次数由时间决定」和「聚合规则」，
这两条才是代码里的判断逻辑；OCR 本身是框架的事。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
import types
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
PASS, FAIL = "✅", "❌"
failures = 0


def check(title: str, condition: bool, detail: str = "") -> None:
    global failures
    if not condition:
        failures += 1
    print(f"{PASS if condition else FAIL} {title}" + (f"  → {detail}" if detail else ""))


def install_maa_stub() -> None:
    """顶掉 maa 包，让 laser_sweep.py 能在没有框架、没有 GUI 的机器上被导入。"""

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

    class Context:
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
ls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ls)


# --------------------------------------------------------------------------- #
# 假控制器 / 假上下文：只在采样循环需要「重截一帧」时被碰到
# --------------------------------------------------------------------------- #
class FakeJob:
    def __init__(self, image):
        self._image = image

    def wait(self):
        return self

    def get(self, wait: bool = False):
        return self._image


class FakeController:
    def __init__(self, images=None):
        self.calls = 0
        self._images = list(images or [])
        self.fail = False

    def post_screencap(self):
        self.calls += 1
        if self.fail:
            raise OSError("截图接口挂了")
        image = self._images.pop(0) if len(self._images) > 1 else (self._images[0] if self._images else object())
        return FakeJob(image)


class FakeTasker:
    def __init__(self, controller):
        self.controller = controller


class FakeContext:
    def __init__(self, controller=None):
        self.tasker = FakeTasker(controller or FakeController())


def reset_state(**overrides):
    ls._state.update(
        values=["20"],
        index=0,
        csv_path=None,
        measured=None,
        last_measured=None,
        history=[],
        guard={},
        alert={},
        value_box=None,
        sample_stats=[],
        image_size=None,
    )
    ls._state.update(overrides)


# --------------------------------------------------------------------------- #
# ① 采样循环：次数由时间决定，不由写死的常数决定
# --------------------------------------------------------------------------- #
print("=" * 78)
print("① 采样循环：一秒能读几次，由 OCR 实际速度决定")
print("=" * 78)

# 快的机器：每次 20 毫秒 → 一秒能读几十次（受上限约束）
fast_calls = {"n": 0}


def fast_reader():
    fast_calls["n"] += 1
    time.sleep(0.02)
    return 100.0 + fast_calls["n"] * 0.01


reset_state()
samples, stats = ls._sample_loop(fast_reader, window_sec=1.0, max_count=200, slow_sec=5.0)
check("快的机器一秒内读了多次（不是只读一次）", stats["attempts"] >= 20, f"{stats['attempts']} 次")
check("采样时长接近 1 秒窗（±0.35 秒容差）", 0.6 <= stats["seconds"] <= 1.4, f"{stats['seconds']:.2f} 秒")
check("样本数与成功读取次数一致（都成功）", len(samples) == stats["attempts"],
      f"样本 {len(samples)} / 尝试 {stats['attempts']}")

# 慢一点的机器：每次 250 毫秒 → 一秒只能读 4 次左右
slow_calls = {"n": 0}


def medium_reader():
    slow_calls["n"] += 1
    time.sleep(0.25)
    return 50.0 + slow_calls["n"]


reset_state()
samples2, stats2 = ls._sample_loop(medium_reader, window_sec=1.0, max_count=200, slow_sec=5.0)
check("慢机器读得少（≤8 次），但也不是 0 次", 1 <= stats2["attempts"] <= 8, f"{stats2['attempts']} 次")
check("同一段代码、不同机器 → 采样次数确实不同",
      stats2["attempts"] < stats["attempts"], f"{stats2['attempts']} < {stats['attempts']}")

# 上限必须真正生效（防呆）
reset_state()
_never_ending = {"n": 0}


def instant_reader():
    _never_ending["n"] += 1
    time.sleep(0.001)
    return 1.0


_, stats3 = ls._sample_loop(instant_reader, window_sec=10.0, max_count=7, slow_sec=5.0)
check("次数上限生效（防呆：窗再长也不越过 max_count）", stats3["attempts"] == 7, f"{stats3['attempts']} 次")

# 单次就超过 slow_sec：立刻收手，不能把一轮拖成几十秒
reset_state()
t0 = time.monotonic()
_, stats4 = ls._sample_loop(lambda: time.sleep(0.3) or 1.0, window_sec=1.0, max_count=60, slow_sec=0.2)
took = time.monotonic() - t0
check("单次读数超过 sample_slow_sec → 只读一次就收手", stats4["attempts"] == 1, f"{stats4['attempts']} 次")
check("慢路径不会把这一轮拖长", took < 0.9, f"{took:.2f} 秒")
check("慢路径被标记出来（日志要能说明为什么只读一次）", stats4["slow"] is True)

# reader 自己抛异常：单次失败要跳过，不能中断整轮
reset_state()


def flaky_reader():
    if _never_ending["n"] % 2 == 0:
        _never_ending["n"] += 1
        raise RuntimeError("偶发失败")
    _never_ending["n"] += 1
    return 42.0


_, stats5 = ls._sample_loop(flaky_reader, window_sec=0.5, max_count=6, slow_sec=5.0)
check("单次采样抛异常 → 被吞掉并继续，不中断整轮", stats5["attempts"] >= 1, f"{stats5['attempts']} 次")

# seed：定位时顺便拿到的那一次要计入样本，不能白扔
reset_state()
_, stats6 = ls._sample_loop(lambda: 0.0, window_sec=0.05, max_count=1, slow_sec=5.0, seed=7.5)
check("定位时拿到的那一次被计入样本（seed）", stats6["samples"][0] == 7.5 and stats6["seeded"],
      str(stats6["samples"]))


# --------------------------------------------------------------------------- #
# ② 聚合：取平均，但先把明显的 OCR 误读剔掉
# --------------------------------------------------------------------------- #
print()
print("=" * 78)
print("② 聚合：取平均，但一个误读不能把平均值拉歪")
print("=" * 78)

mean, info = ls._aggregate_samples([-79.70, -79.72, -79.75, -79.68], 3.0, 1.0)
check("正常抖动 → 取算术平均",
      all(abs(mean - v) < 0.05 for v in [-79.70, -79.72, -79.75, -79.68]), f"平均 {mean}")
expected = sum([-79.70, -79.72, -79.75, -79.68]) / 4
check("平均数算得准", abs(mean - expected) < 1e-9, f"{mean} vs {expected}")
check("没有样本被剔除", info["dropped"] == [], str(info["dropped"]))

# 混进一个小数点右移的误读（README 里记录过的真实形态）
mean2, info2 = ls._aggregate_samples([-79.70, -79.72, -797.20, -79.75, -79.68], 3.0, 1.0)
check("误读 -797.20 被剔除、不参与平均", len(info2["dropped"]) == 1 and info2["dropped"][0] == -797.20,
      str(info2["dropped"]))
check("剔掉误读后平均值仍然正确（≈ -79.71）", abs(mean2 - (-79.7125)) < 0.01, f"{mean2}")
check("如果不剔除，平均值会被拉歪（说明这一步确实必要）",
      abs(sum([-79.70, -79.72, -797.20, -79.75, -79.68]) / 5 - mean2) > 100,
      f"含误读的平均 = {sum([-79.70, -79.72, -797.20, -79.75, -79.68]) / 5}")

# 丢负号 / 少一位，同样要挡住
mean3, info3 = ls._aggregate_samples([1061.0, 1061.2, 79.72, 1060.9], 3.0, 1.0)
check("丢负号那类离谱值（79.72 混在 1061 里）被剔除",
      len(info3["dropped"]) == 1, str(info3["dropped"]))

# 样本极少且互相矛盾：宁可记一个可疑值交给守卫，也不能返回 None
mean4, info4 = ls._aggregate_samples([0.0, 100.0], 3.0, 1.0)
check("样本全是离群值时退回全部样本的平均（不返回 None）",
      mean4 is not None and abs(mean4 - 50.0) < 1e-9, f"{mean4}")
check("这种兜底情况在统计里能看出来", info4["kept"] == 2, str(info4))

# 空样本
mean5, info5 = ls._aggregate_samples([], 3.0, 1.0)
check("一个样本都没有 → 返回 None（交给上层判识别失败）", mean5 is None, str(mean5))

# 单个样本
mean6, _ = ls._aggregate_samples([-79.72], 3.0, 1.0)
check("只有一个样本时平均值就是它本身", mean6 == -79.72, f"{mean6}")

# 大数值靠百分比：1000.0 上 1% = ±10，2.0 的抖动该放行
mean7, info7 = ls._aggregate_samples([1000.0, 1002.0, 998.0], 3.0, 1.0)
check("大数值时按百分比给的带宽更宽（2.0 的抖动不算误读）",
      info7["dropped"] == [], str(info7["dropped"]))

# 中位数不受离群值影响：这是选它的理由
check("中位数不会被离群值带偏",
      ls._median([1.0, 2.0, 3.0, 1000.0]) == 2.5, str(ls._median([1.0, 2.0, 3.0, 1000.0])))


# --------------------------------------------------------------------------- #
# ③ 数值框缓存：配置或尺寸一变就作废，绝不拿旧坐标硬认
# --------------------------------------------------------------------------- #
print()
print("=" * 78)
print("③ 数值框缓存：拿不准就重新整屏定位")
print("=" * 78)

BASE_PARAM = {
    "window_regex": "^(GaussianBeam)$",
    "anchor_text": ["Wavelength"],
    "roi": [0, 0, 0, 0],
    "prefer": "auto",
}

reset_state()
check("没缓存时返回 None", ls._value_box_cached((1440, 829), BASE_PARAM) is None)

ls._remember_value_box((100, 200, 60, 20), {"layout": "below", "anchor": "Wavelength"},
                       (1440, 829), BASE_PARAM, fast=True)
cached = ls._value_box_cached((1440, 829), BASE_PARAM)
check("同配置同尺寸 → 缓存命中", cached is not None and cached["box"] == [100, 200, 60, 20], str(cached))

check("截图尺寸变了 → 缓存作废", ls._value_box_cached((1280, 720), BASE_PARAM) is None)
check("换了锚点词 → 缓存作废",
      ls._value_box_cached((1440, 829), {**BASE_PARAM, "anchor_text": ["功率CH1"]}) is None)
check("换了窗口正则 → 缓存作废",
      ls._value_box_cached((1440, 829), {**BASE_PARAM, "window_regex": "^(other)$"}) is None)
check("换了 roi → 缓存作废",
      ls._value_box_cached((1440, 829), {**BASE_PARAM, "roi": [0, 0, 400, 300]}) is None)

ls._forget_value_box("测试")
check("清空后缓存不再命中", ls._value_box_cached((1440, 829), BASE_PARAM) is None)


# --------------------------------------------------------------------------- #
# ④ 窄 ROI：外扩后被裁进图内，不能让 OCR 拿到越界框
# --------------------------------------------------------------------------- #
print()
print("=" * 78)
print("④ 采样 ROI：外扩之后必须还在图里")
print("=" * 78)

check("普通框按 pad 外扩", ls._pad_box((100, 200, 60, 20), 8, (1440, 829)) == [92, 192, 76, 36],
      str(ls._pad_box((100, 200, 60, 20), 8, (1440, 829))))
check("贴左上角的框被裁到 0", ls._pad_box((2, 3, 40, 20), 8, (1440, 829)) == [0, 0, 50, 31],
      str(ls._pad_box((2, 3, 40, 20), 8, (1440, 829))))
check("贴右下角的框不越界", ls._pad_box((1400, 800, 35, 25), 8, (1440, 829)) == [1392, 792, 48, 37],
      str(ls._pad_box((1400, 800, 35, 25), 8, (1440, 829))))
check("尺寸未知时只保证非负", ls._pad_box((2, 3, 40, 20), 8, None) == [0, 0, 50, 31],
      str(ls._pad_box((2, 3, 40, 20), 8, None)))
check("宽高为 0 的框被拒绝（免得 OCR 拿到空 ROI）", ls._pad_box((10, 10, 0, 20), 8, (1440, 829)) is None)
check("框格式不对时返回 None（不抛异常）", ls._pad_box((1, 2), 8, (1440, 829)) is None)

# fast 判定：决定采样走「窄 ROI（快）」还是「整屏（慢）」，判错就等于白等
SCREEN = (1440, 829)
check("正常的数值框判为窄（走快路径）",
      ls._is_narrow_box((200, 300, 130, 24), SCREEN) is True)
check("整屏大小的框判为宽（不会假称快路径）",
      ls._is_narrow_box((0, 0, 1440, 829), SCREEN) is False)
check("占半屏的框也判为宽",
      ls._is_narrow_box((0, 0, 1440, 500), SCREEN) is False)
check("尺寸未知时按窄处理（读不出会自己回退）",
      ls._is_narrow_box((200, 300, 130, 24), None) is True)
check("框格式不对时判为宽（安全侧）", ls._is_narrow_box((1, 2), SCREEN) is False)



# --------------------------------------------------------------------------- #
# ⑤ 整条读数链路：用假 OCR 走一遍 analyze()，看落库的到底是哪个数
# --------------------------------------------------------------------------- #
print()
print("=" * 78)
print("⑤ 整条链路：落库的是「多次采样的平均」，不是某一次的快照")
print("=" * 78)


class FakeAnalyzeArg:
    def __init__(self, param, image=None, roi=(0, 0, 0, 0)):
        self.custom_recognition_param = json.dumps(param, ensure_ascii=False)
        self.image = image if image is not None else FakeImage((1440, 829))
        self.roi = roi


class FakeImage:
    def __init__(self, size):
        self.shape = (size[1], size[0], 3)


FULL_SCREEN_TEXT = "功率CH1"
VALUE_TEXT = "-79.72"


def fake_ocr_hits(context, image, node, wanted, roi, only_rec):
    """假 OCR：整屏时返回锚点 + 数值，窄 ROI 时返回一个带抖动的数值。"""
    is_narrow = bool(roi) and (roi[2] > 0 and roi[3] > 0 and (roi[2] < 900 or roi[3] < 600))
    if is_narrow or only_rec:
        # 窄 ROI：模拟 B 软件刷新抖动（每次稍微不同）
        jitter = (_jitter["n"] % 5) * 0.02
        _jitter["n"] += 1
        return [((200, 300, 130, 24), f"{-(79.72 + jitter):.3f}dBm")]
    return [
        ((100, 290, 90, 25), FULL_SCREEN_TEXT),
        ((200, 300, 130, 24), f"{VALUE_TEXT}dBm"),
        ((100, 420, 200, 20), "code=-1073807265, VISA Write in MY TSL-510_Initialize.vi"),
    ]


_jitter = {"n": 0}
real_ocr_hits = ls._ocr_hits
ls._ocr_hits = fake_ocr_hits
real_find_window = ls._find_window
ls._find_window = lambda param: None  # 走主控制器分支（不用真的抓窗口）

try:
    reset_state()
    param = {
        "mode": "ocr",
        "window_regex": "",
        "anchor_text": ["功率CH1"],
        "roi": [0, 0, 0, 0],
        "pattern": r"[-+]?\d+(?:\.\d+)?",
        "value_max_chars": 24,
        "min_digit_ratio": 0.3,
        "anchor_max_chars": 16,
        "ocr_node": "LaserSweepOcrValueB",
        "only_rec": False,
        "sample_enabled": True,
        "sample_window_sec": 0.4,
        "sample_max_count": 60,
        "sample_slow_sec": 5,
        "sample_roi_pad": 8,
        "sample_outlier_abs": 3.0,
        "sample_outlier_pct": 1,
    }
    ctx = FakeContext(FakeController([FakeImage((1440, 829))]))
    result = ls.LaserReadValueB().analyze(ctx, FakeAnalyzeArg(param))

    detail = result.detail or {}
    check("识别成功（读到数）", detail.get("measured") is not None, str(detail.get("error")))
    check("确实采了多次（不是单点快照）", int(detail.get("sample_count") or 0) > 1,
          f"{detail.get('sample_count')} 次")
    measure = detail.get("measured")
    check("记录的是采样平均，落在抖动区间里",
          -79.95 < float(measure) < -79.65, f"{measure}")
    check("_state['measured'] 也是这个平均值（守卫要拿它当基准）",
          ls._state.get("measured") == float(measure), f"{ls._state.get('measured')}")

    # 采样时每帧都重新截图（拿的必须是最新画面，否则采的是同一张旧图）
    check("采样期间反复重新截图", ctx.tasker.controller.calls >= 2, f"{ctx.tasker.controller.calls} 次")
    check("数值框被记住（下一轮直接窄 ROI，不用再整屏 OCR）",
          isinstance(ls._state.get("value_box"), dict), str(ls._state.get("value_box")))
    check("采样统计留在 _state 里（排查时能看出这台机器一秒读几次）",
          bool(ls._state.get("sample_stats")), str(ls._state.get("sample_stats"))[:120])

    # 第二轮：应该走缓存，不再整屏 OCR
    full_scan_calls = {"n": 0}
    base_fake = fake_ocr_hits

    def counting_ocr(context, image, node, wanted, roi, only_rec):
        is_narrow = bool(roi) and roi[2] < 900
        if not is_narrow and not only_rec:
            full_scan_calls["n"] += 1
        return base_fake(context, image, node, wanted, roi, only_rec)

    ls._ocr_hits = counting_ocr
    ctx2 = FakeContext(FakeController([FakeImage((1440, 829))]))
    result2 = ls.LaserReadValueB().analyze(ctx2, FakeAnalyzeArg(param))
    check("第二轮（同一配置同一尺寸）不再整屏 OCR", full_scan_calls["n"] == 0,
          f"整屏 OCR {full_scan_calls['n']} 次")
    check("第二轮照样读到了数", (result2.detail or {}).get("measured") is not None,
          str((result2.detail or {}).get("error")))
    ls._ocr_hits = fake_ocr_hits

    # 关掉采样：退回旧行为，只读一次
    _jitter["n"] = 0
    reset_state()
    off_param = {**param, "sample_enabled": False}
    ctx3 = FakeContext(FakeController([FakeImage((1440, 829))]))
    result3 = ls.LaserReadValueB().analyze(ctx3, FakeAnalyzeArg(off_param))
    detail3 = result3.detail or {}
    check("sample_enabled=false → 只读一次（旧行为仍可用）",
          len(detail3.get("samples") or []) == 1, str(detail3.get("samples")))
    check("关掉采样时不再反复截图", ctx3.tasker.controller.calls == 0,
          f"{ctx3.tasker.controller.calls} 次")

    # 整屏都认不到数：必须返回失败（让流水线写空值占位），不能编一个数出来
    ls._ocr_hits = lambda *a, **k: []
    reset_state()
    ctx4 = FakeContext(FakeController())
    result4 = ls.LaserReadValueB().analyze(ctx4, FakeAnalyzeArg({**param, "sample_enabled": False}))
    check("整屏一个数都认不到 → 判失败（不编造数据）",
          (result4.detail or {}).get("measured") is None and bool((result4.detail or {}).get("error")),
          str((result4.detail or {}).get("error")))
finally:
    ls._ocr_hits = real_ocr_hits
    ls._find_window = real_find_window


# --------------------------------------------------------------------------- #
# ⑦ 配置一致性：界面覆盖块与流水线默认值必须字段对齐
#
# 这一条守的是本项目最容易踩的坑：pipeline_override 对对象是**整体替换**，
# 少写一个字段就等于把它从原 pipeline 里抹掉（表现为「读数突然读不到了」
# 或「锚点丢了乱认」）。所以界面里那份 custom_recognition_param 必须和
# my_task.json 里的默认值字段集合完全一致，命名也必须对得上。
# --------------------------------------------------------------------------- #
print()
print("=" * 78)
print("⑦ 配置一致性：interface.json 的覆盖块 vs my_task.json 的默认值")
print("=" * 78)


def strip_jsonc(raw: str) -> str:
    """去掉 // 行注释（不动字符串里的内容），好让 json 模块能解析。"""
    out, in_str, esc, i = [], False, False, 0
    while i < len(raw):
        ch = raw[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            out.append(ch)
        else:
            if ch == '"':
                in_str = True
                out.append(ch)
            elif ch == "/" and i + 1 < len(raw) and raw[i + 1] == "/":
                while i < len(raw) and raw[i] != "\n":
                    i += 1
                continue
            else:
                out.append(ch)
        i += 1
    return "".join(out)


interface = json.loads(
    strip_jsonc((WORKSPACE / "assets" / "interface.json").read_text(encoding="utf-8"))
)
pipeline = json.loads(
    strip_jsonc(
        (WORKSPACE / "assets" / "resource" / "pipeline" / "my_task.json").read_text(
            encoding="utf-8"
        )
    )
)

override = (interface["option"]["读数采样项"].get("pipeline_override") or {}).get(
    "LaserSweepReadValueB"
)
check("界面覆盖了读数节点（否则采样开关在界面上改了也不生效）", isinstance(override, dict),
      str(type(override)))

if isinstance(override, dict):
    ov_param = override.get("custom_recognition_param") or {}
    base_param = pipeline["LaserSweepReadValueB"]["custom_recognition_param"]
    missing = sorted(set(base_param) - set(ov_param))
    extra = sorted(set(ov_param) - set(base_param))
    check("界面覆盖块没漏字段（漏一个就等于把它从 pipeline 里抹掉）", missing == [], str(missing))
    check("界面覆盖块没有拼错的字段名", extra == [], str(extra))
    check("采样开关由界面提供", ov_param.get("sample_enabled") == "{读数采样}",
          str(ov_param.get("sample_enabled")))
    check("采样时长由界面提供", ov_param.get("sample_window_sec") == "{采样时长}",
          str(ov_param.get("sample_window_sec")))
    # 界面没提供的必须保持和 pipeline 默认值一致，否则两处各说各话
    drift = {
        k: (ov_param[k], base_param[k])
        for k in base_param
        if k in ov_param and "{读数采样}" not in str(ov_param[k])
        and "{采样时长}" not in str(ov_param[k])
        and ov_param[k] != base_param[k]
    }
    check("其余常量字段与 pipeline 默认值一致（两处不能各说各话）", drift == {}, str(drift))

    inputs = {i["name"] for i in interface["option"]["读数采样项"]["inputs"]}
    check("「读数采样」在界面上有对应输入项", "读数采样" in inputs, str(sorted(inputs)))
    check("「采样时长」在界面上有对应输入项", "采样时长" in inputs, str(sorted(inputs)))
    _sample_parent = interface["option"]["读数采样"]
    check("「读数采样」是折叠分组（checkbox + 展开设置按钮）",
          _sample_parent.get("type") == "checkbox"
          and any("读数采样项" in (c.get("option") or []) for c in _sample_parent.get("cases", [])),
          str(_sample_parent.get("type")))

# --------------------------------------------------------------------------- #
# ⑦b 一个节点只许被一处 override 覆盖
#
# 这条是 2026-10-27 那次**严重事故**换来的，代价最大：
#   为了让告警邮件拿到 SMTP 配置，我在「邮件设置」里也写了一份
#   LaserSweepStart 的 override（只含 smtp_*）。而 pipeline_override 对
#   custom_action_param 是**整体替换**，两个 option 都挂在这个任务上时，
#   后应用的那份把 start/end/step/count **整个顶掉** ——
#   用户界面填 30→42，agent 收到的是代码默认 20→30，怎么调都从 20 开始扫。
#
#   我当时还在注释里写了"两处是合并的、互不覆盖"，那句话是错的。
#   所以这条断言必须存在：它能在**离线**就抓住这类配置冲突，
#   而不是等到用户跑一整轮、发现数据全错。
# --------------------------------------------------------------------------- #
print()
print("⑦b 一个节点只许被一处 override 覆盖（2026-10-27 事故）")
print("=" * 78)

_node_owners: dict[str, list[str]] = {}
for _opt_name, _opt in interface["option"].items():
    for _node in (_opt.get("pipeline_override") or {}):
        _node_owners.setdefault(_node, []).append(_opt_name)

_conflicts = {n: o for n, o in _node_owners.items() if len(o) > 1}
check("没有任何节点被两个 option 同时覆盖（否则字段会被整体顶掉）",
      _conflicts == {}, str(_conflicts))

# LaserSweepStart 必须完整带上扫描参数 —— 少一个就等于把它抹成代码默认值
_start_ov = (interface["option"]["扫描设置项"].get("pipeline_override") or {}).get(
    "LaserSweepStart", {}
).get("custom_action_param", {})
_scan_fields = ("start", "end", "step", "count", "wait_sec",
                "csv_dir", "csv_name", "open_when_done", "csv_header")
_missing_scan = [k for k in _scan_fields if k not in _start_ov]
check("扫描参数（起止/步长/点数…）都在 LaserSweepStart 的覆盖块里",
      _missing_scan == [], str(_missing_scan))
check("起始值确实来自界面占位符（不是硬编码 20）",
      _start_ov.get("start") == "{起始值}", str(_start_ov.get("start")))
check("结束值确实来自界面占位符", _start_ov.get("end") == "{结束值}", str(_start_ov.get("end")))
check("步长确实来自界面占位符", _start_ov.get("step") == "{步长}", str(_start_ov.get("step")))
check("扫描点数确实来自界面占位符", _start_ov.get("count") == "{扫描点数}",
      str(_start_ov.get("count")))

# 2026-10-28：主任务不再写任何邮件字段，邮件配置只在「邮件通知」任务里维护。
_mail_fields = ("smtp_host", "smtp_user", "smtp_password", "mail_to",
                "notify_mode", "mail_subject_prefix")
_leaked_mail = [k for k in _mail_fields if k in _start_ov]
check("LaserSweepStart 覆盖块里没有邮件字段（邮件设置只在「邮件通知」任务里填）",
      _leaked_mail == [], str(_leaked_mail))

# 主任务只挂三个扫描相关 option，不再出现邮件输入框。
_scan_task = next(t for t in interface["task"] if t["name"] == "激光参数扫描")
_scan_opts = _scan_task.get("option") or []
check("扫描任务挂的是「扫描设置 / 读数校验 / 读数采样」",
      _scan_opts == ["扫描设置", "读数校验", "读数采样"], str(_scan_opts))
check("扫描任务没有「邮件设置」", "邮件设置" not in _scan_opts, str(_scan_opts))
_scan_parent = interface["option"]["扫描设置"]
check("「扫描设置」是折叠分组（checkbox + 展开设置按钮）",
      _scan_parent.get("type") == "checkbox"
      and any("扫描设置项" in (c.get("option") or []) for c in _scan_parent.get("cases", [])),
      str(_scan_parent.get("type")))
_check_parent = interface["option"]["读数校验"]
check("「读数校验」是折叠分组（checkbox + 展开设置按钮）",
      _check_parent.get("type") == "checkbox"
      and any("读数校验项" in (c.get("option") or []) for c in _check_parent.get("cases", [])),
      str(_check_parent.get("type")))

# 邮件信息只保留在「邮件通知」任务里；「邮件测试」只是按钮，不重复存一份配置。
_mail_task = next(t for t in interface["task"] if t["name"] == "邮件通知")
_mail_test_task = next(t for t in interface["task"] if t["name"] == "邮件测试")
check("「邮件通知」任务挂「邮件设置」（唯一填写入口）",
      (_mail_task.get("option") or []) == ["邮件设置"], str(_mail_task.get("option")))
check("「邮件测试」任务不挂 option（按钮只读邮件通知的配置）",
      not _mail_test_task.get("option"), str(_mail_test_task.get("option")))

# 读数校验的 Guard 覆盖块必须把界面输入项都带上，且不含 smtp 字段。
_guard_ov = (interface["option"]["读数校验项"].get("pipeline_override") or {}).get(
    "LaserSweepGuard", {}
).get("custom_action_param", {})
_guard_fields = ("enabled", "tolerance_abs", "tolerance_pct", "retry_wait", "retry_max",
                 "notify_enabled", "continue_on_abnormal")
_missing_guard = [k for k in _guard_fields if k not in _guard_ov]
check("读数校验的 Guard 覆盖块字段齐全", _missing_guard == [], str(_missing_guard))
check("读数校验的 Guard 覆盖块没有 smtp 字段",
      not any(k.startswith("smtp_") or k == "mail_to" for k in _guard_ov),
      str([k for k in _guard_ov if k.startswith("smtp_") or k == "mail_to"]))

# 采样默认值本身也要能过一遍解析，别写出一个认不出来的值
guard_like = ls._flag(pipeline["LaserSweepReadValueB"]["custom_recognition_param"].get("sample_enabled"), True)
check("pipeline 里的 sample_enabled 解析成 True（默认开启）", guard_like is True, str(guard_like))
check("pipeline 里的采样窗默认是 1 秒",
      ls._number(pipeline["LaserSweepReadValueB"]["custom_recognition_param"].get("sample_window_sec"),
                 ls.DEFAULT_SAMPLE_WINDOW_SEC) == 1.0)
check("代码默认值与 pipeline 默认值一致",
      ls.DEFAULT_SAMPLE_WINDOW_SEC == 1.0
      and ls.DEFAULT_SAMPLE_MAX_COUNT == pipeline["LaserSweepReadValueB"]["custom_recognition_param"]["sample_max_count"]
      and ls.DEFAULT_SAMPLE_OUTLIER_ABS == pipeline["LaserSweepReadValueB"]["custom_recognition_param"]["sample_outlier_abs"],
      f"{ls.DEFAULT_SAMPLE_WINDOW_SEC} / {ls.DEFAULT_SAMPLE_MAX_COUNT} / {ls.DEFAULT_SAMPLE_OUTLIER_ABS}")





# --------------------------------------------------------------------------- #
# ⑥ 截图链路（2026-10-27 重写）：24bpp、缓冲区复用、只渲染数值框
#
# 为什么要专门测这一层：读数快路径现在是「每帧只渲染数值框那一小块」，
# 一旦这里判错，表现是**读不到数**（而不是慢），而"读不到"会被当成识别失败
# 写进空值占位 —— 整轮数据就废了。所以代码里几个"拿不准就退回稳妥路径"的
# 兜底必须逐条钉住。
# --------------------------------------------------------------------------- #
print()
print("=" * 78)
print("⑥ 截图链路：24bpp 零拷贝、缓冲区复用、区域裁剪的边界")
print("=" * 78)

# 行跨度：24bpp 每行按 4 字节对齐。1440 宽恰好整除 → 才能零拷贝。
check("行跨度 1440x3 = 4320（4 的倍数，可零拷贝）", ls._row_stride(1440, 3) == 4320,
      str(ls._row_stride(1440, 3)))
check("行跨度 1920x3 = 5760（4 的倍数）", ls._row_stride(1920, 3) == 5760)
check("行跨度 1441x3 = 4324（补 1 字节，走 copy 兜底）", ls._row_stride(1441, 3) == 4324,
      str(ls._row_stride(1441, 3)))
check("行跨度 1024x3 = 3072（4 的倍数）", ls._row_stride(1024, 3) == 3072)
# 常见分辨率都不该有填充 —— 否则"零拷贝"对那台机器就是空话。
# ⚠️ 1366 宽是反例：1366*3 = 4098，不是 4 的倍数，必须补 2 字节
#    （1366x768 是很常见的笔记本分辨率，这条断言就是当初差点漏掉的）。
PAD_CASE = [w for w in (1280, 1366, 1440, 1600, 1920, 2560, 3840) if ls._row_stride(w, 3) != w * 3]
check("只有 1366 这类宽度会产生行填充（其余可零拷贝）", PAD_CASE == [1366], str(PAD_CASE))
check("有填充时兜底分支仍然正确（1366 → 4100）", ls._row_stride(1366, 3) == 4100,
      str(ls._row_stride(1366, 3)))

# 缓冲区复用：只增不减，别在尺寸抖动时反复分配
ls._cap_buffer.update(bpp=0, bytes=0, buf=None)
b1 = ls._reuse_buffer(4000, 24)
b2 = ls._reuse_buffer(2000, 24)  # 变小 → 仍复用同一块
b3 = ls._reuse_buffer(8000, 24)  # 变大 → 重建
b4 = ls._reuse_buffer(6000, 24)  # 变小 → 复用大的那块
check("缓冲区变小仍复用同一块（不重复分配）", b1 is b2, "同一块" if b1 is b2 else "重建了")
check("缓冲区不够大时才重建", b3 is not b1)
check("重建后变小仍然复用（只增不减）", b4 is b3)
check("位深变化会重建（24/32bpp 不能混用）",
      ls._reuse_buffer(100, 32) is not b4, "已按位深区分")

# 纯色帧检测：只渲染区域的前提是窗口响应裁剪；不响应就得到纯色
import numpy as _np

check("纯黑帧判为空", ls._frame_looks_empty(_np.zeros((30, 80, 3), dtype=_np.uint8)) is True)
check("纯白帧判为空（不能只判黑）",
      ls._frame_looks_empty(_np.full((30, 80, 3), 255, dtype=_np.uint8)) is True)
_two = _np.zeros((30, 80, 3), dtype=_np.uint8)
_two[3, 7] = 11
check("有内容判为非空", ls._frame_looks_empty(_two) is False)
check("空数组判为空（不抛异常）",
      ls._frame_looks_empty(_np.zeros((0, 0, 3), dtype=_np.uint8)) is True)

# 区域裁剪：ROI 必须被夹进窗口内，否则 GDI 会静默画出奇怪结果
check("区域 -> 有效", ls._pad_box((100, 200, 60, 20), 0, (1440, 829)) == [100, 200, 60, 20])
check("区域为 0 宽高时拒绝", ls._pad_box((100, 200, 0, 20), 0, (1440, 829)) is None)

# ── region=None（"整张图就是要认的那块"）不得崩 ────────────────────────────
# 2026-10-27 实机事故的根因就在这里：
#   _read_region 里取"离原框最近的候选"时对 None 做了 tuple(region)，
#   抛 TypeError；而异常被 _sample_loop 的 except 吞成"这次跳过"，
#   结果 16 次采样全部静默失败，日志却显示「每秒 16.5 次」、样本只有 1 个。
#   用户拿到的 CSV 每行都是单点值，他以为做了 1 秒平均。
print()
print("  region=None 路径（整图即目标区域）")
_region_calls = []


def _fake_ocr_for_region(context, image, node, wanted, roi, only_rec):
    _region_calls.append(roi)
    return [((10, 5, 100, 24), "-79.72dBm")]


_real_ocr_for_region = ls._ocr_hits
ls._ocr_hits = _fake_ocr_for_region
try:
    _img = FakeImage((148, 40))
    _one, _diag = ls._read_region(
        FakeContext(FakeController([_img])),
        {"pattern": r"[-+]?\d+(?:\.\d+)?", "value_max_chars": 24,
         "min_digit_ratio": 0.3, "sample_roi_pad": 8},
        _img,
        None,  # ← 关键：整图即区域
    )
    check("region=None 不抛异常", True)
    check("region=None 能认到数值", _one is not None and _one.value == -79.72,
          str(None if _one is None else _one.value))
    check("region=None 时 roi 覆盖整张小图（192x40 那种越界不该出现）",
          bool(_region_calls) and _region_calls[-1] == [0, 0, 148, 40], str(_region_calls[-1:]))
finally:
    ls._ocr_hits = _real_ocr_for_region

# 采样快路径：reader 必须拿「只渲数值框」的小图去认，且只认一次（不再整屏检测）
print()
print("  采样快路径：每帧只渲染数值框（不是整窗）")

# 这是「读数节点真正会拿到的那一套参数」：必须有 mode=ocr，
# 否则 LaserReadValueB.analyze 会走进 fixed 分支直接返回占位值（0），
# 根本不会去截图 —— 那样这一节的断言全是空转。
REGION_PARAM = {
    "mode": "ocr",
    "window_regex": "^(FakeB)$",
    "anchor_text": ["功率CH1"],
    "roi": [0, 0, 0, 0],
    "prefer": "auto",
    "pattern": r"[-+]?\d+(?:\.\d+)?",
    "value_max_chars": 24,
    "min_digit_ratio": 0.3,
    "anchor_max_chars": 16,
    "ocr_node": "LaserSweepOcrValueB",
    "only_rec": False,
    "sample_enabled": True,
    "sample_window_sec": 0.3,
    "sample_max_count": 10,
    "sample_slow_sec": 5,
    "sample_roi_pad": 8,
    "sample_ocr_node": "LaserSweepOcrValueB",
    "sample_outlier_abs": 3.0,
    "sample_outlier_pct": 1,
}

_real_screencap = ls._screencap_window
_regions_seen = []


def fake_screencap(hwnd, region=None):
    _regions_seen.append(region)
    if region is None:
        return _np.zeros((829, 1440, 3), dtype=_np.uint8)
    w, h = region[2], region[3]
    return _np.zeros((h, w, 3), dtype=_np.uint8)


ls._screencap_window = fake_screencap
_ocr_rois = []


def fake_ocr(context, image, node, wanted, roi, only_rec):
    _ocr_rois.append((roi, image.shape[:2], only_rec))
    h, w = image.shape[:2]
    # 整窗（大图）→ 返回「锚点 + 数值」，让定位逻辑能建立数值框
    # 小图（= 只渲了数值框）→ 只返回一个带抖动的数值
    if w > 800 and h > 500 and not only_rec:
        return [
            ((100, 290, 90, 25), "功率CH1"),
            ((200, 300, 130, 24), "-79.72dBm"),
            ((100, 420, 200, 20), "code=-1073807265, VISA Write in MY TSL-510_Initialize.vi"),
        ]
    jitter = (len(_ocr_rois) % 4) * 0.02
    return [((10, 5, 100, 24), f"{-79.72 - jitter:.3f}dBm")]


_real_ocr2 = ls._ocr_hits
ls._ocr_hits = fake_ocr
_real_find2 = ls._find_window
ls._find_window = lambda param: (12345, "FakeB", "Class")

try:
    reset_state()
    ctx = FakeContext(FakeController([FakeImage((1440, 829))]))
    _regions_seen.clear()
    _ocr_rois.clear()
    res = ls.LaserReadValueB().analyze(ctx, FakeAnalyzeArg(dict(REGION_PARAM)))
    d = res.detail or {}
    check("区域采样路径读到了数", d.get("measured") is not None, str(d.get("error")))

    # 第一次定位必须整窗（region=None），之后每帧都必须只抓数值框
    small = [r for r in _regions_seen if r is not None]
    check("第一帧是整窗渲染（用于定位）", _regions_seen and _regions_seen[0] is None,
          str(_regions_seen[:2]))
    check("后续每帧都只渲染数值框那一小块", len(small) >= 1, f"{len(small)} 次区域抓取")
    if small:
        rw, rh = small[0][2], small[0][3]
        check("区域尺寸远小于整窗（这才是省时间的来源）",
              rw * rh < 1440 * 829 * 0.05, f"{rw}x{rh} vs 1440x829")

    # 只认小块时 roi 必须是"整张小图"，不该再拿窗口坐标去框（坐标系已经变了）
    last_roi, last_shape, last_only = _ocr_rois[-1]
    th, tw = last_shape
    check("只认小块时 roi 覆盖整张小图（坐标系已改为图内坐标）",
          last_roi[2] <= tw and last_roi[3] <= th, f"roi={last_roi} 图={tw}x{th}")
    check("采样走 only_rec（跳过文本检测，这是快的关键）", last_only is True, str(last_only))

    # ── 缓存失效后的重定位：必须先试小区域，不能立刻整屏 OCR ──────────────
    # 整屏 OCR 5.5 秒是整条链路最贵的一步；窗口没动时控件还在原处，
    # 认一小块只要零点几秒。这条断言防的是"哪天把 hint 优化改没了"。
    print()
    print("  缓存失效重定位：先试小区域，认不到才整屏")
    reset_state()
    ls._state["value_box"] = {
        "box": [200, 300, 130, 24],
        "layout": "right",
        "anchor": "功率CH1",
        "size": (1440, 829),
        "signature": "different-on-purpose",  # 故意让缓存失效，强制走重定位
        "fast": True,
    }
    _regions_seen.clear()
    _ocr_rois.clear()
    ctx_hint = FakeContext(FakeController([FakeImage((1440, 829))]))
    res_hint = ls.LaserReadValueB().analyze(ctx_hint, FakeAnalyzeArg(dict(REGION_PARAM)))
    d_hint = res_hint.detail or {}
    check("缓存失效后仍能读出数", d_hint.get("measured") is not None, str(d_hint.get("error")))
    check("重定位时确实先用了小区域（标注 hinted）", d_hint.get("hinted") is True,
          f"hinted={d_hint.get('hinted')}")
    # hint 命中之后不应该再有整屏尺寸的 OCR。
    # ⚠️ 头一次 1440x829 是进入节点时框架给的 argv.image（那时还没走 hint），
    #    所以这里判的是「hint 那条路径之后」，不是"全程一次大图都没有"。
    hint_idx = next(
        (i for i, (_r, shape, _o) in enumerate(_ocr_rois) if shape[1] < 800), None
    )
    check("hint 路径走的是小图（<800px 宽）", hint_idx is not None,
          str([s for _r, s, _o in _ocr_rois][:4]))
    if hint_idx is not None:
        after = [shape for _r, shape, _o in _ocr_rois[hint_idx:]]
        check("hint 命中后不再有整屏 OCR",
              all(w < 800 for h, w in after), str(after[:4]))
    # ⚠️ 2026-10-27 审查修正：hint 命中**不能**沿用旧坐标。
    #
    #    旧断言是「hint 命中后数值框仍是原来的框」，那正是被审查指出的缺陷：
    #    hint 命中只证明"这个区域里有个数字"，不证明"它还在老位置"。
    #    窗口尺寸变化导致缓存失效时布局往往也变了，沿用旧框会让整轮采样认错位置。
    #
    #    现在的行为：用 OCR 实际返回的框（图内坐标）换算回客户区坐标。
    #    这个用例里 fake_ocr 固定返回 (10,5,100,24)，hint=[0,60,570,504]，
    #    所以新框 = [10, 65, 100, 24] —— 断言这个，才是验证"用了实际位置"。
    _hint = ls._pad_box([200, 300, 130, 24], 240, (1440, 829))
    _expect_box = [10 + _hint[0], 5 + _hint[1], 100, 24]
    check("hint 命中后用 OCR 返回的实际位置，而不是沿用旧坐标",
          (d_hint.get("value_box") or [])[:4] == _expect_box,
          f"得到 {d_hint.get('value_box')}，期望 {_expect_box}")
finally:
    ls._screencap_window = _real_screencap
    ls._ocr_hits = _real_ocr2
    ls._find_window = _real_find2


print()
print("=" * 78)
print(f"结果：{'全部通过 🎉' if failures == 0 else f'{failures} 项失败'}")
print("=" * 78)
sys.exit(1 if failures else 0)
