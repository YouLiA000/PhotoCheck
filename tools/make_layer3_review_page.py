#!/usr/bin/env python3
"""把第三层判读表 + 遮盖后导出图拼成一页可点选的 HTML。

为什么不用 CSV 手填：
第三层每行要填两列（affects_reading 0/1、over_redacted 整数），38 行 = 76 个格子。
手填容易串行、漏行、改错列；而判读表一旦错行，后面的 JSON 和结论全废。
所以做成：图上点一下 0/1、数字框填误遮数、最后一个按钮导出**完整 CSV**（含原有列），
导出的文件可直接喂给
`tools/make_layer3_sheet.py --from-image-csv ... --layer2 ...`。第二层 JSON 用于
核对整图集合与每图 RedactionPlan 数量，不能省略。

用法：
    python tools/make_layer3_review_page.py \
        --csv docs/eval-reports/layer3_review_38.csv \
        --report results/layer3-exports/integration-report.json \
        --out results/layer3-exports/review.html
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_mapping(report_path: Path) -> dict[str, str]:
    """image_id -> 导出图文件名（只取文件名，页面按相对路径引用）。"""
    data = json.loads(report_path.read_text(encoding="utf-8"))
    mapping: dict[str, str] = {}
    for item in data.get("processed") or []:
        output = (item.get("result") or {}).get("output")
        if not output:
            continue
        mapping[item["image_id"]] = Path(str(output).replace("\\", "/")).name
    return mapping


def build(rows: list[dict], mapping: dict[str, str]) -> str:
    payload = []
    for row in rows:
        image_id = row["image_id"]
        payload.append({
            "image_id": image_id,
            "file": mapping.get(image_id, ""),
            "state": row.get("state", ""),
            "change_pct": row.get("change_pct", ""),
            "redactions": row.get("redactions", ""),
            "detections": row.get("detections", ""),
            "residuals": row.get("residuals", ""),
            "affects_reading": row.get("affects_reading", ""),
            "over_redacted": row.get("over_redacted", ""),
            "note": row.get("note", ""),
        })
    columns = list(rows[0].keys())
    return TEMPLATE.replace("__ROWS__", json.dumps(payload, ensure_ascii=False)) \
                   .replace("__COLS__", json.dumps(columns, ensure_ascii=False)) \
                   .replace("__TITLE__", f"第三层判读 · {len(payload)} 张")


TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>__TITLE__</title>
<style>
  body{font-family:"Microsoft YaHei",system-ui,sans-serif;margin:0;padding:16px;background:#f5f6f8;color:#111}
  header{position:sticky;top:0;background:#fff;border-bottom:1px solid #ddd;padding:12px 16px;z-index:9}
  h1{font-size:18px;margin:0 0 6px}
  .hint{font-size:13px;color:#555;line-height:1.6}
  .bar{margin-top:8px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
  button{cursor:pointer;border:1px solid #bbb;background:#fff;border-radius:6px;padding:8px 14px;font-size:14px}
  button.primary{background:#1a73e8;color:#fff;border-color:#1a73e8}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(620px,1fr));gap:14px;margin-top:14px}
  .card{background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:12px}
  .card.done{border-color:#34a853;box-shadow:0 0 0 2px #e6f4ea inset}
  .id{font-weight:600;font-size:14px}
  .meta{font-size:12px;color:#666;margin:2px 0 8px}
  img{width:100%;border:1px solid #eee;border-radius:4px;background:#fafafa;cursor:zoom-in}
  .ctl{display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap}
  .seg button{padding:6px 16px}
  .seg button.on{background:#1a73e8;color:#fff;border-color:#1a73e8}
  input[type=number]{width:80px;padding:6px;border:1px solid #bbb;border-radius:6px}
  input[type=text]{flex:1;min-width:120px;padding:6px;border:1px solid #bbb;border-radius:6px}
  .label{font-size:12px;color:#555}
</style></head><body>
<header>
  <h1>__TITLE__</h1>
  <div class="hint">
    判的是「<b>遮盖之后有没有影响非敏感内容的阅读</b>」，不是「遮得干不干净」。<br>
    每张都要选 0 或 1；误遮数填整数（没有就填 0）。全部填完后点右上「导出 CSV」，
    把下载的文件交给 C，同时提供对应 layer2 JSON，运行
    <code>make_layer3_sheet.py --from-image-csv ... --layer2 ...</code>。
  </div>
  <div class="bar">
    <span class="label">已填 <b id="done">0</b> / <span id="total">0</span></span>
    <button class="primary" onclick="exportCsv()">导出 CSV</button>
    <span class="label" id="tip"></span>
  </div>
</header>
<div class="grid" id="grid"></div>
<script>
const ROWS = __ROWS__;
const COLS = __COLS__;
const state = {};
ROWS.forEach(r => { state[r.image_id] = {
  affects_reading: r.affects_reading || "",
  over_redacted: r.over_redacted || "",
  note: r.note || ""
};});

function refresh(){
  let n = 0;
  ROWS.forEach(r => {
    const s = state[r.image_id];
    const ok = (s.affects_reading === "0" || s.affects_reading === "1") && s.over_redacted !== "";
    document.getElementById("card-" + r.image_id).classList.toggle("done", ok);
    if (ok) n++;
  });
  document.getElementById("done").textContent = n;
}

function setA(id, v){
  state[id].affects_reading = v;
  document.querySelectorAll(`[data-seg="${id}"] button`).forEach(b =>
    b.classList.toggle("on", b.dataset.v === v));
  refresh();
}
function setO(id, v){ state[id].over_redacted = v.replace(/[^0-9]/g,""); refresh(); }
function setN(id, v){ state[id].note = v; }

function esc(v){ v = String(v==null?"":v); return /[",\\n]/.test(v) ? '"'+v.replace(/"/g,'""')+'"' : v; }

function exportCsv(){
  const lines = [COLS.join(",")];
  ROWS.forEach(r => {
    const s = state[r.image_id];
    lines.push(COLS.map(c => esc(c in s ? s[c] : r[c])).join(","));
  });
  const blob = new Blob(["\\ufeff" + lines.join("\\r\\n") + "\\r\\n"], {type:"text/csv;charset=utf-8"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "layer3_review_filled.csv";
  a.click();
  const missing = ROWS.filter(r => {
    const s = state[r.image_id];
    return !((s.affects_reading==="0"||s.affects_reading==="1") && s.over_redacted!=="");
  }).length;
  document.getElementById("tip").textContent = missing ? `还有 ${missing} 张没填完，导出的文件会被校验器拒绝` : "全部填完，可以导出";
}

const grid = document.getElementById("grid");
ROWS.forEach((r, i) => {
  const div = document.createElement("div");
  div.className = "card";
  div.id = "card-" + r.image_id;
  div.innerHTML = `
    <div class="id">${i+1}. ${r.image_id}</div>
    <div class="meta">state=${r.state} · 变化 ${r.change_pct}% · 遮盖 ${r.redactions} · 检出 ${r.detections} · 残留 ${r.residuals}</div>
    <img loading="lazy" src="${r.file}" alt="${r.image_id}" title="点击看原图" onclick="window.open(this.src,'_blank')">
    <div class="ctl">
      <span class="label">影响阅读</span>
      <span class="seg" data-seg="${r.image_id}">
        <button data-v="0" onclick="setA('${r.image_id}','0')">0 否</button>
        <button data-v="1" onclick="setA('${r.image_id}','1')">1 是</button>
      </span>
      <span class="label">误遮数</span>
      <input type="number" min="0" step="1" oninput="setO('${r.image_id}', this.value)">
    </div>
    <div class="ctl"><input type="text" placeholder="备注（可空）" oninput="setN('${r.image_id}', this.value)"></div>
  `;
  grid.appendChild(div);
});
document.getElementById("total").textContent = ROWS.length;
ROWS.forEach(r => {
  if (r.affects_reading === "0" || r.affects_reading === "1") setA(r.image_id, r.affects_reading);
  if (r.over_redacted !== "") setO(r.image_id, String(r.over_redacted));
});
refresh();
</script></body></html>
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成第三层判读页（含图、可点选、可导出 CSV）")
    ap.add_argument("--csv", required=True, help="layer3_review_38.csv")
    ap.add_argument("--report", required=True, help="run_c_dataset.py 的 integration-report.json")
    ap.add_argument("--out", required=True, help="输出 html")
    args = ap.parse_args(argv)

    csv_path, report_path = Path(args.csv), Path(args.report)
    if not csv_path.is_file() or not report_path.is_file():
        print(f"[error] 找不到 {csv_path} 或 {report_path}", file=sys.stderr)
        return 2

    with csv_path.open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        print("[error] 判读表为空", file=sys.stderr)
        return 2

    mapping = load_mapping(report_path)
    missing = [r["image_id"] for r in rows if not mapping.get(r["image_id"])]
    if missing:
        print(f"[error] 以下图没有对应导出文件：{missing}", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(rows, mapping), encoding="utf-8", newline="\n")
    print(f"[ok] {out}  共 {len(rows)} 张，图取自 {report_path.name}")
    print("[提示] 打开后逐张点 0/1、填误遮数，最后点「导出 CSV」")
    return 0


if __name__ == "__main__":
    sys.exit(main())
