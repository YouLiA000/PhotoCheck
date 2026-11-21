"""PhotoCheck 进度审计 —— 核心模型。

三条设计原则：

1. **每条检查必须给出证据**，不能只说「通过」。没有证据的通过等于没查
   （像 lock 集 image_id 与 dev 重名那种事，所有校验都"通过"，但东西是错的）。
2. **每条检查绑定规格卡卡号**，报告按卡聚合，才能直接回答「K-13 达标了吗」。
3. **允许「无法判定」**：依赖 A / B 产出的检查不算 C 的失败，但要出现在报告里，
   否则「集成还差什么」永远看不见。
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable


class Status(str, Enum):
    PASS = "PASS"
    PARTIAL = "PARTIAL"
    FAIL = "FAIL"
    SKIP = "SKIP"


# 状态图标：**全项目唯一来源**。run.py 曾自己又定义了一份一模一样的 ICON，
# 两处改一处漏一处，控制台和 Markdown 两个输出就会不一致。
STATUS_ICON = {Status.PASS: "OK", Status.PARTIAL: "~~", Status.FAIL: "XX", Status.SKIP: "--"}


@dataclass
class Result:
    status: Status
    evidence: str
    hint: str = ""

    @property
    def ok(self) -> bool:
        return self.status is Status.PASS


def ok(evidence: str) -> Result:
    return Result(Status.PASS, evidence)


def partial(evidence: str, hint: str = "") -> Result:
    return Result(Status.PARTIAL, evidence, hint)


def fail(evidence: str, hint: str = "") -> Result:
    return Result(Status.FAIL, evidence, hint)


def skip(reason: str, hint: str = "") -> Result:
    return Result(Status.SKIP, reason, hint)


@dataclass
class Check:
    id: str
    card: str
    title: str
    level: str
    owner: str
    fn: Callable[["Context"], Result]
    gate: tuple[str, ...] = ()


REGISTRY: list[Check] = []


def check(cid: str, card: str, title: str, level: str = "P0",
          owner: str = "C", gate: tuple[str, ...] = ()):
    """注册一条检查。gate 填该检查属于哪些闸门，如 ("W6", "W8")。"""

    def deco(fn: Callable[[Context], Result]) -> Callable[[Context], Result]:
        # ID 必须唯一：审计报告、豁免清单、文档索引都按 ID 定位。
        # 2026-11-20 发现 `DS-10` 被两条检查共用（42 项只有 41 个唯一 ID），
        # 按 ID 做豁免会指到错误的检查上，所以在注册期就拦住。
        for existing in REGISTRY:
            if existing.id == cid:
                raise ValueError(
                    f"审计检查 ID 重复：{cid}"
                    f"（已有「{existing.title}」，新增「{title}」）。"
                    "ID 必须唯一，否则按 ID 豁免/追踪会指向错误检查。"
                )
        REGISTRY.append(Check(cid, card, title, level, owner, fn, gate))
        return fn

    return deco


class Context:
    """给检查项用的小工具：路径、缓存、跑测试。

    检查里重复读同一个 JSON 很常见（模板交集要读 dev 和 lock），缓存能省掉大量 IO。
    """

    def __init__(self, root: Path):
        self.root = root
        self.contract_dir = root / "contract"
        self.dataset_dir = root / "dataset"
        self.dev_dir = self.dataset_dir / "dev"
        self.lock_dir = self.dataset_dir / "lock"
        self.ui_dir = root / "ui"
        self.src_dir = root / "src"
        self.docs_dir = root / "docs"
        self._cache: dict[str, Any] = {}

    def read_text(self, path: Path) -> str:
        key = f"text::{path}"
        if key not in self._cache:
            self._cache[key] = path.read_text(encoding="utf-8", errors="replace")
        return self._cache[key]

    def labels(self, set_name: str) -> dict[str, list[dict]]:
        """读某一集的全部真值，返回 {image_id: [记录...]}。"""
        key = f"labels::{set_name}"
        if key in self._cache:
            return self._cache[key]

        import json

        out: dict[str, list[dict]] = {}
        lbl_dir = self.dataset_dir / set_name / "labels"
        if lbl_dir.is_dir():
            for p in sorted(lbl_dir.glob("*.json")):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    continue
                records = data if isinstance(data, list) else [data]
                iid = next((r.get("image_id") for r in records
                            if r.get("record_type") == "ImageDescriptor"), p.stem)
                out[iid] = records
        self._cache[key] = out
        return out

    def manifest(self, set_name: str) -> list[dict]:
        import csv

        key = f"manifest::{set_name}"
        if key in self._cache:
            return self._cache[key]
        p = self.dataset_dir / set_name / "manifest.csv"
        rows = []
        if p.is_file():
            rows = list(csv.DictReader(p.open(encoding="utf-8-sig")))
        self._cache[key] = rows
        return rows

    def run_tests(self, test_dir: str, timeout: int = 180) -> tuple[bool, str]:
        """跑一个目录下的 unittest，返回 (是否全绿, 摘要)。"""
        key = f"tests::{test_dir}"
        if key in self._cache:
            return self._cache[key]

        python = self.root / ".venv" / "Scripts" / "python.exe"
        if not python.exists():
            python = Path(sys.executable)
        try:
            proc = subprocess.run(
                [str(python), "-m", "unittest", "discover", "-s", test_dir],
                cwd=str(self.root), capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            res = (False, f"超时 >{timeout}s")
            self._cache[key] = res
            return res

        tail = (proc.stderr or "") + (proc.stdout or "")
        line = ""
        for ln in reversed(tail.splitlines()):
            if ln.startswith("Ran ") or ln.startswith("OK") or "FAILED" in ln:
                line = ln.strip() + (" | " + line if line else "")
                if "Ran " in line and ("OK" in line or "FAILED" in line):
                    break
        detail = line or tail.strip().splitlines()[-1][:120] if tail.strip() else "无输出"
        res = (proc.returncode == 0, detail)
        self._cache[key] = res
        return res

    def git(self, *args: str) -> str:
        try:
            proc = subprocess.run(["git", *args], cwd=str(self.root),
                                  capture_output=True, text=True, timeout=20,
                                  encoding="utf-8", errors="replace")
            return (proc.stdout or "").strip()
        except Exception:
            return ""


def run_all(ctx: Context) -> list[tuple[Check, Result]]:
    results: list[tuple[Check, Result]] = []
    for c in REGISTRY:
        try:
            r = c.fn(ctx)
            if not isinstance(r, Result):
                r = fail(f"检查返回了 {type(r).__name__}，不是 Result", "审计器 bug")
        except Exception as exc:  # 检查自身崩了 ≠ 项目不达标
            r = fail(f"检查抛异常：{type(exc).__name__}: {exc}",
                     "这是审计器自身的 bug，先修 audit/checks/")
        results.append((c, r))
    return results
