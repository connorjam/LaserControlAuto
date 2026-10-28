"""绘制曲线图自检：CSV 解析、最近 CSV 选择、PNG 生成、界面/流水线配置一致性。

    python tools/check_plot.py

背景（2026-10-28 新增「绘制曲线图」任务）：
    「绘制曲线图」是**独立任务**：把扫描导出的 CSV 画成曲线（X = 温度，
    Y = B 软件读数 / 输出功率），PNG 保存在 CSV 同级目录，默认画最近导出的
    单个 CSV。它的失败模式都很安静 —— 选错文件、认错列、画出空白图，
    表面都像"任务跑完了"，所以这些判据必须离线钉住。

不连软件、不依赖 maa 包，几秒钟跑完。绘图部分用 Windows 自带的 GDI+，
非 Windows 环境会跳过「真的生成 PNG」那几条（其余逻辑照测）。
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import sys
import time
import types
import importlib.util
from contextlib import contextmanager
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE / "agent"))

# Windows 控制台默认是 GBK，打印 ✅/❌ 会 UnicodeEncodeError；
# 和 agent 里一样改成 UTF-8（改不了就算了，不影响断言本身）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

PASS, FAIL = "✅", "❌"
failures = 0


def check(title: str, condition: bool, detail: str = "") -> None:
    global failures
    if not condition:
        failures += 1
    print(f"{PASS if condition else FAIL} {title}" + (f"  → {detail}" if detail else ""))


@contextmanager
def scratch(name: str):
    """临时目录放 debug/ 下：受限环境里系统 temp 不一定可写。"""
    base = WORKSPACE / "debug" / f"_check_plot_{name}"
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def write_csv(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8-sig")


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


def png_size(path: Path) -> tuple[int, int]:
    """只读 PNG 的 IHDR，取宽高（不依赖 PIL）。"""
    raw = path.read_bytes()
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
        raise ValueError("不是合法的 PNG")
    width, height = struct.unpack(">II", raw[16:24])
    return width, height


import plot_curve  # noqa: E402  （上面已经把 agent/ 加进 sys.path）

# --------------------------------------------------------------------------- #
# ① CSV 解析：默认表头 / 自定义表头 / 无表头 / 空值行
# --------------------------------------------------------------------------- #
print("=" * 78)
print("① CSV 解析")
print("=" * 78)

SAMPLE_ROWS = [
    "序号,设定参数,B软件测量值,时间,备注",
    "1,20,-79.72,2026-10-28 20:00:00,",
    "2,21,-79.74,2026-10-28 20:01:00,",
    "3,22,-79.70,2026-10-28 20:02:00,",
    "4,23,-79.81,2026-10-28 20:03:00,",
    "5,24,,2026-10-28 20:04:00,读数失败",
    "6,25,-79.69,2026-10-28 20:05:00,",
]

with scratch("parse") as base:
    default_csv = base / "default.csv"
    write_csv(default_csv, "\n".join(SAMPLE_ROWS) + "\n")
    data = plot_curve.read_curve_csv(default_csv)
    check("默认表头：认出 X = 设定参数", data.x_header == "设定参数", data.x_header)
    check("默认表头：认出 Y = B软件测量值", data.y_header == "B软件测量值", data.y_header)
    check("默认表头：跳过 1 行空值", data.skipped == 1, str(data.skipped))
    check("默认表头：有效点 5 个", len(data.x_values) == 5, str(len(data.x_values)))
    check("默认表头：X 值正确", data.x_values == [20.0, 21.0, 22.0, 23.0, 25.0], str(data.x_values))
    check("默认表头：Y 值正确",
          data.y_values == [-79.72, -79.74, -79.70, -79.81, -79.69], str(data.y_values))

    # 自定义表头：关键词匹配 + 带单位的数值
    alt_csv = base / "alt.csv"
    write_csv(
        alt_csv,
        "温度（°C）,输出功率（dBm）,备注\n"
        "20.5,-79.7,ok\n"
        "21.0,-79.6,ok\n",
    )
    alt = plot_curve.read_curve_csv(alt_csv)
    check("自定义表头：认出温度/输出功率两列", alt.x_header == "温度（°C）" and alt.y_header == "输出功率（dBm）",
          f"{alt.x_header} / {alt.y_header}")
    check("带单位数值也能解析", alt.x_values == [20.5, 21.0] and alt.y_values == [-79.7, -79.6],
          f"{alt.x_values} / {alt.y_values}")

    # 无表头：1,20,-79.7 → X=第 2 列、Y=第 3 列
    bare_csv = base / "bare.csv"
    write_csv(bare_csv, "1,20,-79.7\n2,21,-79.6\n")
    bare = plot_curve.read_curve_csv(bare_csv)
    check("无表头 CSV：按第 2/3 列解析", bare.x_values == [20.0, 21.0] and bare.y_values == [-79.7, -79.6],
          f"{bare.x_values} / {bare.y_values}")

    # 全是空值 → 必须报可读错误，而不是画一张空图
    empty_csv = base / "empty.csv"
    write_csv(empty_csv, "序号,设定参数,B软件测量值,时间\n1,20,,2026-10-28 20:00:00\n")
    try:
        plot_curve.read_curve_csv(empty_csv)
        check("全部为空值时拒绝绘图", False, "没有抛 PlotError")
    except plot_curve.PlotError as exc:
        check("全部为空值时拒绝绘图", "没有可绘制的数据行" in str(exc), str(exc))

# --------------------------------------------------------------------------- #
# ② 最近导出的单个 CSV：按修改时间挑，忽略非 CSV
# --------------------------------------------------------------------------- #
print()
print("=" * 78)
print("② 最近导出的单个 CSV")
print("=" * 78)

with scratch("latest") as base:
    old_csv = base / "20261027_120000_20_30_11.csv"
    new_csv = base / "run2" / "20261028_200000_20_30_11.csv"
    write_csv(old_csv, "序号,设定参数,B软件测量值,时间\n1,20,-79.7,2026-10-27 12:00:00\n")
    write_csv(new_csv, "序号,设定参数,B软件测量值,时间\n1,20,-79.7,2026-10-28 20:00:00\n")
    (base / "readme.txt").write_text("not a csv", encoding="utf-8")
    # 明确把 old 设成更早、new 设成更晚（文件系统精度不够时也稳）
    os.utime(old_csv, (time.time() - 3600, time.time() - 3600))
    os.utime(new_csv, (time.time(), time.time()))

    latest = plot_curve.find_latest_csv(base)
    check("在子目录里找到最新的 CSV", latest == new_csv, str(latest))
    check("不把非 CSV 当成结果", latest is not None and latest.suffix.lower() == ".csv", str(latest))
    check("目录不存在时安全返回 None", plot_curve.find_latest_csv(base / "no_such_dir") is None, "")

# --------------------------------------------------------------------------- #
# ③ PNG 生成：尺寸、文件名、同目录、单点不崩
# --------------------------------------------------------------------------- #
print()
print("=" * 78)
print("③ PNG 生成")
print("=" * 78)

has_gdi = sys.platform == "win32" and plot_curve._gdiplus is not None
if not has_gdi:
    print("（非 Windows / 无 GDI+，跳过真实绘图断言）")
else:
    with scratch("render") as base:
        csv_path = base / "20261028_200000_20_30_11.csv"
        write_csv(csv_path, "\n".join(SAMPLE_ROWS) + "\n")
        result = plot_curve.plot_csv(csv_path)
        image = Path(result["image_path"])
        check("默认图片名 = <CSV名>_曲线.png", image.name == "20261028_200000_20_30_11_曲线.png", image.name)
        check("图片与 CSV 同目录", image.parent == csv_path.parent, str(image.parent))
        check("图片文件确实存在且非空", image.is_file() and image.stat().st_size > 10_000,
              f"{image.stat().st_size if image.is_file() else 0} 字节")
        try:
            width, height = png_size(image)
            check("PNG 尺寸 1600×1000", (width, height) == (1600, 1000), f"{width}×{height}")
        except ValueError as exc:
            check("PNG 格式合法", False, str(exc))
        check("统计：有效点 5 个 / 跳过 1 行", result["points"] == 5 and result["skipped"] == 1,
              f"{result['points']} / {result['skipped']}")

        # 用户指定图片名：仍然强制落在 CSV 同目录
        custom = plot_curve.plot_csv(csv_path, Path("subdir") / "my_plot.jpg")
        check("指定图片名时也只取文件名并补 .png",
              Path(custom["image_path"]).parent == csv_path.parent
              and Path(custom["image_path"]).name == "my_plot.png",
              str(custom["image_path"]))
        check("指定图片名也能生成", Path(custom["image_path"]).is_file(), str(custom["image_path"]))

        # 单点 CSV：不能因为跨度为 0 就除零 / 画崩
        single_csv = base / "single.csv"
        write_csv(single_csv, "序号,设定参数,B软件测量值,时间\n1,25,-79.7,2026-10-28 20:00:00\n")
        single = plot_curve.plot_csv(single_csv)
        check("单点 CSV 也能出图", Path(single["image_path"]).is_file(), str(single["image_path"]))

# --------------------------------------------------------------------------- #
# ④ 配置一致性：任务存在、分组存在、override 字段对齐
# --------------------------------------------------------------------------- #
print()
print("=" * 78)
print("④ 配置一致性：interface.json / my_task.json")
print("=" * 78)

interface = json.loads(strip_jsonc((WORKSPACE / "assets" / "interface.json").read_text(encoding="utf-8")))
pipeline = json.loads(
    strip_jsonc((WORKSPACE / "assets" / "resource" / "pipeline" / "my_task.json").read_text(encoding="utf-8"))
)

plot_task = next((t for t in interface["task"] if t["name"] == "绘制曲线图"), None)
check("任务列表里有「绘制曲线图」", plot_task is not None, str(plot_task))
check("任务是独立入口（group = 绘图）", isinstance(plot_task, dict) and plot_task.get("group") == ["绘图"],
      str(plot_task.get("group") if plot_task else None))
check("任务的 entry 在 pipeline 里有对应节点", plot_task and plot_task.get("entry") in pipeline,
      str(plot_task.get("entry") if plot_task else None))
check("有「绘图」分组", any(g.get("name") == "绘图" for g in interface.get("group", [])),
      str([g.get("name") for g in interface.get("group", [])]))
check("任务挂了「绘图设置」option",
      isinstance(plot_task, dict) and plot_task.get("option") == ["绘图设置"],
      str(plot_task.get("option") if plot_task else None))

plot_node = pipeline.get("LaserSweepPlotCurve") or {}
base_param = plot_node.get("custom_action_param") or {}
check("pipeline 节点有 custom_action=laser_sweep_plot_curve",
      plot_node.get("custom_action") == "laser_sweep_plot_curve",
      str(plot_node.get("custom_action")))

override = (interface.get("option", {}).get("绘图设置", {}).get("pipeline_override") or {}).get(
    "LaserSweepPlotCurve", {}
).get("custom_action_param") or {}
missing = sorted(set(base_param) - set(override))
extra = sorted(set(override) - set(base_param))
check("覆盖块没漏字段（漏一个就等于从 pipeline 抹掉）", missing == [], str(missing))
check("覆盖块没有拼错的字段名", extra == [], str(extra))
check("source_csv 来自界面占位符", override.get("source_csv") == "{CSV文件}", str(override.get("source_csv")))
check("image_name 来自界面占位符", override.get("image_name") == "{图片文件名}", str(override.get("image_name")))
check("open_when_done 来自界面开关", override.get("open_when_done") == "{完成后打开图片}",
      str(override.get("open_when_done")))

inputs = {item["name"] for item in interface["option"]["绘图设置"]["inputs"]}
check("界面有「CSV文件」「图片文件名」「完成后打开图片」三项",
      {"CSV文件", "图片文件名", "完成后打开图片"}.issubset(inputs), str(sorted(inputs)))

# 一个节点只许被一处 override 覆盖
owners: dict[str, list[str]] = {}
for option_name, option in interface["option"].items():
    for node_name in (option.get("pipeline_override") or {}):
        owners.setdefault(node_name, []).append(option_name)
conflicts = {n: o for n, o in owners.items() if len(o) > 1}
check("没有任何节点被两个 option 同时覆盖", conflicts == {}, str(conflicts))

# --------------------------------------------------------------------------- #
# ⑤ 任务层：默认 CSV 选择优先级 + 动作真的能产出图片
#
# 这一段用桩模块顶掉 maa，直接调 laser_sweep 里的 _resolve_plot_csv /
# LaserSweepPlotCurve —— 上面 ①~③ 测的是 plot_curve 模块本身，
# 这里测的是「任务接线」：会话状态 > 落盘 last_csv > output 里最新 CSV。
# --------------------------------------------------------------------------- #
print()
print("=" * 78)
print("⑤ 任务层：默认 CSV 选择与动作执行")
print("=" * 78)


def install_maa_stub() -> None:
    class _RunArg:
        pass

    class CustomAction:
        RunArg = _RunArg

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
    cm = types.ModuleType("maa.context")
    cm.Context = Context
    ca = types.ModuleType("maa.custom_action")
    ca.CustomAction = CustomAction
    cr = types.ModuleType("maa.custom_recognition")
    cr.CustomRecognition = CustomAction
    for name, mod in {
        "maa": maa,
        "maa.agent": agent,
        "maa.agent.agent_server": agent_server,
        "maa.context": cm,
        "maa.custom_action": ca,
        "maa.custom_recognition": cr,
    }.items():
        sys.modules[name] = mod


install_maa_stub()
_spec = importlib.util.spec_from_file_location("laser_sweep", WORKSPACE / "agent" / "laser_sweep.py")
ls = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ls)

with scratch("task") as base:
    out_root = base / "output"
    older = out_root / "run1" / "20261027_120000_20_30_11.csv"
    newer = out_root / "run2" / "20261028_200000_20_30_11.csv"
    write_csv(older, "序号,设定参数,B软件测量值,时间\n1,20,-79.7,2026-10-27 12:00:00\n")
    write_csv(newer, "序号,设定参数,B软件测量值,时间\n1,25,-79.6,2026-10-28 20:00:00\n")
    os.utime(older, (time.time() - 3600, time.time() - 3600))
    os.utime(newer, (time.time(), time.time()))

    # 1) 界面显式指定文件
    picked = ls._resolve_plot_csv({"source_csv": str(older)})
    check("界面指定 CSV → 用指定的那一份", picked == older, str(picked))
    # 2) 界面指定目录 → 目录里最新的
    picked_dir = ls._resolve_plot_csv({"source_csv": str(out_root)})
    check("界面指定目录 → 自动取目录里最新的 CSV", picked_dir == newer, str(picked_dir))

    saved_csv = ls._state.get("csv_path")
    real_state_file = ls._state_file
    ls._state_file = lambda: base / "laser_sweep_output.json"
    try:
        # 3) 会话里刚扫完的优先
        ls._state["csv_path"] = older
        picked_session = ls._resolve_plot_csv({})
        check("本次会话扫描过 → 优先用本次的 CSV", picked_session == older, str(picked_session))

        # 4) 会话没有 → 读落盘的 last_csv
        ls._state["csv_path"] = None
        ls._remember_output_root(out_root, newer)
        raw_state = json.loads(ls._state_file().read_text(encoding="utf-8"))
        check("扫描开始时会记下 last_csv", raw_state.get("last_csv") == str(newer), str(raw_state))
        picked_last = ls._resolve_plot_csv({})
        check("没有会话状态 → 用上次扫描记录（last_csv）", picked_last == newer, str(picked_last))

        # 5) 记录的文件没了 → 退回 output 里最新的
        newer.unlink()
        picked_fallback = ls._resolve_plot_csv({})
        check("记录的 CSV 不存在 → 退回 output 里最新的 CSV", picked_fallback == older, str(picked_fallback))

        # 6) 动作真的能跑通（open_when_done=false，避免自检弹窗）
        class _Arg:
            custom_action_param = {"source_csv": "", "image_name": "", "open_when_done": False}
            node_name = "LaserSweepPlotCurve"

        ok = ls.LaserSweepPlotCurve().run(None, _Arg())
        image = plot_curve.default_image_path(older)
        check("LaserSweepPlotCurve 动作返回成功", ok is True, str(ok))
        check("动作产出的图片落在 CSV 同级目录", image.is_file(), str(image))
    finally:
        ls._state_file = real_state_file
        ls._state["csv_path"] = saved_csv

print()
print("=" * 78)
if failures:
    print(f"{FAIL} 共 {failures} 条断言失败")
    sys.exit(1)
print(f"{PASS} 全部通过")
