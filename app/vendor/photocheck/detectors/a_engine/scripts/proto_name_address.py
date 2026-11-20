"""Prototype: find 姓名 / 地址 / 邮箱 on OCR blocks, and measure how often it is wrong.

Scope note: this is a feasibility probe, NOT a detector to ship. The point is to
answer one question before anyone commits to expanding the scope -

    how often does a rule-based name finder mistake 职务/单位/公司名 for a person?

The answer decides whether expanding the contract is a two-week job or a
two-month one, so it has to be measured rather than guessed.

Usage:
    python scripts/proto_name_address.py --images-dir <image dir> --out reports/proto_na
    python scripts/proto_name_address.py --ocr-cache <local cache.json> --out reports/proto_na

The generated OCR cache and candidate export are local-only. The candidate CSV
stores a text hash, never the recognised name/address/email text itself.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from pathlib import Path

# --- rules -----------------------------------------------------------------

# A label that a person's name sits next to. 收件人/寄件人 are the strong ones on
# waybills; 姓名/联系人 cover cards and forms.
NAME_KEYS = ("收件人", "寄件人", "姓名", "联系人", "签收人", "发货人")

# What a "name" block looks like on its own: 2-4 Chinese chars, optionally with a
# courtesy suffix. Nothing here can tell 张三 from 项目经理 - that is the whole
# problem, and why the adjacency rule exists.
PERSON = re.compile(r"^[\u4e00-\u9fa5]{2,4}(先生|女士|小姐|老师|经理)?$")

# Words that pass PERSON but are clearly not people. Hand-written from what the
# dev set actually contains; a real implementation would need a proper list or a
# model, which is itself part of the cost estimate.
NOT_A_PERSON = {
    "收件人", "寄件人", "发货人", "联系人", "签收人", "姓名",
    "联系电话", "收件地址", "证件号", "内件品名", "运单号", "订单号",
    "项目经理", "北辰数据", "中通速递", "顺丰速运", "圆通速递", "韵达快递",
    "文件", "合同", "备注", "客服", "扫码查询", "详情", "说明",
}

ADDR_KEY = re.compile(r"(地址|住址)\s*$")
ADDR_HEAD = re.compile(r"[\u4e00-\u9fa5]{2,}(省|市|自治区)")
ADDR_TAIL = re.compile(r"(区|县|镇|乡|街道|路|街|道|巷|号|号楼|单元|室|栋)")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

CONTEXT_TOL = 46  # px; blocks on one printed line sit within ~1 line height


def resolve_ocr_cache(explicit: str | None, here: Path) -> Path | None:
    """Resolve a local cache without assuming the ignored file still exists."""
    candidate = Path(explicit) if explicit else here / "reports" / "dev_ocr_cache.json"
    return candidate if candidate.is_file() else None


def _same_row(a, b, tol: int = CONTEXT_TOL) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return abs((ay + ah / 2) - (by + bh / 2)) < max(ah, bh, tol)


def _right_of(a, b) -> bool:
    """`a` is to the right of `b` (the value sits right of its label)."""
    return a[0] >= b[0]


def find(blocks: list[dict]) -> list[dict]:
    """Return candidate regions with the rule that fired, so the audit can trace them."""
    out: list[dict] = []
    taken: set[int] = set()

    for i, b in enumerate(blocks):
        text = b["text"].strip()

        if EMAIL.search(text):
            out.append({"kind": "email", "bbox": b["bbox"], "text": text,
                        "rule": "regex:email", "block": i})
            taken.add(i)

    for i, b in enumerate(blocks):
        text = b["text"].strip()

        # --- name: value block adjacent to a label block -------------------
        if i not in taken and PERSON.match(text) and text not in NOT_A_PERSON \
                and not any(c.isdigit() for c in text):
            for j, o in enumerate(blocks):
                if j == i:
                    continue
                if any(k in o["text"] for k in NAME_KEYS) and _same_row(b["bbox"], o["bbox"]) \
                        and _right_of(b["bbox"], o["bbox"]):
                    out.append({"kind": "name", "bbox": b["bbox"], "text": text,
                                "rule": f"adjacent-to:{o['text'][:8]}", "block": i})
                    taken.add(i)
                    break

        # --- address: a block carrying a province/city head ---------------
        # The first version also emitted the *label* block ("收件地址"), which on
        # the dev set was 8 of 24 candidates - a third of the output was the word
        # "address" rather than an address. Labels are context, not content: they
        # tell you where to look, they are not the thing to redact.
        if i not in taken and ADDR_HEAD.search(text) and not ADDR_KEY.search(text):
            out.append({"kind": "address", "bbox": b["bbox"], "text": text,
                        "rule": "regex:address-head", "block": i})
            taken.add(i)

        # --- address continuation: '3号楼502' under a street line ---------
        if i not in taken and ADDR_TAIL.search(text) and not ADDR_HEAD.search(text) \
                and len(text) <= 14 and not EMAIL.search(text):
            for j in (i - 1, i + 1):
                if 0 <= j < len(blocks) and j in taken:
                    prev = [o for o in out if o["block"] == j]
                    if prev and prev[0]["kind"] == "address" \
                            and abs(b["bbox"][1] - blocks[j]["bbox"][1]) < 110:
                        out.append({"kind": "address_tail", "bbox": b["bbox"], "text": text,
                                    "rule": "continues-address", "block": i})
                        taken.add(i)
                        break
    return out


# --- run -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description="Prototype name/address finder")
    ap.add_argument(
        "--ocr-cache",
        default=None,
        help="local OCR cache; the generated dev cache is intentionally not tracked",
    )
    ap.add_argument("--images-dir", default=None,
                    help="run OCR on this folder instead of using the cached dev blocks")
    ap.add_argument("--out", default=str(here / "reports" / "proto_na"))
    args = ap.parse_args(argv)

    if args.images_dir:
        from PIL import Image

        sys.path.insert(0, str(here))
        from core.detect import ocr as ocr_mod

        ocr_mod.warmup()
        cache = {}
        for f in sorted(Path(args.images_dir).glob("*")):
            if f.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                continue
            blocks = ocr_mod.detect_text_blocks(Image.open(f).convert("RGB"))
            cache[f.stem] = [{"bbox": list(b.bbox), "score": round(b.score, 3), "text": b.text}
                             for b in blocks]
            print(f"  ocr {f.name}: {len(blocks)} blocks", end="\r")
        print()
    else:
        cache_path = resolve_ocr_cache(args.ocr_cache, here)
        if cache_path is None:
            print(
                "缺少本地 OCR 缓存；原始 OCR 文本不会从仓库提供。请二选一：\n"
                "  1. 从图片重新运行：--images-dir <图片目录>\n"
                "  2. 指定本机生成的缓存：--ocr-cache <本地 JSON>",
                file=sys.stderr,
            )
            return 2
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"无法读取 OCR 缓存（{type(exc).__name__}）", file=sys.stderr)
            return 2
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    per_image: dict[str, dict[str, int]] = {}

    for iid, blocks in sorted(cache.items()):
        found = find(blocks)
        per_image[iid] = {}
        for f in found:
            per_image[iid][f["kind"]] = per_image[iid].get(f["kind"], 0) + 1
            rows.append({
                "image_id": iid,
                "kind": f["kind"],
                "bbox": json.dumps(f["bbox"]),
                "rule": f["rule"],
                "text_sha256": hashlib.sha256(f["text"].encode("utf-8")).hexdigest(),
            })

    with (outdir / "candidates.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=["image_id", "kind", "bbox", "rule", "text_sha256"],
        )
        w.writeheader()
        w.writerows(rows)

    # --- summary ---
    from collections import Counter

    by_kind = Counter(r["kind"] for r in rows)
    images_with = {k: sum(1 for v in per_image.values() if v.get(k)) for k in by_kind}

    print(f"38 张图，候选区域 {len(rows)} 个\n")
    print(f"{'kind':14s}{'区域数':>7s}{'涉及张数':>9s}")
    for k, v in by_kind.most_common():
        print(f"{k:14s}{v:7d}{images_with[k]:9d}")

    print(f"\n候选明细 -> {outdir / 'candidates.csv'}")
    print("\n--- 按图列出（前 10 张）---")
    for iid in list(per_image)[:10]:
        if per_image[iid]:
            print(f"  {iid}: {per_image[iid]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
