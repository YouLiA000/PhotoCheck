"""界面层检查（K-11）。

Gradio 的坑：import 成功不代表能构造 Blocks（版本差异会在 build 时炸），
所以这里真的调一次 build()，而不是只看文件在不在。
"""

from __future__ import annotations

from audit.model import Context, Result, check, fail, ok, partial, skip


@check("UI-01", "K-11", "app.build() 能构造 Blocks", gate=("W8",))
def build_blocks(ctx: Context) -> Result:
    p = ctx.ui_dir / "app.py"
    if not p.is_file():
        return fail("缺 ui/app.py", "先做最小可演示界面")
    import importlib.util

    try:
        spec = importlib.util.spec_from_file_location("_pc_ui_app", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:
        return fail(f"import 失败：{type(exc).__name__}: {exc}", "多半是依赖版本或语法问题")
    build = getattr(mod, "build", None)
    if not callable(build):
        return fail("没有 build()", "K-11 要求可构造的 Blocks 工厂")
    try:
        demo = build()
    except Exception as exc:
        return fail(f"build() 抛错：{type(exc).__name__}: {exc}", "gradio 版本差异，按堆栈改")
    children = len(getattr(demo, "children", []) or [])
    return ok(f"Blocks 构造成功，{children} 个直接子块")


@check("UI-02", "K-11", "三页 Tab：检测 / 处置 / 结论", gate=("W8",))
def tabs(ctx: Context) -> Result:
    p = ctx.ui_dir / "app.py"
    if not p.is_file():
        return skip("没有 app.py")
    src = ctx.read_text(p)
    found = [name for name in ("检测", "处置", "结论") if f'gr.Tab("{name}")' in src or f"gr.Tab('{name}')" in src]
    if len(found) == 3:
        return ok(" / ".join(found))
    return partial(f"找到 {len(found)}/3：{' / '.join(found) or '无'}",
                   "1280×800 内塞不下全部控件，所以 K-11 要分 Tab")


@check("UI-03", "K-11", "界面测试全绿", gate=("W8",))
def ui_tests(ctx: Context) -> Result:
    if not (ctx.ui_dir / "tests").is_dir():
        return fail("缺 ui/tests", "至少覆盖「清单与结论面板不漏完整号码」")
    passed, summary = ctx.run_tests("ui/tests")
    return ok(summary) if passed else fail(summary, "看具体失败用例")


@check("UI-04", "K-11", "数据源直接吃契约真值，不另造 mock", gate=("W8",))
def datasource(ctx: Context) -> Result:
    p = ctx.ui_dir / "datasource.py"
    if not p.is_file():
        return skip("没有 datasource.py", "若界面直接读 labels 也算达标，但集成时要改")
    src = ctx.read_text(p)
    hits = [k for k in ("labels", "ImageDescriptor", "Detection") if k in src]
    if hits:
        return ok(f"引用 {hits}，换真检测器只需改这一个模块")
    return partial("没看到读契约记录的痕迹", "确认数据来源，避免 W5 集成时再造一套")


# 用一个很小的脚本在子进程里真跑一遍：界面能不能点，取决于这些纯函数对不对，
# 而不是取决于文件在不在。合成数据放在脚本里，不依赖 dataset（新克隆也能跑）。
_PROBE = r'''
import json, sys
sys.path.insert(0, ".")
from ui import decision

findings = [
    {"id": "a", "kind": "text", "subtype": "phone", "risk": 2, "bbox": [1, 2, 3, 4]},
    {"id": "b", "kind": "qr", "subtype": "qr_wifi", "risk": 3, "bbox": [5, 6, 7, 8]},
    {"id": "m", "kind": "meta", "subtype": "gps", "risk": 3, "bbox": None},
]
first = decision.build_decisions(findings, ["a", "b"])
second = decision.build_decisions(findings, ["a"], decision.index_decisions(first))
box = decision.make_manual_box(-5, -5, 100, 50, width=80, height=60, seq=1)
payload = decision.export_payload("img-1", first, [box])
moved = dict(box, bbox=[box["bbox"][0] + 1] + box["bbox"][1:])
print(json.dumps({"first": first, "second": second, "box": box,
                  "payload_keys": list(payload),
                  "boxes_rev": [decision.boxes_revision([box]),
                                decision.boxes_revision([moved]),
                                decision.boxes_revision([])]},
                 ensure_ascii=False))
'''


def _probe(ctx: Context):
    import json
    import subprocess
    import sys

    python = ctx.root / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = __import__("pathlib").Path(sys.executable)
    proc = subprocess.run([str(python), "-c", _PROBE], cwd=str(ctx.root),
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout).strip()[-200:]
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1]), None
    except Exception as exc:  # noqa: BLE001
        return None, f"探针输出无法解析：{exc}"


