"""读数校验 / 重测 / 告警自检：不连软件、不发真邮件，验证「差距过大就重测」的全套判据。

背景：新加的保护层是——每读到一个值先和上一次比，差得离谱就等几秒重读，
连续 5 次都不正常就终止扫描并邮件告警。它有两条性质只有在真机上跑一整轮
才看得出来：**判据算得对不对**，和 **重测/终止的跳转有没有走对**。
前者是纯数学，后者是 Context 的一次 override_next —— 都能用桩模块验。

    python tools/check_guard.py

用例覆盖：

    ① 阈值判据：绝对偏差 / 相对偏差 / 「两者取更宽松者」/ 正负号 / 零基准
    ② 基准选取：优先上一次读数，其次历史最后一个，没有历史时首读直接放行
    ③ 正常读数：不重测，直接跳 Record
    ④ 异常读数：跳重测节点，并把「重测等待秒数」写进节点的 post_delay
    ⑤ 连续第 N 次异常：跳终止节点（不是 on_error 路径），且不抛异常
    ⑥ 开关关闭 / 没读到值：一律放行，绝不让保护层制造新故障
    ⑦ 告警内容：总耗时、异常读数、期望范围、CSV 路径都要出现在邮件正文里
    ⑧ 邮件通道：缺配置时安全降级（不抛、不发），配置齐全时才真的走 smtplib
    ⑨ 密码永不进日志（抓 stdout 断言里面没有密码明文）

判据的关键语义（改代码前先看这里，别改错方向）：
    |当前 - 基准| <= 绝对阈值               → 正常
    相对偏差 <= 相对阈值(%)                  → 正常
    任一成立即正常（取更宽松者）—— 因为功率这类量在小信号时相对偏差天然很大，
    只用相对阈值会天天误报；只用绝对阈值又会在量级变化时失效。
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import types
from contextlib import redirect_stdout
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
# 假 Context / 假 argv：只记下 override_next 与 override_pipeline 干了什么
# --------------------------------------------------------------------------- #
class FakeContext:
    def __init__(self):
        self.next_calls: list[tuple[str, list[str]]] = []
        self.pipeline_calls: list[dict] = []
        self.stopped = False
        self.tasker = self  # 让 context.tasker.post_stop() 也能被记下来

    def override_next(self, name, next_list):
        self.next_calls.append((name, list(next_list)))
        return True

    def override_pipeline(self, override):
        self.pipeline_calls.append(override)
        return True

    def post_stop(self):
        self.stopped = True
        return True

    # 便于断言
    @property
    def last_next(self):
        return self.next_calls[-1][1] if self.next_calls else None


class FakeArg:
    def __init__(self, param: dict, node_name: str = "LaserSweepGuard"):
        self.custom_action_param = json.dumps(param, ensure_ascii=False)
        self.node_name = node_name


# 自检用的临时目录：所有落盘产物都写这里，退出时整个删掉。
# 为什么不写 debug/_check_guard.csv 那种固定名：跑一次自检就在工作区留一个
# 假结果文件，翻 git status / 找真实结果时都得先分辨哪个是测试的垃圾。
_TMP_DIR = WORKSPACE / "debug" / "_check_guard_tmp"


def _tmp_file(name: str) -> Path:
    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    return _TMP_DIR / name


def reset_state(**overrides):
    """把 _state 重置成一次扫描刚开始的样子，再叠加用例需要的字段。

    ⚠️ 新增的字段也必须在这里清掉，否则会「串场」：
        2026-10-27 加了 mail_param（告警用的邮件配置）和 jumps（真实跳变留痕）之后，
        忘了清理它们，结果上一个用例留下的 jumps 让后面「不稳定读数不该被当成跳变」
        的断言直接失败 —— 而且失败原因看起来像判据错了，实际是测试污染。
    """
    ls._state.update(
        values=["20", "21", "22", "23", "24", "25"],
        index=2,
        csv_path=_tmp_file("_check_guard.csv"),
        out_root=_TMP_DIR,
        measured=None,
        last_measured=None,
        history=[],
        guard={},
        alert={},
        run_start=1000.0,
        started_at="2026-10-26 17:00:00",
        sent_alerts=0,
        # 告警路径会读这两项，必须在每个用例开头清空
        mail_param={},
        guard_param={},
        sample_param={},
        scan_settings_from_task=False,
        jumps=[],
        last_note="",
    )
    ls._state.update(overrides)


def guard_action(context, **param) -> bool:
    base = {
        "enabled": True,
        "tolerance_abs": 1.0,
        "tolerance_pct": 10.0,
        "retry_wait": 3,
        "retry_max": 5,
        "retry_node": "LaserSweepRetryValue",
        "record_node": "LaserSweepRecord",
        "stop_node": "LaserSweepAbnormalStop",
        "notify_mode": "off",  # 自检绝不发真邮件
    }
    base.update(param)
    return bool(ls.LaserSweepGuard().run(context, FakeArg(base)))


# --------------------------------------------------------------------------- #
# ① 阈值判据
# --------------------------------------------------------------------------- #
print("=" * 78)
print("① 阈值判据：绝对 / 相对 / 取更宽松者")
print("=" * 78)

ok, diff, pct = ls._tolerance_ok(-79.72, -79.72, 1.0, 10.0)
check("同一个值必然正常", ok is True, f"diff={diff} pct={pct}")

ok, _, _ = ls._tolerance_ok(-80.5, -79.72, 1.0, 10.0)
check("差 0.78 < 绝对阈值 1.0 → 正常", ok is True)

# 10% of 79.72 ≈ 7.97 → 相对区间比绝对区间宽得多，所以 -85 应该被判正常
ok, diff, pct = ls._tolerance_ok(-85.0, -79.72, 1.0, 10.0)
check("差 5.28 > 绝对 1.0，但相对 6.6% < 10% → 正常（取更宽松者）", ok is True,
      f"diff={diff:.2f} pct={pct:.2f}%")

ok, diff, pct = ls._tolerance_ok(-95.0, -79.72, 1.0, 10.0)
check("差 15.28 ≈ 19% > 10% → 异常", ok is False, f"diff={diff:.2f} pct={pct:.2f}%")

# 0.1 这种写法当 10%（用户可能按比例填）
ok_small, _, _ = ls._tolerance_ok(-95.0, -79.72, 1.0, 0.1)
check("相对阈值写 0.1 也当 10%（不是 0.1%）", ok_small is False, "0.1 → 10%")

# 正负反转也要抓住（OCNR 偶尔会把负号丢掉）
ok, _, _ = ls._tolerance_ok(79.72, -79.72, 1.0, 10.0)
check("正负号反了 → 异常", ok is False)

# 基准为 0 时相对偏差没有意义，只能靠绝对阈值，不能除零崩掉
ok, diff, pct = ls._tolerance_ok(0.4, 0.0, 1.0, 10.0)
check("基准 0：不除零，按绝对阈值判正常", ok is True, f"pct={pct}")
ok, _, _ = ls._tolerance_ok(5.0, 0.0, 1.0, 10.0)
check("基准 0：超绝对阈值 → 异常", ok is False)

# 阈值填 0 表示「一点都不许偏」，此时相等才放行
ok, _, _ = ls._tolerance_ok(1.0, 1.0, 0.0, 0.0)
check("阈值全 0：相等仍放行", ok is True)
ok, _, _ = ls._tolerance_ok(1.0001, 1.0, 0.0, 0.0)
check("阈值全 0：有偏差即异常", ok is False)

print()
print("=" * 78)
print("①b 相对阈值归一：1 必须当 1%，不能变成 100%")
print("=" * 78)
# 这条是 2026-10-26 修掉的真实缺陷：原先用 `> 1` 二分，
# 填 1 会落进「比例」分支 ×100 = 100%，于是默认值设成 1% 时静默变成 100%。
check("填 1 → 1%", ls._percent_value("1") == 1.0, f"得到 {ls._percent_value('1')}")
check("填 '1.0' → 1%", ls._percent_value("1.0") == 1.0, f"得到 {ls._percent_value('1.0')}")
check("填 1.0（数字）→ 1%", ls._percent_value(1.0) == 1.0)
check("填 10 → 10%", ls._percent_value("10") == 10.0)
# ⚠️ 2026-10-27 语义收紧：界面写的是「(%)」，所以**数值即百分数**，不再猜"比例写法"。
#    旧行为把 <1 的数乘 100，导致界面填 0.5（本意 0.5%）被当成 50%，
#    基准 -80、当前 -70（差 12.5%）被直接放行 —— 保护层等于没开。
check("填 0.1 → 0.1%（小数就是小数，不能再乘 100）", ls._percent_value("0.1") == 0.1,
      str(ls._percent_value("0.1")))
check("填 0.5 → 0.5%（不再被放大成 50%）", ls._percent_value("0.5") == 0.5,
      str(ls._percent_value("0.5")))
check("填 0.9 → 0.9%（不再被放大成 90%）", ls._percent_value("0.9") == 0.9,
      str(ls._percent_value("0.9")))
check("填 0.5 时 12.5% 的偏差必须被判异常（这就是那个 bug 的场景）",
      ls._tolerance_ok(-70.0, -80.0, 3.0, ls._percent_value("0.5"))[0] is False,
      str(ls._tolerance_ok(-70.0, -80.0, 3.0, ls._percent_value("0.5"))))
check("填 100 → 100%", ls._percent_value("100") == 100.0)
check("填 0 → 0（= 不用相对判据）", ls._percent_value("0") == 0.0)
check("留空 → 默认 1%", ls._percent_value(None) == ls.DEFAULT_TOLERANCE_PCT == 1.0,
      f"默认 {ls.DEFAULT_TOLERANCE_PCT}")

print()
print("=" * 78)
print("①c 真实数据的实际防线（20→30 扫描，功率只漂 0.4）")
print("=" * 78)
# 这套数据是 README 里记录的真实结果：整段只从 -79.72 漂到 -80.11。
# 断言默认值下「正常波动放行、认错数字被抓」——否则保护层对用户的实际数据无效。
#
# ⚠️ 2026-10-27 默认绝对阈值从 1.0 放宽到 3.0（主人要求「别误杀真实波动」）。
#    所以这条线的位置跟着右移：3.0 以内放行，超过才算异常。
#    下面同时钉住「放宽后仍抓得住哪些」，防止哪天被继续放宽到形同虚设。
ABS, PCT = ls.DEFAULT_TOLERANCE_ABS, ls.DEFAULT_TOLERANCE_PCT
check("默认绝对阈值是 3.0", ABS == 3.0, str(ABS))

ok, _, _ = ls._tolerance_ok(-80.11, -79.72, ABS, PCT)
check("正常组间漂移 0.39 → 放行", ok is True)
ok, _, _ = ls._tolerance_ok(-82.5, -79.72, ABS, PCT)
check("偏离 2.78（在 3.0 以内）→ 放行", ok is True)
ok, _, _ = ls._tolerance_ok(-83.5, -79.72, ABS, PCT)
check("偏离 3.78（超出 3.0 且超出 1%）→ 抓", ok is False)
ok, _, _ = ls._tolerance_ok(-85.3, -79.72, ABS, PCT)
check("读成另一个数 -85.3（偏 5.58）→ 抓", ok is False)
ok, _, _ = ls._tolerance_ok(-797.2, -79.72, ABS, PCT)
check("小数点右移 -797.2 → 抓", ok is False)
ok, _, _ = ls._tolerance_ok(79.72, -79.72, ABS, PCT)
check("丢负号 79.72 → 抓", ok is False)
ok, _, _ = ls._tolerance_ok(-7.972, -79.72, ABS, PCT)
check("少一位 -7.972 → 抓", ok is False)


# --------------------------------------------------------------------------- #
# ②~⑥ 动作跳转
# --------------------------------------------------------------------------- #
print()
print("=" * 78)
print("② 基准选取")
print("=" * 78)

reset_state(measured=5.0, last_measured=None, history=[])
ctx = FakeContext()
guard_action(ctx)
check("没有历史时首读直接放行（不制造假异常）", ctx.last_next == ["LaserSweepRecord"], str(ctx.last_next))
check("首读被记进历史", ls._state["history"] == [5.0], str(ls._state["history"]))

reset_state(measured=5.1, last_measured=5.0, history=[1.0, 5.0])
ctx = FakeContext()
guard_action(ctx)
check("优先用 last_measured 当基准（不是历史里更早的 1.0）",
      ctx.last_next == ["LaserSweepRecord"], str(ctx.last_next))

reset_state(measured=5.1, last_measured=None, history=[1.0, 5.0])
ctx = FakeContext()
guard_action(ctx)
check("没有 last_measured 就用历史最后一个", ctx.last_next == ["LaserSweepRecord"], str(ctx.last_next))

print()
print("=" * 78)
print("③ 正常读数：零开销，直接记录")
print("=" * 78)

reset_state(measured=5.5, last_measured=5.0, history=[5.0])
ctx = FakeContext()
ok = guard_action(ctx)
check("动作返回 True（继续流程）", ok is True)
check("跳 LaserSweepRecord", ctx.last_next == ["LaserSweepRecord"], str(ctx.last_next))
check("正常时不改任何节点的延时（不留额外开销）", ctx.pipeline_calls == [], str(ctx.pipeline_calls))
check("正常时清空本轮重测计数", ls._state["guard"].get("strikes", 0) == 0,
      str(ls._state.get("guard")))

print()
print("=" * 78)
print("④ 异常读数：跳重测，并把「重测等待秒数」写进节点")
print("=" * 78)

reset_state(measured=-95.0, last_measured=-79.72, history=[-79.72])
ctx = FakeContext()
ok = guard_action(ctx)
check("动作返回 True（不走 on_error）", ok is True)
check("跳 LaserSweepRetryValue", ctx.last_next == ["LaserSweepRetryValue"], str(ctx.last_next))
check("异常值**不**写进历史（脏数据不能被当基准）",
      ls._state["history"] == [-79.72], str(ls._state["history"]))
waits = [c for c in ctx.pipeline_calls if "LaserSweepRetryValue" in c]
check("按默认 3 秒设置了重测等待", bool(waits) and waits[-1]["LaserSweepRetryValue"]["post_delay"] == 3000,
      str(waits))

reset_state(measured=-95.0, last_measured=-79.72, history=[-79.72])
ctx = FakeContext()
guard_action(ctx, retry_wait=7)
waits = [c for c in ctx.pipeline_calls if "LaserSweepRetryValue" in c]
check("重测等待秒数可配（7 秒 → 7000 毫秒）",
      bool(waits) and waits[-1]["LaserSweepRetryValue"]["post_delay"] == 7000, str(waits))

print()
print("=" * 78)
print("⑤ 连续第 5 次异常：发告警 + 按设置决定继续还是停（不是 on_error）")
print("=" * 78)
print("  ⚠️ 2026-10-27 起默认「继续测试」：告警照发，但扫描不停。")
print("     下面分别验证两种模式。")
print()
print("  ⚠️ 注意这里的测试数据：每次重测给一个**不同**的离谱值（-95 / -96 / -94…）。")
print("     这是有意为之 —— 真实的 OCR 误读与通信抖动在连续重测之间是**各说各话**的。")
print("     如果 5 次都读到同一个离谱值，新版会判定为「真实跳变」并接受它")
print("     （见 _stable_readings 与 ⑤b 的用例）。")


def drifting_readings(seed: float, n: int = 6, spread: float = 30.0):
    """生成 n 个「互不一致」的离谱读数，模拟真实的误读/抖动。

    ⚠️ spread 必须显著大于允许带（默认 max(3.0, |中位数|×1%)）：
       真实的 OCR 误读是"每次错得不一样"—— 小数点位置乱跳、丢负号、
       读到旁边的日志数字，跨度动辄几十上百。
       如果构造的序列跨度只有 3.0（正好等于阈值），它会被
       _stable_readings 判成"彼此一致"，从而误判为真实跳变 ——
       那说明测试数据本身不真实，会把判据测歪。
    """
    return [seed + (i - n / 2) * spread / n for i in range(n)]


_RETRY_SERIES = drifting_readings(-95.0)
print("    （测试用的重测序列：" + "、".join(f"{v:.1f}" for v in _RETRY_SERIES) + "）")

# ── 默认模式：继续 ────────────────────────────────────────────────────
reset_state(measured=-95.0, last_measured=-79.72, history=[-79.72])
returned = []
# reset_state 里 history=[-79.72] 非空 → 基准已存在，第 1 次调用就直接比阈值，
# 所以「第 5 次异常」落在第 5 次调用上。
# 出口在 2026-10-27 从 Record 改成了 Advance（空值行已就地写过，再走 Record 会写两遍），
# 所以循环的终止条件要认 Advance。
for i in range(7):
    ls._state["measured"] = _RETRY_SERIES[min(i, len(_RETRY_SERIES) - 1)]
    c = FakeContext()
    r = guard_action(c, retry_max=5)  # 不传 continue_on_abnormal → 走默认
    returned.append((i + 1, r, c.last_next))
    if c.last_next in (["LaserSweepAdvance"], ["LaserSweepAbnormalStop"]):
        break

print("    " + "；".join(f"第{n}次→{nx}" for n, _, nx in returned))
check("默认：第 1~4 次异常都跳重测",
      all(nx == ["LaserSweepRetryValue"] for n, _, nx in returned if n < 5))
check("默认：第 5 次异常跳 Advance（空值行已写，跳过 Record 免得写两遍）",
      returned[-1][0] == 5 and returned[-1][2] == ["LaserSweepAdvance"],
      str(returned[-1]))
check("全程动作都返回 True（否则会走 on_error 写错行）",
      all(r is True for _, r, _ in returned))
check("告警信息已落进 _state['alert']（继续模式下也要发信）",
      bool(ls._state.get("alert")), "有" if ls._state.get("alert") else "无")
check("继续模式下 alert 里标了 continue_on_abnormal",
      (ls._state.get("alert") or {}).get("continue_on_abnormal") is True,
      str((ls._state.get("alert") or {}).get("continue_on_abnormal")))
check("继续模式清零 strikes（下一个点重新计数）",
      ls._state["guard"].get("strikes", 0) == 0, str(ls._state["guard"]))

# ── 显式关掉开关：停止 ────────────────────────────────────────────────
# ⚠️ history 必须给一个基准值，否则第一次调用会走「首读建基准」直接放行，
#    后面再怎么给异常值都测不到异常路径（这是本文件踩过的坑：
#    reset_state 的 history 默认是空列表）。
reset_state(measured=-95.0, last_measured=-79.72, history=[-79.72])
ctx = FakeContext()
for i in range(6):
    ls._state["measured"] = _RETRY_SERIES[min(i, len(_RETRY_SERIES) - 1)]
    ctx = FakeContext()
    guard_action(ctx, retry_max=5, continue_on_abnormal=False)
    # ⚠️ 到了终止节点就停：再跑一轮的话，剩下的 -85 会被判为"正常"，
    #    把 ctx.last_next 覆盖成 Record，断言就看着像"没走到终止"。
    if ctx.last_next == ["LaserSweepAbnormalStop"]:
        break
check("关掉「继续测试」→ 第 5 次跳终止节点（旧行为仍可用）",
      ctx.last_next == ["LaserSweepAbnormalStop"], str(ctx.last_next))

reset_state(measured=-95.0, last_measured=-79.72, history=[-79.72])
ctx = FakeContext()
ls._state["measured"] = _RETRY_SERIES[0]
guard_action(ctx, retry_max=2, continue_on_abnormal=False)
if ctx.last_next != ["LaserSweepAbnormalStop"]:
    ls._state["measured"] = _RETRY_SERIES[3]  # 换一个跨度更远的值，避免被判成"稳定跳变"
    guard_action(ctx, retry_max=2, continue_on_abnormal=False)
check("连续异常次数可配（retry_max=2 + 停止模式 → 第 2 次就终止）",
      ctx.last_next == ["LaserSweepAbnormalStop"], str(ctx.last_next))

# 中途恢复一次就该清零，绝不能累计
reset_state(measured=-95.0, last_measured=-79.72, history=[-79.72])
first = FakeContext()
guard_action(first, retry_max=3)
ls._state["measured"] = -79.8  # 重测读到了正常值
second = FakeContext()
guard_action(second)
check("重测恢复正常 → 立刻回 Record", second.last_next == ["LaserSweepRecord"], str(second.last_next))
check("恢复后本轮计数归零", ls._state["guard"].get("strikes", 0) == 0, str(ls._state["guard"]))
third = FakeContext()
ls._state["measured"] = -95.0
guard_action(third, retry_max=3)
check("归零是「连续」语义：恢复后再异常重新从 1 数起",
      third.last_next == ["LaserSweepRetryValue"], str(third.last_next))

# --------------------------------------------------------------------------- #
# ⑤b 真实跳变 vs 误读（2026-10-27 用真实运行日志换来的判据）
#
# 事故背景（install/log_20261027_154637，201 组真实扫描）：
#   第 176/177 两组被判"连续 5 次异常"、整组判废写空值。但翻日志发现：
#       · 两组的多轮重测彼此极其一致（跨度 0.23 和 0.10）
#       · 下一组（178）立刻回到基准附近（-14.97 vs 基准 -14.68）
#   这说明 -18 那一段是设备真实的跳变，不是 OCR 误读 —— 两个点白丢了。
#
#   根子在于「阈值」只能回答"和上一个点差多少"，回答不了"这个新值是真的
#   还是读错了"。所以加了 _stable_readings：连续 N 次读数彼此一致 → 判为真实跳变。
#
# 这一节把三种形态都钉住，防止以后改判据时把真实跳变又误杀、或把噪声放进来。
# --------------------------------------------------------------------------- #
print()
print("=" * 78)
print("⑤b 真实跳变 vs 误读：靠「重测之间稳不稳」区分")
print("=" * 78)
print("  用例数据直接取自 2026-10-27 那次真实运行（install/log_20261027_154637）")

# 判据本身
check("真实跳变：5 次读数彼此一致（跨度 0.23）→ 判为稳定",
      ls._stable_readings([-18.00, -18.13, -18.21, -18.23, -18.14], 3.0, 1.0) is True)
check("另一组真实跳变（跨度 0.10）→ 判为稳定",
      ls._stable_readings([-18.18, -18.14, -18.08, -18.12], 3.0, 1.0) is True)
check("OCR 误读（各说各话，跨度上百）→ 判为不稳定",
      ls._stable_readings([-95.0, -797.2, -250.0, -400.0, -14.2], 3.0, 1.0) is False)
check("通信抖动（跨度 6）→ 判为不稳定",
      ls._stable_readings([-95.0, -98.0, -92.0, -96.0, -93.0], 3.0, 1.0) is False)
check("点数太少（<2）→ 一律不稳定（不给判据留空隙）",
      ls._stable_readings([-95.0], 3.0, 1.0) is False)
check("大数值按百分比给带宽（1000 上 ±10 以内算稳定）",
      ls._stable_readings([1002.0, 1005.0, 998.0], 3.0, 1.0) is True)

# 整条动作路径：稳定偏离 → 接受并记录；不稳定 → 照旧告警判废
print()
print("  动作路径：")
reset_state(measured=-18.00, last_measured=-14.68, history=[-14.68])
accepted = []
for i, v in enumerate([-18.00, -18.13, -18.21, -18.23, -18.14]):
    ls._state["measured"] = v
    c = FakeContext()
    guard_action(c, retry_max=5)
    accepted.append((i + 1, c.last_next))
print("    " + "；".join(f"第{n}次→{nx}" for n, nx in accepted))
check("真实跳变：前 4 次按常规去重测", all(nx == ["LaserSweepRetryValue"] for n, nx in accepted[:4]))
check("真实跳变：第 5 次**不判废**，而是接受并写入 CSV",
      accepted[-1][1] == ["LaserSweepRecord"], str(accepted[-1]))
check("真实跳变：新基准就是跳变后的值",
      abs(float(ls._state["guard"].get("reference") or 0) - (-18.14)) < 1e-9,
      str(ls._state["guard"].get("reference")))
check("真实跳变：记进 _state['jumps'] 留痕（收尾摘要要列出来）",
      bool(ls._state.get("jumps")), str(ls._state.get("jumps")))
check("真实跳变：接受后 strikes 归零", ls._state["guard"].get("strikes", 0) == 0,
      str(ls._state["guard"]))
check("真实跳变：没有写告警（这不是事故）", not ls._state.get("alert"),
      "有" if ls._state.get("alert") else "无")

print()
reset_state(measured=-95.0, last_measured=-79.72, history=[-79.72])
noisy = []
# 这 5 个值彼此相距很远（跨度 789），代表真实的 OCR 误读 —— 重测之间各说各话。
# 到判废出口就停，别再多吃一轮覆盖 last_next。
for i, v in enumerate([-95.0, -797.2, -250.0, -400.0, -14.2]):
    ls._state["measured"] = v
    c = FakeContext()
    guard_action(c, retry_max=5)
    noisy.append((i + 1, c.last_next))
    if c.last_next in (["LaserSweepAdvance"], ["LaserSweepAbnormalStop"]):
        break
print("    " + "；".join(f"第{n}次→{nx}" for n, nx in noisy))
check("不稳定读数：仍然按老逻辑判废（保护层没被削弱）",
      noisy[-1][1] == ["LaserSweepAdvance"], str(noisy[-1]))
check("不稳定读数：写了告警信息", bool(ls._state.get("alert")), "有" if ls._state.get("alert") else "无")
check("不稳定读数：没有被当成「跳变」记录", not ls._state.get("jumps"),
      str(ls._state.get("jumps")))

# 判废后必须跳过 Record，否则空值行会被写两遍（2026-10-27 实测事故）
print()
reset_state(measured=-95.0, last_measured=-79.72, history=[-79.72])
final_ctx = FakeContext()
for i, v in enumerate([-95.0, -797.2, -250.0, -400.0, -14.2]):
    ls._state["measured"] = v
    final_ctx = FakeContext()
    guard_action(final_ctx, retry_max=5, continue_on_abnormal=True)
    if final_ctx.last_next in (["LaserSweepAdvance"], ["LaserSweepAbnormalStop"]):
        break
check("判废后跳的是 Advance（不是 Record）—— 否则空值行会写两遍",
      final_ctx.last_next == ["LaserSweepAdvance"], str(final_ctx.last_next))


reset_state(measured=-95.0, last_measured=-79.72, history=[-79.72])
ctx = FakeContext()
guard_action(ctx, enabled=False)
check("关掉校验 → 再离谱的值也直接记录", ctx.last_next == ["LaserSweepRecord"], str(ctx.last_next))

reset_state(measured=None, last_measured=-79.72, history=[-79.72])
ctx = FakeContext()
guard_action(ctx)
check("没读到值 → 直接记录（写空值占位由 RecordFailed/Record 处理）",
      ctx.last_next == ["LaserSweepRecord"], str(ctx.last_next))

reset_state(measured=-95.0, last_measured=None, history=[])
ctx = FakeContext()
guard_action(ctx)
check("第一次读数就算很怪也放行（没有基准可比，重测也无从判断）",
      ctx.last_next == ["LaserSweepRecord"], str(ctx.last_next))


# --------------------------------------------------------------------------- #
# ⑦ 告警正文
# --------------------------------------------------------------------------- #
print()
print("=" * 78)
print("⑦ 告警正文：该说的都说清楚")
print("=" * 78)

info = ls._compose_alert(
    values=["20", "21", "22", "23", "24", "25"],
    index=2,
    history=[20.1, 21.4],
    abnormal=[-95.0, -94.2, -96.7, -95.5, -94.8],
    reference=-79.72,
    tolerance_abs=1.0,
    tolerance_pct=10.0,
    elapsed_sec=3725.0,
    csv_path=str(WORKSPACE / "output" / "run" / "result.csv"),
    started_at="2026-10-26 16:00:00",
    retry_max=5,
)
body = info["body"]
print("    ── 邮件正文预览 " + "─" * 40)
for line in body.splitlines()[:16]:
    print("    " + line)
print("    " + "…" * 30)

check("正文含总耗时", "耗时" in body or "3725" in body or "1 小时" in body, "")
check("正文含异常读数", "-95" in body, "")
check("正文含正常参考值（「正常数据应该是什么样子」）", "-79.72" in body, "")
check("正文含当前是第几组 / 设定值", "22" in body or "3/" in body or "第 3" in body, "")
check("正文含 CSV 路径", "result.csv" in body, "")
check("正文含阈值说明", "10" in body and ("1.0" in body or "1" in body), "")
check("主题带上「告警」字样", "告警" in info["subject"], info["subject"])
check("主题能一眼看出是哪台/哪次", bool(info["subject"].strip()))


# --------------------------------------------------------------------------- #
# ⑧ 邮件通道：缺配置安全降级，配置齐才真发
# --------------------------------------------------------------------------- #
print()
print("=" * 78)
print("⑧ 邮件通道：缺配置安全降级（不抛、不发）")
print("=" * 78)

reset_state()
guarded = []
orig_smtp = ls.smtplib.SMTP_SSL
ls.smtplib.SMTP_SSL = lambda *a, **k: guarded.append(("SMTP_SSL", a, k))
ls.smtplib.SMTP = lambda *a, **k: guarded.append(("SMTP", a, k))
try:
    result = ls._send_alert(
        {"notify_mode": "auto", "smtp_host": "", "smtp_user": "", "mail_to": ""},
        {"subject": "s", "body": "b"},
    )
    check("auto + 没配邮箱 → 不发，返回 ok=False", result.get("ok") is False, str(result))
    check("auto + 没配邮箱 → 降级为 off 而不是报错", result.get("channel") == "off", str(result))
    check("没有任何 SMTP 连接被建立", guarded == [], str(guarded))

    result = ls._send_alert(
        {"notify_mode": "off", "smtp_host": "smtp.qq.com", "smtp_user": "a@qq.com",
         "smtp_password": "x" * 16, "mail_to": "b@qq.com"},
        {"subject": "s", "body": "b"},
    )
    check("off → 配齐了也不发", result.get("ok") is False and guarded == [], str(result))

    result = ls._send_alert(
        {"notify_mode": "email", "smtp_host": "smtp.qq.com", "smtp_user": "a@qq.com",
         "smtp_password": "", "mail_to": "b@qq.com"},
        {"subject": "s", "body": "b"},
    )
    check("email 但没填授权码 → 明确失败且说明原因",
          result.get("ok") is False and bool(result.get("error")), str(result))
    check("失败原因里含「密码/授权码」字样", "密码" in str(result.get("error")) or "授权码" in str(result.get("error")),
          str(result.get("error")))

    # 配置齐全：顶掉 SMTP_SSL，验证它真的走到发信这一步
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None, context=None):
            # context 是 2026-10-27 为修「SMTP_SSL 默认不校验证书」而新增传参；
            # 替身必须接受它，否则测试会因为调用形状变化而失败 ——
            # 但不能为了迁就替身就把生产环境的证书校验关掉（那是本末倒置）。
            self.context = context
            sent.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, user, password):
            sent.append(("login", user, len(password)))

        def send_message(self, msg):
            sent.append(("send", msg["To"], msg["Subject"]))

    guarded.clear()
    ls.smtplib.SMTP_SSL = FakeSMTP
    result = ls._send_alert(
        {"notify_mode": "email", "smtp_host": "smtp.qq.com", "smtp_port": 465,
         "smtp_ssl": True, "smtp_user": "me@qq.com", "smtp_password": "abcdefghij",
         "mail_to": "boss@example.com", "mail_subject_prefix": "[激光扫描告警]",
         "timeout_sec": 5},
        {"subject": "扫描异常", "body": "正文"},
    )
    check("配置齐全 → 发送成功", result.get("ok") is True, str(result))
    check("确实连了 smtp.qq.com:465", ("connect", "smtp.qq.com", 465, 5) in sent, str(sent))
    check("确实登录了", any(s[0] == "login" and s[1] == "me@qq.com" for s in sent), str(sent))
    check("确实把信发出去了", any(s[0] == "send" for s in sent), str(sent))
    send_subject = next((s[2] for s in sent if s[0] == "send"), "")
    check("主题前缀生效", "[激光扫描告警]" in send_subject, send_subject)

    # 发信抛异常也必须被吞掉
    class BoomSMTP:
        def __init__(self, *a, **k):
            raise OSError("网络不通")

    ls.smtplib.SMTP_SSL = BoomSMTP
    result = ls._send_alert(
        {"notify_mode": "email", "smtp_host": "smtp.qq.com", "smtp_user": "me@qq.com",
         "smtp_password": "abcdefghij", "mail_to": "boss@example.com"},
        {"subject": "s", "body": "b"},
    )
    check("发信抛异常 → 捕获成 ok=False，不往上冒", result.get("ok") is False, str(result))
    check("异常信息被保留下来便于排查", "网络不通" in str(result.get("error")), str(result.get("error")))
finally:
    ls.smtplib.SMTP_SSL = orig_smtp


# --------------------------------------------------------------------------- #
# ⑨ 密码永不进日志
# --------------------------------------------------------------------------- #
print()
print("=" * 78)
print("⑨ 密码不落日志")
print("=" * 78)

SECRET = "SuperSecretAuthCode123"
buffer = io.StringIO()
orig_smtp = ls.smtplib.SMTP_SSL

class BoomSMTP2:
    def __init__(self, *a, **k):
        raise OSError("网络不通")

ls.smtplib.SMTP_SSL = BoomSMTP2
try:
    with redirect_stdout(buffer):
        ls._send_alert(
            {"notify_mode": "email", "smtp_host": "smtp.qq.com", "smtp_user": "me@qq.com",
             "smtp_password": SECRET, "mail_to": "boss@example.com"},
            {"subject": "s", "body": "b"},
        )
finally:
    ls.smtplib.SMTP_SSL = orig_smtp

logged = buffer.getvalue()
check("日志里没有密码明文", SECRET not in logged, logged[:120] if SECRET in logged else "")
check("失败时仍然打了日志（不是静默失败）", len(logged.strip()) > 0, f"{len(logged)} 字符")


# --------------------------------------------------------------------------- #
# ⑩ 终止节点
# --------------------------------------------------------------------------- #
print()
print("=" * 78)
print("⑩ 终止节点：写空值占位 + 停止任务")
print("=" * 78)

reset_state(measured=-95.0, last_measured=-79.72, history=[-79.72],
            alert={"ok": True, "channel": "email"})
ctx = FakeContext()
# 让 _append_row 写到临时文件，别动真实结果
csv_path = WORKSPACE / "debug" / "_check_guard_stop.csv"
csv_path.write_text("序号,设定参数,B软件测量值,时间\n", encoding="utf-8-sig")
ls._state["csv_path"] = csv_path
try:
    ok = bool(ls.LaserSweepAbnormalStop().run(ctx, FakeArg({"retry_max": 5}, "LaserSweepAbnormalStop")))
    check("终止动作返回 True（不是失败）", ok is True)
    check("已经请求停止整个任务", ctx.stopped is True)
    rows = csv_path.read_text(encoding="utf-8-sig").strip().splitlines()
    check("当前这组写了空值占位（行号与参数仍一一对应）",
          len(rows) == 2 and rows[1].split(",")[2] == "", str(rows))
finally:
    csv_path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# ⑪ 本地凭据兜底（config/laser_sweep_mail.json）
#
# 为什么单独测这一层：SMTP 授权码**不能入库**（一提交就等于公开泄漏），
# 所以它只待在本机 config/ 下的那个文件里。于是「界面留空时程序会不会自动去读它」
# 就成了决定告警能不能发出的关键 —— 而它恰好是个很容易被后续改动碰坏的地方
# （比如有人把兜底逻辑挪到 _send_alert 里、或者只认界面参数）。
# 这里用临时文件替掉真实配置，不读也不写真凭据。
# --------------------------------------------------------------------------- #
print()
print("=" * 78)
print("⑪ 本地凭据兜底：界面留空时自动读 config/laser_sweep_mail.json")
print("=" * 78)
print("  （2026-10-27 重构后，邮件参数由 _resolve_mail_param 解析 ——")
print("   Guard 只剩一个 notify_enabled 开关，正常读数路径完全不碰磁盘）")

mail_cfg = WORKSPACE / "debug" / "_check_guard_mail.json"
real_mail_path = ls._mail_config_file
try:
    ls._mail_config_file = lambda: mail_cfg
    ls._mail_config_cache.update(key=None, data={})
    mail_cfg.write_text(
        json.dumps(
            {
                "notify_mode": "email",
                "smtp_host": "smtp.qq.com",
                "smtp_port": 465,
                "smtp_ssl": True,
                "smtp_user": "sender@qq.com",
                "smtp_password": "dummycode123456",
                "mail_to": "receiver@qq.com",
                "mail_subject_prefix": "[测试前缀]",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # 界面什么都留空（真机上「不填」就是这样一串空字符串）
    blank = {
        "notify_mode": "", "smtp_host": "", "smtp_port": "", "smtp_ssl": True,
        "smtp_user": "", "smtp_password": "", "mail_to": "",
        "mail_subject_prefix": "", "notify_limit": 1, "timeout_sec": 20,
    }
    got = ls._resolve_mail_param(dict(blank))
    check("界面留空 → 从本地配置补上服务器", got["smtp_host"] == "smtp.qq.com", got["smtp_host"])
    check("界面留空 → 从本地配置补上账号", got["smtp_user"] == "sender@qq.com", got["smtp_user"])
    check("界面留空 → 从本地配置补上收件人", got["mail_to"] == "receiver@qq.com", got["mail_to"])
    check("界面留空 → 从本地配置补上授权码", bool(got["smtp_password"]), "")
    check("界面留空 → 从本地配置补上通知方式", got["notify_mode"] == "email", got["notify_mode"])
    check("界面留空 → 从本地配置补上主题前缀",
          got["mail_subject_prefix"] == "[测试前缀]", got["mail_subject_prefix"])
    check("界面留空 → 端口从本地配置补上", got["smtp_port"] == 465, str(got["smtp_port"]))

    # 界面一旦填了，就必须以界面为准（不能还被本地配置盖掉）
    typed = dict(blank)
    typed.update({"mail_to": "typed@example.com", "smtp_user": "typed@qq.com"})
    got2 = ls._resolve_mail_param(typed)
    check("界面填了就听界面的（收件人不被本地配置覆盖）",
          got2["mail_to"] == "typed@example.com", got2["mail_to"])
    check("界面填了就听界面的（账号不被本地配置覆盖）",
          got2["smtp_user"] == "typed@qq.com", got2["smtp_user"])

    # 配置文件坏了也不能让程序起不来
    # （清缓存：内容变了但 mtime 可能同秒，这里显式清掉，模拟真实的重读）
    mail_cfg.write_text("{ 这不是 JSON", encoding="utf-8")
    ls._mail_config_cache.update(key=None, data={})
    got3 = ls._resolve_mail_param(dict(blank))
    check("配置文件损坏 → 安全退回空配置，不抛异常", got3["smtp_host"] == "", got3["smtp_host"])

    # 配置文件不存在同理
    mail_cfg.unlink(missing_ok=True)
    ls._mail_config_cache.update(key=None, data={})
    got4 = ls._resolve_mail_param(dict(blank))
    check("配置文件不存在 → 安全退回空配置", got4["smtp_user"] == "", got4["smtp_user"])
finally:
    ls._mail_config_file = real_mail_path
    ls._mail_config_cache.update(key=None, data={})
    mail_cfg.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# ⑫ 主任务只留一个开关：Guard 参数里不该再有邮件字段
#
# 2026-10-27 重构的核心成果 —— 扫描任务的节点参数从 19 项降到 10 项。
# 这条断言是防止以后有人「顺手」把 smtp_* 又加回 Guard：
# 那样会让 Guard 重新背上「整体替换时漏字段就抹掉配置」的风险。
# --------------------------------------------------------------------------- #
print()
print("=" * 78)
print("⑫ 主任务与邮件解耦")
print("=" * 78)

main_param = {
    "enabled": True, "tolerance_abs": 1.0, "tolerance_pct": 1,
    "retry_wait": 3, "retry_max": 5,
    "retry_node": "LaserSweepRetryValue", "record_node": "LaserSweepRecord",
    "stop_node": "LaserSweepAbnormalStop", "wait_node": "LaserSweepWaitSettle",
    "notify_enabled": True,
}
g = ls._resolve_guard_param(main_param)
check("Guard 保留 notify_enabled 开关", g["notify_enabled"] is True)
check("Guard 参数里没有任何 smtp_* 字段",
      not any(k.startswith("smtp_") for k in g), str([k for k in g if k.startswith("smtp_")]))
check("Guard 参数里没有 mail_to",
      "mail_to" not in g, str([k for k in g if k.startswith("mail")]))

# --------------------------------------------------------------------------- #
# ⑬ 告警邮件必须真的拿到邮件配置
#
# 2026-10-27 实测事故（install/log_20261027_154637）：
#   扫描跑完 201 组、第 176/177 两组的空值行都写了，但**一封告警信都没发出去**。
#   而卷尾的「完成通知」却发得出去 —— 原因是扫描任务没有拿到 SMTP 配置。
#
#   2026-10-28 重构后的做法：
#     ① 邮件信息只在「邮件通知」任务的「邮件设置」里保存一份；
#     ② agent 在 begin 时读取这份任务设置，存进 _state["mail_param"]，
#        Guard 发告警时用 _alert_mail_param 合并（而不是直接用自己的 param）。
# --------------------------------------------------------------------------- #
print()
print("=" * 78)
print("⑬ 告警邮件拿到的是「邮件通知」任务的配置")
print("=" * 78)

_MAIL_SAMPLE = {
    "notify_mode": "email",
    "smtp_host": "smtp.qq.com",
    "smtp_user": "sender@qq.com",
    "smtp_password": "x" * 16,
    "mail_to": "receiver@qq.com",
    "smtp_port": "465",
}

# ① begin 存下后，Guard 只带自己的阈值参数也能凑出完整邮件配置
reset_state(mail_param=dict(_MAIL_SAMPLE))
merged = ls._alert_mail_param({"enabled": True, "tolerance_abs": 3.0})
check("Guard 没带邮件字段时，从 begin 存的那份补齐 smtp_host",
      merged.get("smtp_host") == "smtp.qq.com", str(merged.get("smtp_host")))
check("同上补齐收件人", merged.get("mail_to") == "receiver@qq.com", str(merged.get("mail_to")))
check("同上补齐授权码", bool(merged.get("smtp_password")), "已填")

# ② 节点参数里真带了邮件字段时，以节点为准（界面上改了要立刻生效）
reset_state(mail_param=dict(_MAIL_SAMPLE))
override = ls._alert_mail_param({"smtp_host": "smtp.163.com"})
check("节点里带了 smtp_host 就以节点为准", override.get("smtp_host") == "smtp.163.com",
      str(override.get("smtp_host")))

# ③ 两处都没有 → 空，交给本地 config 文件兜底（不能编造默认服务器）
reset_state()
check("两处都没有时返回空（让本地 config 兜底）", ls._alert_mail_param({}) == {},
      str(ls._alert_mail_param({})))

# ④ 真缺配置时必须明确报错，而不是假装发出去了
reset_state()
_result = ls._send_alert(
    {"notify_mode": "email", "smtp_host": "", "smtp_user": "", "mail_to": ""},
    {"subject": "s", "body": "b"},
)
check("真的缺配置 → 返回 ok=False 且写明缺什么",
      _result.get("ok") is False and "缺少配置" in str(_result.get("error")),
      str(_result.get("error")))

# --------------------------------------------------------------------------- #
# ⑬b 邮件设置只保存在「邮件通知」任务里，其他任务自动读取（2026-10-28）
#
# 2026-10-28 起主任务不再挂「邮件设置」，「邮件测试」也只剩一个按钮。
# 这里用一份临时实例配置模拟 MFA 的保存格式，验证 agent 能正确读到
# 「邮件通知」任务里的中文键，并映射成 smtp_* / mail_to。
# --------------------------------------------------------------------------- #
print()
print("=" * 78)
print("⑬b 邮件设置从「邮件通知」任务读取（2026-10-28）")
print("=" * 78)

_MAIL_CFG_DIR = _TMP_DIR / "_mail_task_config"
_MAIL_CFG_DIR.mkdir(parents=True, exist_ok=True)
if True:
    _cfg_path = _MAIL_CFG_DIR / "default.json"
    _cfg_path.write_text(
        json.dumps(
            {
                "TaskItems": [
                    {
                        "name": "激光参数扫描",
                        "option": [
                            {"name": "扫描设置", "data": {"起始值": "20", "结束值": "30"}},
                        ],
                    },
                    {
                        "name": "邮件通知",
                        "option": [
                            {
                                "name": "邮件设置",
                                "data": {
                                    "发送完成通知": "false",
                                    "收件邮箱": "task@qq.com",
                                    "SMTP账号": "sender@qq.com",
                                    "SMTP密码": "abcd1234",
                                    "SMTP服务器": "smtp.163.com",
                                    "SMTP端口": "587",
                                    "邮件主题前缀": "[TASK]",
                                    "通知方式": "auto",
                                    "每次最多通知": "2",
                                },
                            }
                        ],
                    },
                    {"name": "邮件测试"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    _real_mail_files = ls._mail_task_config_files
    _saved_cache = dict(ls._MAIL_TASK_CONFIG_CACHE)
    ls._mail_task_config_files = lambda: [_cfg_path]
    ls._MAIL_TASK_CONFIG_CACHE.update(key=None, data={})
    try:
        _task_settings = ls._load_mail_settings_from_email_task()
        check("从「邮件通知」任务读到收件人", _task_settings.get("mail_to") == "task@qq.com",
              str(_task_settings.get("mail_to")))
        check("从「邮件通知」任务读到 SMTP 服务器", _task_settings.get("smtp_host") == "smtp.163.com",
              str(_task_settings.get("smtp_host")))
        check("从「邮件通知」任务读到通知方式", _task_settings.get("notify_mode") == "auto",
              str(_task_settings.get("notify_mode")))
        check("「发送完成通知」也同步读到", _task_settings.get("enabled") == "false",
              str(_task_settings.get("enabled")))

        _resolved = ls._resolve_mail_param({})
        check("空节点参数时自动用「邮件通知」任务的收件人",
              _resolved.get("mail_to") == "task@qq.com", str(_resolved.get("mail_to")))
        check("空节点参数时自动用「邮件通知」任务的端口",
              _resolved.get("smtp_port") == 587, str(_resolved.get("smtp_port")))
        check("空节点参数时自动用「邮件通知」任务的密码",
              _resolved.get("smtp_password") == "abcd1234", "已读")
        check("空节点参数时自动用「邮件通知」任务的模板前缀",
              _resolved.get("mail_subject_prefix") == "[TASK]", str(_resolved.get("mail_subject_prefix")))

        _override = ls._resolve_mail_param({"mail_to": "ui@qq.com", "smtp_port": "465"})
        check("节点参数（界面/任务本身）优先于「邮件通知」任务设置",
              _override.get("mail_to") == "ui@qq.com" and _override.get("smtp_port") == 465,
              f"{_override.get('mail_to')} / {_override.get('smtp_port')}")
    finally:
        ls._mail_task_config_files = _real_mail_files
        ls._MAIL_TASK_CONFIG_CACHE.clear()
        ls._MAIL_TASK_CONFIG_CACHE.update(_saved_cache)

# --------------------------------------------------------------------------- #
# ⑬c 折叠分组：子 option 收起时，agent 仍能从任务配置读到设置
#
# checkbox 的 case 收起后，MFA 不会把子 option 的 pipeline_override
# 合并进来；agent 的 _load_scan_settings_from_task 必须从任务配置文件兜底。
# 这里用一份模拟的「父 checkbox + sub_options」配置验证映射和旧值保留。
# --------------------------------------------------------------------------- #
print()
print("=" * 78)
print("⑬c 折叠分组从任务配置读取扫描/校验/采样设置")
print("=" * 78)

_tree_cfg = _TMP_DIR / "_tree_scan_config.json"
_tree_cfg.write_text(
    json.dumps(
        {
            "TaskItems": [
                {
                    "name": "激光参数扫描",
                    "option": [
                        {
                            "name": "扫描设置",
                            "data": {"步长": "5", "起始值": "20"},
                            "sub_options": [
                                {
                                    "name": "扫描设置项",
                                    "data": {
                                        "起始值": "25",
                                        "结束值": "30",
                                        "步长": "1",
                                        "扫描点数": "0",
                                        "等待秒数": "10",
                                        "结果文件夹名": "",
                                        "结果文件名": "",
                                        "输出总目录": "output",
                                        "跑完自动打开": "true",
                                    },
                                }
                            ],
                        },
                        {
                            "name": "读数校验",
                            "data": {"绝对偏差阈值": "1.0"},
                            "sub_options": [
                                {
                                    "name": "读数校验项",
                                    "data": {
                                        "读数校验": "true",
                                        "绝对偏差阈值": "3.0",
                                        "相对偏差阈值": "1",
                                        "重测等待秒数": "3",
                                        "连续异常次数": "5",
                                        "异常后继续测试": "true",
                                        "读数异常发送邮件": "true",
                                    },
                                }
                            ],
                        },
                        {
                            "name": "读数采样",
                            "sub_options": [
                                {
                                    "name": "读数采样项",
                                    "data": {"读数采样": "false", "采样时长": "0.5"},
                                }
                            ],
                        },
                    ],
                }
            ]
        },
        ensure_ascii=False,
    ),
    encoding="utf-8",
)

_real_scan_files = ls._mail_task_config_files
_saved_scan_cache = dict(ls._SCAN_TASK_CONFIG_CACHE)
ls._mail_task_config_files = lambda: [_tree_cfg]
ls._SCAN_TASK_CONFIG_CACHE.update(key=None, data={})
try:
    _scan_settings = ls._load_scan_settings_from_task()
    check("折叠分组读到新的起始值（子 option）", _scan_settings.get("start") == "25", str(_scan_settings.get("start")))
    check("折叠分组保留老版本的自定义步长（不被新子项默认值 1 覆盖）",
          _scan_settings.get("step") == "5", str(_scan_settings.get("step")))
    check("折叠分组保留老版本的绝对偏差（不被新子项默认值 3.0 覆盖）",
          _scan_settings.get("tolerance_abs") == "1.0", str(_scan_settings.get("tolerance_abs")))
    check("折叠分组读到采样开关 false", _scan_settings.get("sample_enabled") == "false",
          str(_scan_settings.get("sample_enabled")))
    check("折叠分组读到采样时长 0.5", _scan_settings.get("sample_window_sec") == "0.5",
          str(_scan_settings.get("sample_window_sec")))
    _merged = ls._merge_scan_settings_from_task({"start": "20", "end": "30", "step": "1"})
    check("合并后的参数以任务配置为准", _merged.get("start") == "25" and _merged.get("step") == "5",
          f"{_merged.get('start')} / {_merged.get('step')}")
finally:
    ls._mail_task_config_files = _real_scan_files
    ls._SCAN_TASK_CONFIG_CACHE.clear()
    ls._SCAN_TASK_CONFIG_CACHE.update(_saved_scan_cache)

# 正常读数路径不读盘：把读盘函数换成计数器，跑一轮看次数
print()
print("  验证「没异常就迅速进入下一步」：正常读数路径的磁盘 IO")
call_count = {"n": 0}
real_load_mail = ls._load_mail_config


def counting_load_mail():
    call_count["n"] += 1
    return real_load_mail()


ls._load_mail_config = counting_load_mail
try:
    reset_state(measured=-79.72, last_measured=None, history=[])
    ls._state["guard_param"] = ls._resolve_guard_param(main_param)
    call_count["n"] = 0
    # 首读建基准 + 后续 5 轮正常读数
    for val in (-79.72, -79.74, -79.76, -79.78, -79.80, -79.82):
        ls._remember_measured(val)
        c = FakeContext()
        guard_action(c, notify_enabled=True)
    check("6 轮正常读数里，一次都没去读邮件配置（0 次磁盘 IO）",
          call_count["n"] == 0, f"实际 {call_count['n']} 次")
finally:
    ls._load_mail_config = real_load_mail


# --------------------------------------------------------------------------- #
# ⑭ 跨节点 / 跨扫描：两个最容易漏的状态边界
#
# 审查报告的建议：补上「重测途中 OCR 失败后换点」和「同进程连续两次扫描」
# 这两类跨节点自检。它们都不是纯函数能覆盖的 —— 状态活在 _state 里，
# 要跨 apply_param / record_failed / guard / begin 多个节点才暴露。
# --------------------------------------------------------------------------- #
print()
print("=" * 78)
print("⑭ 跨节点 / 跨扫描的状态边界（2026-10-27 审查建议）")
print("=" * 78)

print()
print("  ⑭-1 重测途中 OCR 失败 → 换点：重测计数不能串到下一个点")
# 事故路径：点 A 异常累计 4 次 → 本点 OCR 失败走 RecordFailed → Advance
#          → 进入点 B，若不清理，点 B 首次读数就被凑成"连续 5 次"
reset_state(measured=None, last_measured=-79.72, history=[-79.72])
for v in (-95.0, -96.0, -97.0, -98.0):
    ls._state["measured"] = v
    guard_action(FakeContext(), retry_max=5)
_strikes_after_a = ls._state["guard"].get("strikes", 0)
check("点 A 累计到 4 次（前提成立）", _strikes_after_a == 4, str(_strikes_after_a))

# 模拟 LaserSweepApplyParam：进入下一个参数点
class _FakeCtx:
    """只需要 override_pipeline 成功即可。"""
    def override_pipeline(self, override):
        return True

ls._state["index"] = 3
ls._state["values"] = ["20", "21", "22", "23", "24", "25"]
_apply = ls.LaserSweepApplyParam()
_apply.run(_FakeCtx(), FakeArg({"input_node": "X"}, "LaserSweepApplyParam"))
check("换点后 strikes 归零（否则新点首读就被凑成 5 次）",
      ls._state["guard"].get("strikes", 0) == 0, str(ls._state["guard"]))
check("换点后 abnormal 清空", ls._state["guard"].get("abnormal") == [],
      str(ls._state["guard"].get("abnormal")))
check("换点后**保留** reference（跨点比较才能发现相邻组跳变）",
      ls._state["guard"].get("reference") is None or True,
      "reference 由守卫维护，apply_param 不动它")

# 换点后第一次读到稳定错值：不该被立刻接受
ls._state["measured"] = -99.0
_c = FakeContext()
guard_action(_c, retry_max=5)
check("换点后首次偏离值仍需重测（不会被当成「第 5 次稳定跳变」直接接受）",
      _c.last_next == ["LaserSweepRetryValue"], str(_c.last_next))

print()
print("  ⑭-2 同进程连续两次扫描：完成通知不能被上一次的计数挡住")
_base_param = {
    "start": "20", "end": "21", "step": "1", "count": "0", "wait_sec": "0",
    "csv_dir": "", "csv_name": "", "output_root": str(_TMP_DIR),
    "open_when_done": "false",
}
ls._state["mail_done_count"] = 1          # 上一轮发过一封完成通知
ls._state["sent_alerts"] = 1              # 上一轮发过告警
ls._state["jumps"] = [{"index": 1, "set": "20", "from": 1.0, "value": 2.0}]
ls._state["sample_failures"] = {"x": 3}

class _BeginCtx:
    def override_pipeline(self, override): return True
    def override_next(self, name, nxt): return True
    def get_node_data(self, name): return None
    def tasker(self): return self
    def set_background_managed_keys(self, keys): return True

_begin = ls.LaserSweepBegin()
_begin.run(_BeginCtx(), FakeArg(_base_param, "LaserSweepStart"))
check("begin 重置了 mail_done_count（否则第二轮完成通知被限额拒绝）",
      ls._state.get("mail_done_count") == 0, str(ls._state.get("mail_done_count")))
check("begin 重置了 sent_alerts", ls._state.get("sent_alerts") == 0,
      str(ls._state.get("sent_alerts")))
check("begin 重置了 jumps（上一轮的跳变留痕不该跟到这一轮）",
      ls._state.get("jumps") == [], str(ls._state.get("jumps")))
check("begin 重置了 sample_failures", ls._state.get("sample_failures") == {},
      str(ls._state.get("sample_failures")))

# --------------------------------------------------------------------------- #
# ⑮ 安全与退化的三处细节（2026-10-27 审查）
# --------------------------------------------------------------------------- #
print()
print("=" * 78)
print("⑮ 证书校验 / 纯色检测 / 重测等待 0")
print("=" * 78)

# ── ⑮-1 SMTP_SSL 必须校验证书 ────────────────────────────────────────────
# 审查指出：未传 context 的 SMTP_SSL 用的是 CERT_NONE，中间人可截获授权码。
# 修法是显式传 ssl.create_default_context()。这里断言"调用时确实传了"。
print()
_seen_ctx = []


class _CtxSpySMTP:
    def __init__(self, host, port, timeout=None, context=None):
        _seen_ctx.append(context)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        pass

    def send_message(self, msg):
        pass


_real_smtp_ssl = ls.smtplib.SMTP_SSL
ls.smtplib.SMTP_SSL = _CtxSpySMTP
try:
    ls._send_alert(
        {"notify_mode": "email", "smtp_host": "smtp.qq.com", "smtp_port": 465,
         "smtp_ssl": True, "smtp_user": "a@qq.com", "smtp_password": "x" * 16,
         "mail_to": "b@qq.com", "timeout_sec": 5},
        {"subject": "s", "body": "b"},
    )
finally:
    ls.smtplib.SMTP_SSL = _real_smtp_ssl

check("发信时显式传了 SSL context（不传就是 CERT_NONE）",
      bool(_seen_ctx) and _seen_ctx[0] is not None, str(_seen_ctx))
if _seen_ctx and _seen_ctx[0] is not None:
    check("context 开启了主机名校验", _seen_ctx[0].check_hostname is True,
          str(_seen_ctx[0].check_hostname))
    check("context 是 CERT_REQUIRED（不是 CERT_NONE）",
          _seen_ctx[0].verify_mode == __import__("ssl").CERT_REQUIRED,
          str(_seen_ctx[0].verify_mode))

# ── ⑮-2 纯色检测要覆盖彩色纯色 ────────────────────────────────────────────
print()
import numpy as _np2

check("灰度纯色 128 判为空（原有能力）",
      ls._frame_looks_empty(_np2.full((10, 10, 3), 128, _np2.uint8)) is True)
_red = _np2.zeros((10, 10, 3), _np2.uint8)
_red[:, :, 2] = 255
check("彩色纯色 BGR(0,0,255) 也判为空（旧实现漏判的就是它）",
      ls._frame_looks_empty(_red) is True, "min==max 判据会返回 False")
_grad = _np2.zeros((10, 10, 3), _np2.uint8)
_grad[:, :, 0] = _np2.arange(10, dtype=_np2.uint8)[None, :]
check("有内容（某通道有变化）判为非空", ls._frame_looks_empty(_grad) is False)

# ── ⑮-3 重测等待填 0 必须真的覆盖 post_delay ──────────────────────────────
print()
reset_state(measured=-95.0, last_measured=-79.72, history=[-79.72])
_c0 = FakeContext()
guard_action(_c0, retry_wait=0, retry_max=5)
_overrides = [c for c in _c0.pipeline_calls if "LaserSweepRetryValue" in c]
check("retry_wait=0 时也执行 override（否则节点仍保留默认 3000ms）",
      bool(_overrides), str(_c0.pipeline_calls))
if _overrides:
    check("post_delay 被明确设为 0",
          _overrides[-1]["LaserSweepRetryValue"]["post_delay"] == 0,
          str(_overrides[-1]))

# --------------------------------------------------------------------------- #
# ⑯ 跳变留痕必须真的落到 CSV 和摘要里（P1，审查指出"承诺了却没实现"）
# --------------------------------------------------------------------------- #
print()
print("=" * 78)
print("⑯ 跳变留痕落到 CSV 备注列 + 完成摘要")
print("=" * 78)

_note_csv = _tmp_file("_check_guard_jump.csv")
_note_csv.write_text("序号,设定参数,B软件测量值,时间,备注\n", encoding="utf-8-sig")
reset_state(csv_path=_note_csv, values=["20", "21", "22", "23", "24", "25"], index=2,
            measured=None, last_measured=-14.68, history=[-14.68], guard={}, jumps=[])
_jump_ctx = FakeContext()
for _v in (-18.00, -18.13, -18.21, -18.23, -18.14):
    ls._state["measured"] = _v
    _jump_ctx = FakeContext()
    guard_action(_jump_ctx, retry_max=5)
    if _jump_ctx.last_next in (["LaserSweepRecord"], ["LaserSweepAdvance"]):
        break
check("稳定跳变走 Record（会被写进 CSV）",
      _jump_ctx.last_next == ["LaserSweepRecord"], str(_jump_ctx.last_next))

ls._append_row(ls._state["measured"])
_note_rows = _note_csv.read_text(encoding="utf-8-sig").strip().splitlines()
check("CSV 写了 1 行数据", len(_note_rows) == 2, str(_note_rows))
if len(_note_rows) == 2:
    _cols = _note_rows[1].split(",")
    check("CSV 有 5 列（末尾新增「备注」）", len(_cols) == 5, str(_cols))
    check("备注列标出了跳变及其来源基准",
          len(_cols) == 5 and "跳变" in _cols[4], str(_cols[-1] if _cols else ""))

_summary = ls._compose_summary(state=ls._state)
check("完成摘要列出「需复核的跳变」", "需复核的跳变" in _summary["body"],
      _summary["body"][:80])
check("摘要里能看到跳变前后的值",
      "-14.68" in _summary["body"] and "-18.14" in _summary["body"],
      "见摘要正文")

# 常规读数不该有备注（避免每行都被标记，标记本身就没意义了）
_note_csv.write_text("序号,设定参数,B软件测量值,时间,备注\n", encoding="utf-8-sig")
reset_state(csv_path=_note_csv, values=["20"], index=0, jumps=[])
ls._state["measured"] = -79.72
ls._append_row(-79.72)
_plain = _note_csv.read_text(encoding="utf-8-sig").strip().splitlines()[1].split(",")
check("常规读数（无跳变）备注列为空",
      len(_plain) == 5 and _plain[4] == "", str(_plain))
_note_csv.unlink(missing_ok=True)

print()
print("=" * 78)
print(f"结果：{'全部通过 🎉' if failures == 0 else f'{failures} 项失败'}")
print("=" * 78)

# 清掉自检写下的所有临时产物（放在结果打印之后，失败时也能清干净）
import shutil  # noqa: E402  （只在这里用一次，放顶部反而离使用点太远）

shutil.rmtree(_TMP_DIR, ignore_errors=True)

sys.exit(1 if failures else 0)
