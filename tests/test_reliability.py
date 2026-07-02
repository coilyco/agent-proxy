"""Tests for the reliability harness."""

from __future__ import annotations

import json
from pathlib import Path

from scripts import reliability_loop as rl


def test_endpoint_resolution(monkeypatch):
    monkeypatch.setenv("PROXY_BASE_URL", "http://127.0.0.1:9000")
    monkeypatch.setenv("PROXY_TOWER_BASE_URL", "http://tower.example")
    assert rl._endpoint_and_model("proxy") == ("http://127.0.0.1:9000/v1/chat/completions", "fast-think")
    assert rl._endpoint_and_model("direct") == ("http://tower.example/v1/chat/completions", "qwen3-coder:30b")


def test_run_scores_and_renders_reports(monkeypatch, tmp_path):
    calls = []

    def fake_score(url, model, messages, expect_tool):
        calls.append((url, model, expect_tool, len(messages)))
        return (not expect_tool, "missed_toolcall" if expect_tool else "ok")

    monkeypatch.setattr(rl, "_score_turn", fake_score)
    monkeypatch.setattr(rl, "_endpoint_and_model", lambda target: ("http://example/v1/chat/completions", "fast-think"))

    summary = rl.run("proxy", 2)

    assert summary.turns == 2
    assert summary.usable == 1
    assert summary.reliability == 50.0
    assert summary.reasons == {"ok": 1, "missed_toolcall": 1}
    assert [turn.reason for turn in summary.turn_results] == ["ok", "missed_toolcall"]
    assert calls[0][2] is False
    assert calls[1][2] is True

    md = rl.render_markdown(summary)
    assert "M2 Reliability Measurement" in md
    assert "`proxy`" in md
    assert "Failure histogram" in md

    out_md = tmp_path / "report.md"
    out_json = tmp_path / "report.json"
    rl.write_report(summary, markdown_path=out_md, json_path=out_json)
    assert out_md.read_text(encoding="utf-8") == md
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["target"] == "proxy"
    assert payload["failure_histogram"] == {"missed_toolcall": 1, "ok": 1}