@check("UI-05", "K-11", "能生成 Decision（全局版次）且补框有内容版次", gate=("W6", "W8"))
def decision_builder(ctx: Context) -> Result:
    """K-11 W6 验收：勾选生成 redact、取消生成 retain、每项最多一条、改版是全局的。

    这条以前没有，导致 K-11 显示 4/4 而处置页其实是空的——**只查结构的检查会给人
    已经做完的错觉**，比没有检查更危险。

    2026-11-07 补第二段：B 指出补框变化 Decision 的 revision 看不出来，
    他要判断"这份包和上一份是不是同一批框"只能靠 `manual_boxes_revision`。
    """
    if not (ctx.ui_dir / "decision.py").is_file():
        return fail("没有 ui/decision.py", "K-11 W6 要求能生成契约 Decision")
    data, err = _probe(ctx)
    if data is None:
        return fail(f"探针跑不起来：{err}", "先跑 python ui/app.py 定位")

    first = {r["finding_id"]: r for r in data["first"]}
    if len(first) != 2:
        return fail(f"可决定项应为 2（meta 不参与），实际 {len(first)}",
                    "契约：元数据不接受 Decision")
    if any(r["action"] != "redact" or r["revision"] != 1 for r in first.values()):
        return fail("首次生成应为 redact + revision=1", "检查 build_decisions 的默认动作")

    second = {r["finding_id"]: r for r in data["second"]}
    # 2026-11-07 起 revision 是**全局版次**：任一决定变化，全部 Decision 一起升。
    # 混合 revision 会被 B 的 validate_records 直接拒（他 11-07 报的四项阻塞之一）。
    revisions = {r["revision"] for r in second.values()}
    if revisions != {2}:
        return fail(f"改动后全部决定应共享 revision=2，实际 {sorted(revisions)}",
                    "全局版次：混合 revision 会被 validate_records 拒")
    if second["a"]["action"] != "redact":
        return fail(f"未变的 a 应保持 redact，实际 {second['a']['action']}",
                    "版次变了不代表动作变了")
    if second["b"]["action"] != "retain":
        return fail(f"取消勾选后应为 retain，实际 {second['b']['action']}",
                    "检查 build_decisions 的动作映射")

    # 补框版次：B 拿到包要能判断"框有没有变"
    if "manual_boxes_revision" not in data["payload_keys"]:
        return fail("交接包没有 manual_boxes_revision",
                    "补框变化 Decision 的 revision 看不出来，B 无法判断批次")
    same, moved, empty = data["boxes_rev"]
    if same == moved:
        return fail("框坐标改了但版次没变", "boxes_revision 必须覆盖坐标")
    if same == empty:
        return fail("有框和没框的版次相同", "空集也要有确定的值")
    return ok("2 项决定；取消一项 → 全部升到 revision 2（全局版次）；补框版次随坐标变化")


@check("UI-06", "K-11", "手动补框可加入且越界坐标被钳制", gate=("W8",))
def manual_box(ctx: Context) -> Result:
    """K-11 W7：「能手动补一个框并处置」。补框是 finding_id=null 的计划区域，不是 Detection。"""
    if not (ctx.ui_dir / "decision.py").is_file():
        return fail("没有 ui/decision.py", "手动补框的校验逻辑应可单测，别埋在回调里")
    data, err = _probe(ctx)
    if data is None:
        return fail(f"探针跑不起来：{err}", "先跑 python ui/app.py 定位")
    box = data["box"]
    if not box["id"].startswith("manual-"):
        return fail(f"补框 id 应为 manual- 前缀，实际 {box['id']}", "别伪装成 Detection")
    # 输入 (-5,-5,100,50) 对 80×60 的图：起点越界要同时缩短宽高（保留实际框住的部分），
    # 所以宽 100-5=95 → 被图宽截到 80；高 50-5=45。不是 [0,0,80,50]。
    if box["bbox"] != [0, 0, 80, 45]:
        return fail(f"负坐标应被钳到 [0,0,80,45]，实际 {box['bbox']}",
                    "越界框交给处置器会切出负数索引；只归零不缩宽高会多遮")
    return ok("补框 id=manual-001，负坐标已钳进图内")
