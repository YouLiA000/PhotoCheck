"""PhotoCheck 界面（K-11）。

三页各对应一段流程：
  检测（W3）：选样本/上传 → 画框 + 掩码清单 + 整图风险（K-10）
  处置（W6/W7）：勾选生成 Decision（revision 递增）+ 手动补框 → 导出给 B
  结论（K-12）：等级、计数、复检两条结论

**处置页只生成"决定"，不涂像素。** Decision 是契约里 C 侧的产物，不依赖 B 的任何代码；
把图真正涂掉是 B 的 `redact(image, plan)`。之前把整页留空是过度保守——决定本来就能先做，
B 拿不到真实输入反而只能自己造测试数据。

启动：
    python ui/app.py                  # http://127.0.0.1:7860
    python -m ui.app                  # 同上
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import gradio as gr  # noqa: E402
from PIL import Image  # noqa: E402

from ui import datasource, decision  # noqa: E402

HEADERS = ["#", "类别", "子类", "分值", "来源", "掩码后内容", "位置 (x,y,w×h)"]

DISPOSAL_NOTE = """
**勾选 = 脱敏，取消 = 保留。** 元数据项不在这里——整张图的元数据始终清除，不用也不能选。

改完勾选再点一次「生成决定」，**只有变化的项会递增 `revision`**，没变的不动。
旧复检报告随 revision 变化立即失效（契约 §0.2）。
"""

EXPORT_NOTE = """
导出的文件里有两样东西：

- `decisions`：合法的契约 v4 `Decision` 记录，B 可直接消费；
- `manual_boxes`：手动补框的坐标，**不是 Detection**，B 转成 `finding_id=null` 的计划区域。

