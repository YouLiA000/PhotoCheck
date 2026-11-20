"""Build a self-contained HTML contact sheet for the layer-3 review.

Why this exists: the review material was handed over as a zip, and a zip cannot
be previewed - it only downloads. A reviewer who "cannot open it" is blocked on
something unrelated to the actual task, so this writes one HTML file with every
comparison image embedded as base64. No relative paths, no server, no unzip.

Re-run:
    python scripts/make_layer3_html.py
"""

from __future__ import annotations

import argparse
import base64
import html
import io
import json
from pathlib import Path

from PIL import Image

THUMB_MAX_W = 1200
JPEG_Q = 82


def _embed(path: Path) -> tuple[str, int, int]:
    """Downscale and return (data_uri, w, h). Downscaling matters: the raw PNGs
    are ~6.8 MB together, and base64 inflates by a third."""
    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        if w > THUMB_MAX_W:
            h = round(h * THUMB_MAX_W / w)
            w = THUMB_MAX_W
            im = im.resize((w, h), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=JPEG_Q, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}", w, h


def build(review_dir: Path, out: Path, dataset: Path | None) -> None:
    summary = json.loads((review_dir / "summary.json").read_text(encoding="utf-8"))
    entries = summary["entries"]

    cards = []
    total_bytes = 0
    for e in entries:
        png = review_dir / e["compare_image"]
        if not png.is_file():
            png = review_dir / f"{e['image_id']}.png"
        if not png.is_file():
            print(f"  [skip] missing image for {e['image_id']}")
            continue
        uri, w, h = _embed(png)
        total_bytes += len(uri)
        ratio = float(e.get("changed_pixel_ratio") or 0)
        kind = e.get("change_kind") or ("none" if ratio < 0.005 else "redacted")
        state = e.get("disposal_state") or ""
        cls = "none" if kind == "none" else ("big" if ratio >= 0.10 else "mid")
        cards.append(
            f"""<figure class="card {cls}" data-ratio="{ratio:.4f}" data-kind="{kind}">
  <figcaption>
    <span class="id">{html.escape(e['image_id'])}</span>
    <span class="tags">
      <span class="tag {cls}">改动 {ratio:.2%}</span>
      <span class="tag state {html.escape(state)}">{html.escape(state)}</span>
      <span class="tag">遮 {e.get('redacted_regions', 0)} 处</span>
      <span class="tag">检出 {e.get('detections', 0)}</span>
      <span class="tag">残留 {e.get('residual_count', 0)}</span>
    </span>
  </figcaption>
  <img src="{uri}" width="{w}" height="{h}" loading="lazy"
       alt="{html.escape(e['image_id'])} 左原图 右导出" onclick="zoom(this)">
</figure>"""
        )

    bad = [
        ("image-dev-025", "全黑图（std=0），无内容可遮"),
        ("image-dev-026", "16×16 纯白，尺寸过小"),
        ("image-dev-027", "文件损坏，PIL 无法识别（应抛 ImageIngestError）"),
        ("image-dev-039", "4000×200 极窄长条，无正常单据布局"),
        ("image-dev-040", "文件被截断（应抛 ImageIngestError）"),
    ]
    bad_rows = "\n".join(
        f"<tr><td><code>{k}</code></td><td>{html.escape(v)}</td></tr>" for k, v in bad
    )

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>第三层判读素材 · {len(cards)} 张对比图</title>
<style>
  :root {{
    --bg:#f6f7f9; --panel:#fff; --ink:#1a1d21; --muted:#6b7280;
    --line:#e3e6ea; --accent:#2563eb; --warn:#b45309; --danger:#b91c1c;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:28px 32px 72px; background:var(--bg); color:var(--ink);
    font:15px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }}
  h1 {{ font-size:22px; margin:0 0 6px; }}
  .lede {{ color:var(--muted); margin:0 0 20px; }}
  .bar {{ position:sticky; top:0; z-index:5; background:rgba(246,247,249,.94);
    backdrop-filter:blur(6px); padding:12px 0; margin:0 0 18px; border-bottom:1px solid var(--line);
    display:flex; gap:8px; flex-wrap:wrap; align-items:center; }}
  .bar button {{ font:inherit; font-size:13px; padding:5px 13px; border-radius:999px;
    border:1px solid var(--line); background:var(--panel); color:var(--ink); cursor:pointer; }}
  .bar button[aria-pressed="true"] {{ background:var(--accent); border-color:var(--accent); color:#fff; }}
  .bar .count {{ margin-left:auto; color:var(--muted); font-size:13px; }}
  .notes {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
    padding:16px 20px; margin:0 0 22px; }}
  .notes h2 {{ font-size:15px; margin:0 0 8px; }}
  .notes ol {{ margin:0; padding-left:20px; }}
  .notes li {{ margin:4px 0; }}
  .notes table {{ border-collapse:collapse; margin-top:10px; font-size:13px; }}
  .notes td {{ border-top:1px solid var(--line); padding:5px 12px 5px 0; vertical-align:top; }}
  .grid {{ display:grid; gap:18px; grid-template-columns:repeat(auto-fill,minmax(520px,1fr)); }}
  .card {{ margin:0; background:var(--panel); border:1px solid var(--line);
    border-radius:10px; overflow:hidden; }}
  .card.big {{ border-color:#f0b429; box-shadow:0 0 0 2px #fef3c7; }}
  figcaption {{ display:flex; gap:10px; align-items:center; padding:9px 13px;
    border-bottom:1px solid var(--line); flex-wrap:wrap; }}
  .id {{ font-weight:600; font-size:13.5px; font-family:ui-monospace,Consolas,monospace; }}
  .tags {{ margin-left:auto; display:flex; gap:6px; flex-wrap:wrap; }}
  .tag {{ font-size:11.5px; padding:2px 8px; border-radius:999px;
    background:#eef1f5; color:#475569; white-space:nowrap; }}
  .tag.big {{ background:#fef3c7; color:var(--warn); font-weight:600; }}
  .tag.mid {{ background:#e0f2fe; color:#0369a1; }}
  .tag.none {{ background:#f1f5f9; color:#94a3b8; }}
  .tag.state.passed {{ background:#dcfce7; color:#15803d; }}
  .tag.state.not_applicable {{ background:#f1f5f9; color:#64748b; }}
  .card img {{ display:block; width:100%; height:auto; cursor:zoom-in; background:#fff; }}
  .hidden {{ display:none !important; }}
  #lb {{ position:fixed; inset:0; background:rgba(15,18,22,.92); display:none;
    align-items:center; justify-content:center; z-index:50; cursor:zoom-out; padding:24px; }}
  #lb.on {{ display:flex; }}
  #lb img {{ max-width:100%; max-height:100%; }}
  footer {{ margin-top:36px; color:var(--muted); font-size:13px; }}
</style>
</head>
<body>

<h1>第三层判读素材 · {len(cards)} 张对比图</h1>
<p class="lede">每张图 <b>左 = 原图</b>，<b>右 = App 真实导出的脱敏图</b>。点击图片可放大。</p>

<div class="bar">
  <button data-f="all" aria-pressed="true">全部 {len(cards)}</button>
  <button data-f="big" aria-pressed="false">改动 ≥10%</button>
  <button data-f="mid" aria-pressed="false">改动 0.5–10%</button>
  <button data-f="none" aria-pressed="false">无改动</button>
  <span class="count" id="count"></span>
</div>

<div class="notes">
  <h2>判读前必须知道的两件事</h2>
  <ol>
    <li><b>“无改动”不是漏遮。</b>这几张是干净负例（<code>处置状态 = not_applicable</code>），
        本来就没有可遮内容；像素仍有 0.01–0.04% 差异，是 <b>JPEG 重编码噪声</b>。
        0.5% 以下一律记为 none，不要去追一个不存在的框。</li>
    <li><b>姓名、单位、邮箱、地址没被遮，不是缺陷。</b>A 的检测范围只有五类
        （手机号 / 身份证 / 二维码 / 人脸 / 元数据）。姓名与地址不在其中，
        判读时不要记成“欠遮”——那是规格范围问题，要改的是规格卡，不是检测器。</li>
  </ol>
  <p style="margin:12px 0 0;color:#6b7280;font-size:13px;">
    判读结果请填 <code>判读表.csv</code>，两名判读人<b>各自独立</b>填，不要看对方的。
  </p>
  <table>
    <tr><td colspan="2" style="border:0;padding-left:0;color:#6b7280;font-size:12.5px;">
      以下 5 张不在判读表内（dev 集共 43 张，其中 5 张为故意构造的坏图、无标签）：</td></tr>
    {bad_rows}
  </table>
</div>

<div class="grid" id="grid">
{chr(10).join(cards)}
</div>

<footer>
  生成自 <code>scripts/make_layer3_html.py</code> · 图为 JPEG q{JPEG_Q} 缩略（原图见
  <code>reports/layer3_review/</code>）· 判读列留空是本设计的一部分，脚本填上就是假通过。
</footer>

<div id="lb" onclick="this.classList.remove('on')"><img alt=""></div>

<script>
const cards = [...document.querySelectorAll('.card')];
const countEl = document.getElementById('count');
function apply(f) {{
  let n = 0;
  cards.forEach(c => {{
    const r = parseFloat(c.dataset.ratio);
    const show = f === 'all' || (f === 'big' && r >= 0.10)
      || (f === 'mid' && r >= 0.005 && r < 0.10) || (f === 'none' && r < 0.005);
    c.classList.toggle('hidden', !show);
    if (show) n++;
  }});
  countEl.textContent = `显示 ${{n}} / ${{cards.length}} 张`;
}}
document.querySelectorAll('.bar button').forEach(b => b.addEventListener('click', () => {{
  document.querySelectorAll('.bar button').forEach(x => x.setAttribute('aria-pressed', 'false'));
  b.setAttribute('aria-pressed', 'true');
  apply(b.dataset.f);
}}));
function zoom(img) {{
  const lb = document.getElementById('lb');
  lb.querySelector('img').src = img.src;
  lb.classList.add('on');
}}
document.addEventListener('keydown', e => {{
  if (e.key === 'Escape') document.getElementById('lb').classList.remove('on');
}});
apply('all');
</script>
</body>
</html>
"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    print(f"wrote {out}  ({out.stat().st_size / 1e6:.2f} MB, {len(cards)} images)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the layer-3 HTML contact sheet")
    here = Path(__file__).resolve().parent.parent
    ap.add_argument("--review-dir", default=str(here / "reports" / "layer3_review"))
    ap.add_argument("--out", default=None, help="default: <review-dir>/index.html")
    args = ap.parse_args(argv)

    review = Path(args.review_dir)
    out = Path(args.out) if args.out else review / "index.html"
    build(review, out, None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
