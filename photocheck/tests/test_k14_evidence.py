"""Regression tests for K-14 physical-offline evidence validation."""

from __future__ import annotations

import json
from pathlib import Path

from audit.checks.evals import _offline_evidence_reason
from audit.model import Context


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_with_bom(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8-sig")


def _make_evidence(tmp_path: Path, *, firewall_rules: bool = False) -> Path:
    directory = tmp_path / "docs" / "evidence" / "capture"
    directory.mkdir(parents=True)
    network = {
        "active_adapter_count": 0,
        "adapters": [{"Name": "WLAN", "Status": "Disconnected"}],
        "external_connection_succeeded": False,
    }
    if not firewall_rules:
        network["firewall_profiles"] = [
            {"Name": "Domain", "Enabled": True},
            {"Name": "Private", "Enabled": True},
            {"Name": "Public", "Enabled": True},
        ]
    _write(directory / "network-summary.json", network)
    _write(
        directory / "offline-audit.txt",
        {
            "static_ok": True,
            "runtime": {"ok": True, "disposal_state": "passed"},
        },
    )
    if firewall_rules:
        _write(
            directory / "firewall-rules-summary.json",
            {
                "source_sha256": "a" * 64,
                "rules": [
                    {
                        "enabled": True,
                        "profile": "Any",
                        "direction": "Outbound",
                        "action": "Block",
                        "program_basename": "python.exe",
                    },
                    {
                        "enabled": True,
                        "profile": "Any",
                        "direction": "Outbound",
                        "action": "Block",
                        "program_basename": "exiftool.exe",
                    },
                ],
            },
        )
    return directory / "network-summary.json"


def test_physical_offline_accepts_enabled_profiles(tmp_path: Path) -> None:
    summary = _make_evidence(tmp_path)
    reason = _offline_evidence_reason(Context(tmp_path), summary)
    assert reason is not None
    assert "三个防火墙配置文件均启用" in reason


def test_physical_offline_accepts_runtime_deny_rules(tmp_path: Path) -> None:
    summary = _make_evidence(tmp_path, firewall_rules=True)
    reason = _offline_evidence_reason(Context(tmp_path), summary)
    assert reason is not None
    assert "Any Profile" in reason


def test_physical_offline_rejects_inbound_firewall_rule(tmp_path: Path) -> None:
    summary = _make_evidence(tmp_path, firewall_rules=True)
    rules_path = summary.with_name("firewall-rules-summary.json")
    rules = json.loads(rules_path.read_text(encoding="utf-8"))
    rules["rules"][0]["direction"] = "Inbound"
    _write(rules_path, rules)
    assert _offline_evidence_reason(Context(tmp_path), summary) is None


def test_physical_offline_rejects_failed_smoke_run(tmp_path: Path) -> None:
    summary = _make_evidence(tmp_path)
    audit_path = summary.with_name("offline-audit.txt")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["runtime"]["ok"] = False
    _write(audit_path, audit)
    assert _offline_evidence_reason(Context(tmp_path), summary) is None


def test_physical_offline_rejects_contradictory_adapter_state(tmp_path: Path) -> None:
    summary = _make_evidence(tmp_path)
    network_path = summary
    network = json.loads(network_path.read_text(encoding="utf-8"))
    network["adapters"][0]["Status"] = "Up"
    _write(network_path, network)
    assert _offline_evidence_reason(Context(tmp_path), summary) is None


def test_physical_offline_accepts_powershell_utf8_bom(tmp_path: Path) -> None:
    summary = _make_evidence(tmp_path)
    network = json.loads(summary.read_text(encoding="utf-8"))
    _write_with_bom(summary, network)
    audit_path = summary.with_name("offline-audit.txt")
    audit_data = json.loads(audit_path.read_text(encoding="utf-8"))
    _write_with_bom(audit_path, audit_data)
    assert _offline_evidence_reason(Context(tmp_path), summary) is not None
