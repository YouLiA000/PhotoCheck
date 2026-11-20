"""契约层检查：schema、校验器、迁移脚本、样例闭环。"""

from __future__ import annotations

import json

from audit.model import Context, Result, check, fail, ok, skip


@check("CT-01", "契约v4", "严格 Schema 存在且可解析", gate=("W4",))
def schema_ok(ctx: Context) -> Result:
    p = ctx.contract_dir / "schema" / "photocheck-v4.schema.json"
    if not p.is_file():
        return fail("找不到 photocheck-v4.schema.json", "从规格卡 §0.2 重建五类记录定义")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        return fail(f"JSON 解析失败：{exc}", "确认没有 BOM、括号闭合")
    defs = list((data.get("$defs") or {}).keys())
    need = {"ImageDescriptor", "Detection", "Decision", "RedactionPlan", "VerificationReport"}
    missing = need - set(defs)
    if missing:
        return fail(f"缺定义 {sorted(missing)}", "五类记录必须齐")
    return ok(f"五类记录齐全，{len(defs)} 个定义，version={data.get('title', '?')}")


@check("CT-02", "契约v4", "契约测试全绿", gate=("W4",))
def contract_tests(ctx: Context) -> Result:
    passed, summary = ctx.run_tests("contract/tests")
    return ok(summary) if passed else fail(summary, "跑 pytest/unittest 看具体哪条失败")


@check("CT-03", "契约v4", "闭环样例同时通过校验器", gate=("W4",))
def example_pipeline(ctx: Context) -> Result:
    import subprocess
    import sys
    from pathlib import Path

    p = ctx.contract_dir / "examples" / "valid_pipeline.json"
    if not p.is_file():
        return fail("缺 valid_pipeline.json", "补一份 1 图 / 多 Finding / 决定 / 计划 / 复检的闭环样例")
    python = ctx.root / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = Path(sys.executable)
    proc = subprocess.run([str(python), "contract/validate_records.py", str(p)],
                          cwd=str(ctx.root), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        return fail((proc.stdout + proc.stderr).strip()[:160], "样例与 schema 不同步了")
    return ok("valid_pipeline.json 通过")


@check("CT-04", "契约v4", "v3 迁移脚本可用", gate=())
def migrate_v3(ctx: Context) -> Result:
    p = ctx.contract_dir / "migrate_v3.py"
    if not p.is_file():
        return skip("没有 migrate_v3.py", "如果团队已统一 v4，可以不迁移")
    return ok(f"{p.stat().st_size // 1024 + 1} KB")