单独拿这个文件去 `validate_records` 会报 `redact_without_plan_item`——那是**预期**的：
计划区域归 B（K-08），在 B 生成计划之前，链路本来就是半成品。
"""


def _resolve(sample_id: str | None, upload: str | None):
    """返回 (image_id, 图路径, 记录列表)。上传的图没有检测结果，返回空记录。"""
    if upload:
        return None, Path(upload), []
    if sample_id:
        return sample_id, datasource.image_path(sample_id), datasource.load_records(sample_id)
    return None, None, []


def detect(sample_id, upload, progress=gr.Progress()):
    """一次点击的完整结果：(图, 清单, 状态, 结论面板)。

    面板与清单用**同一份 records** 生成。之前 on_run 又读了一遍文件——多一次 IO 是小事，
    两次读出来的东西可能不一致才是问题（文件被改、或解析路径不同）。
    """
    progress(0.05, desc="载入图像")
    try:
        image_id, path, records = _resolve(sample_id, upload)
    except (OSError, ValueError) as exc:
        message = f"### 无法读取样本\n\n{exc}"
        return None, [], message, message
    if path is None:
        return None, [], "请从下拉框选一张数据集样本，或上传一张图片。", ""

    progress(0.4, desc="读取检测结果")
    try:
        with Image.open(path) as im:
            base = im.convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError):
        message = (
            "### 无法读取图片\n\n"
            "文件可能不是图片、已损坏，或像素尺寸超出安全上限。请换一张图片。"
        )
        return None, [], message, message

    if not records:
        progress(1.0, desc="完成")
        message = (
            "### 上传的图暂时不会检测\n\n"
            "检测器由 A 提供、B 负责集成（W8）。**当前版本只能用数据集样本演示**——"
            "它们的真值就是契约 v4 的 Detection，可以直接走完「检测 → 处置 → 结论」全流程。\n\n"
            "**请在上方下拉框里选一个样本。**"
        )
        return base, [], message, message

    progress(0.75, desc="绘制检测框")
    drawn = datasource.draw_boxes(base, records)
    rows = datasource.display_rows(records)
    exp = datasource.severity(records)

    progress(1.0, desc="完成")
    status = (
        f"**{exp.level}风险 · {exp.score} 分** ｜ 发现 {len(rows)} 项"
        f" ｜ 图像 {base.width}×{base.height}"
        + (f" ｜ `{image_id}`" if image_id else "")
        + "\n\n→ 下一步：切到「处置」页，勾选要擦掉的内容。"
    )
    if exp.bonuses:
        status += "\n\n" + "；".join(name for name, _ in exp.bonuses)
    if exp.mismatches:
        # A 填的 risk 与规则不一致：界面按规则显示，但必须让人看见，否则联调时查不出来
        status += (
            "\n\n> ⚠ 检测器自带 risk 与规则不一致，已按规则显示："
            + "、".join(f"`{i}` {d}→{c}" for i, d, c in exp.mismatches)
        )
    return drawn, rows, status, datasource.panel_markdown(records)


# --------------------------------------------------------------------------
# 处置页：Decision 生成 + 手动补框（K-11 · W6/W7）
# --------------------------------------------------------------------------

DECISION_HEADERS = ["finding_id", "动作", "revision"]
BOX_HEADERS = ["#", "id", "x", "y", "w", "h", "方式"]


def _preview(finding) -> str:
    details = finding.get("details") or {}
    return details.get("masked_preview") or details.get("masked_summary") or "—"


def load_choices(sample_id):
    """按样本列出可决定的发现项，默认全选（= 全部脱敏）。"""
    if not sample_id:
        return gr.update(choices=[], value=[]), "先选一张样本。"
    findings = decision.eligible(datasource.detections(datasource.load_records(sample_id)))
    if not findings:
        return gr.update(choices=[], value=[]), (
            "这张图没有需要决定的对象（元数据不参与决定——整图元数据始终清除）。"
        )
    choices = [decision.choice_label(f, _preview) for f in findings]
    # 带上 image_id：处置页原来不显示"在处理哪张图"，换过样本后容易误以为还是上一张
    return (gr.update(choices=choices, value=[f["id"] for f in findings]),
            f"当前样本 **{sample_id}** ｜ 共 **{len(findings)}** 项可决定。默认全选 = 全部脱敏。")


def apply_decisions(sample_id, selected, previous):
    """生成/更新 Decision。只有动作变化的项才递增 revision。"""
    if not sample_id:
        return [], "先选一张样本。", {}
    findings = datasource.detections(datasource.load_records(sample_id))
    prev = previous or {}
    recs = decision.build_decisions(findings, selected or [], previous=prev)
    state = decision.index_decisions(recs)
    stat = decision.summarize(recs)
    changed = sum(
        1 for r in recs
        if int((prev.get(r["finding_id"]) or {}).get("revision") or 0) != r["revision"]
    )
    rows = [[r["finding_id"], r["action"], r["revision"]] for r in recs]
    message = (
        f"**{stat['total']} 条决定**：脱敏 {stat['redact']}、保留 {stat['retain']}，"
        f"最高 revision {stat['max_revision']}。本轮有 **{changed}** 条发生变化。"
    )
    if changed == 0:
        message += "（勾选没变，revision 不动——这是对的）"
    return rows, message, state


def switch_sample(sample_id):
    """换样本时把三个 state 和两张表一起清掉。

    不清的后果不是"显示脏数据"这么轻：`prev_state` 里存着上一张图的决定，
    而导出按钮读的是 state 而不是当前样本——换图后直接点导出，会把 A 图的决定
    挂到 B 图的 image_id 上发给 B。这是**错误数据**，不是界面瑕疵。
    """
    picker, hint = load_choices(sample_id)
    return picker, hint, [], "尚未生成决定。", {}, [], [], ""


def add_box(sample_id, x, y, w, h, boxes):
    """加入一个手动补框。坐标先钳进图内，退化框直接拒绝。"""
    boxes = list(boxes or [])
    if not sample_id:
        return gr.update(), boxes, "先选一张样本（需要它的尺寸来校验坐标）。"
    with Image.open(datasource.image_path(sample_id)) as im:
        width, height = im.size
    try:
        box = decision.make_manual_box(x, y, w, h, width=width, height=height,
                                       seq=len(boxes) + 1)
    except ValueError as exc:
        return gr.update(), boxes, f"⚠ {exc}"
    boxes.append(box)
    rows = [[i, b["id"], *b["bbox"], b["method"]] for i, b in enumerate(boxes, start=1)]
    return rows, boxes, f"已加入 {len(boxes)} 个补框（图 {width}×{height}）。"


def clear_boxes():
    return [], [], "已清空补框。"


def _write_unique_json(directory: Path, stem: str, payload: dict) -> str:
    """独占创建文件，同名时递增后缀，避免同秒导出覆盖上一条决定。"""
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    for seq in range(1, 1001):
        suffix = "" if seq == 1 else f"-{seq:02d}"
        path = directory / f"{stem}{suffix}.json"
        try:
            with path.open("x", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        except FileExistsError:
            continue
        return str(path)
    raise RuntimeError(f"导出目录中同名文件过多：{stem}")


def export_pack(sample_id, previous, boxes):
    """导出给 B：契约 Decision 记录 + 补框坐标。

    导出前按**当前样本的 finding 集合**过滤一遍。界面已经把换样本时的 state 清了，
    但过滤仍然要做——它是最后一道，挡住"状态没清干净"或"脚本直接调"的情况。
    """
    if not sample_id:
        return None
    findings = datasource.detections(datasource.load_records(sample_id))
    decisions, dropped = decision.filter_for_findings(list((previous or {}).values()), findings)
    boxes = list(boxes or [])
    if not decisions and not boxes:
        return None
    payload = decision.export_payload(sample_id, decisions, boxes)
    if dropped:
        # 不静默丢弃：数量写进文件，B 看到才知道界面那头状态不对
        payload["dropped_foreign_decisions"] = dropped
    out_dir = ROOT / "results" / "decisions"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{sample_id}-{datetime.now():%Y%m%d-%H%M%S}"
    return _write_unique_json(out_dir, stem, payload)


def build() -> gr.Blocks:
    samples = datasource.list_samples()
    with gr.Blocks(title="PhotoCheck · 照片隐私体检") as demo:
        gr.Markdown(
            "# PhotoCheck · 照片隐私体检\n"
            "输入一张图，找出位置明确、可自动检测的隐私线索。"
            "**只陈述检出结果，不做安全承诺。**"
        )

        # 样本选择移到 Tab 外：原来「检测」「处置」各有一个下拉框，用户在检测页换了样本、
        # 切到处置页还是旧的那张。而导出读的是 state 不是下拉框——换图后直接点导出，
        # 会把 A 图的决定挂到 B 图的 image_id 上发给 B。**一个样本只该有一处选择。**
        with gr.Row():
            sample = gr.Dropdown(
                choices=samples,
                value=samples[0] if samples else None,
                label="当前样本（检测与处置共用）",
                # 43 个样本，不给筛选就得滚三屏才能找到某个编号
                filterable=True,
                info=f"共 {len(samples)} 张 · 输入编号（如 012）或场景词可筛选",
                scale=3,
            )
            upload = gr.Image(
                type="filepath",
                label="或上传图片（优先于上方样本；检测器待 W8 集成，现无结果）",
                scale=2,
                height=130,
            )

        with gr.Tab("检测"):
            run = gr.Button("开始检测", variant="primary")
            # 限高：K-11 验收要求 1280×800 不滚动看全关键控件，
            # 数据集里的单据是 1120×1560 的竖图，不限高会把清单顶到屏幕外
            out_img = gr.Image(label="检测结果", interactive=False, height=360)
            table = gr.Dataframe(
                headers=HEADERS, value=[], label="发现项清单（掩码展示）",
                interactive=False, wrap=True,
            )
            status = gr.Markdown("选择上方样本后点「开始检测」。")

        with gr.Tab("处置"):
            gr.Markdown(DISPOSAL_NOTE)
            with gr.Row():
                with gr.Column(scale=1):
                    d_hint = gr.Markdown("当前样本的可决定项（样本在上方切换）。")
                    picker = gr.CheckboxGroup(choices=[], label="勾选 = 脱敏 ／ 取消 = 保留")
                    d_apply = gr.Button("生成决定", variant="primary")
                with gr.Column(scale=2):
                    d_summary = gr.Markdown("尚未生成决定。")
                    d_table = gr.Dataframe(
                        headers=DECISION_HEADERS, value=[], interactive=False, wrap=True,
                        label="当前决定（每项最多一条）",
                    )
                    gr.Markdown("#### 手动补框（检测漏掉的区域）")
                    with gr.Row():
                        bx = gr.Number(value=0, precision=0, label="x")
                        by = gr.Number(value=0, precision=0, label="y")
                        bw = gr.Number(value=200, precision=0, label="宽")
                        bh = gr.Number(value=80, precision=0, label="高")
                    with gr.Row():
                        b_add = gr.Button("加入补框")
                        b_clear = gr.Button("清空")
                    b_msg = gr.Markdown("")
                    b_table = gr.Dataframe(
                        headers=BOX_HEADERS, value=[], interactive=False, wrap=True,
                        label="补框清单",
                    )
                    gr.Markdown(EXPORT_NOTE)
                    d_export = gr.DownloadButton("导出给 B（decisions + 补框）")

            prev_state = gr.State({})
            boxes_state = gr.State([])

        with gr.Tab("结论"):
            gr.Markdown(
                "选择样本并点「开始检测」后，这里给出结论面板。\n\n"
                "**要导出给 B 的决定？** 去「处置」页勾选内容 → 点「生成决定」→ "
                "底部有下载按钮。结论页只读，不放导出。"
            )
            panel = gr.Markdown()

        outputs = [out_img, table, status, panel]

        # 换样本 = 三件事一起做：清掉处置页的旧决定、重载可勾选项、刷新检测结果。
        # 原来只清了处置页，检测页停在旧图，两个 Tab 显示的不是同一张。
        sample.change(
            switch_sample, inputs=[sample],
            outputs=[picker, d_hint, d_table, d_summary,
                     prev_state, boxes_state, b_table, b_msg],
        ).then(detect, inputs=[sample, upload], outputs=outputs)

        run.click(detect, inputs=[sample, upload], outputs=outputs)
        d_apply.click(apply_decisions, inputs=[sample, picker, prev_state],
                      outputs=[d_table, d_summary, prev_state])
        b_add.click(add_box, inputs=[sample, bx, by, bw, bh, boxes_state],
                    outputs=[b_table, boxes_state, b_msg])
        b_clear.click(clear_boxes, outputs=[b_table, boxes_state, b_msg])
        d_export.click(export_pack, inputs=[sample, prev_state, boxes_state],
                       outputs=[d_export])

        # 打开页面就跑第一张：空表格 + "等待开始"看着像坏了，演示时还得先解释一句
        demo.load(detect, inputs=[sample, upload], outputs=outputs)
        demo.load(load_choices, inputs=[sample], outputs=[picker, d_hint])

    return demo


if __name__ == "__main__":
    app = build()
    app.launch(server_name="127.0.0.1", server_port=7860, inbrowser=False, show_api=False)
