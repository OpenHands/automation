"""Tests for .github/scripts/check_issue_readiness.py (CI gating script)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_prod_module():
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / ".github" / "scripts" / "check_issue_readiness.py"
    name = "check_issue_readiness"
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_prod = _load_prod_module()
evaluate_readiness = _prod.evaluate_readiness
extract_sections = _prod.extract_sections


ENHANCEMENT_READY = """### Problem or Use Case

Cron-triggered automations cannot currently target a specific timezone.

### Desired Behavior

The trigger schema accepts an IANA `timezone` field and the scheduler honors it.

### Acceptance Criteria

- [ ] `trigger.timezone` is validated against the IANA database
- [ ] `uv run pytest tests/` covers a non-UTC schedule
"""

BUG_READY = """### Actual Behavior

Running `uv run pytest tests/test_scheduler.py` fails with a KeyError when the
cron schedule has an empty day-of-week field.

### Acceptance Criteria

- [ ] No `KeyError` is raised for an empty day-of-week field
"""


def test_extract_sections_splits_on_headings():
    sections = extract_sections("### Alpha\n\ntext\n\n### Beta\n\nmore\n")
    assert sections["alpha"] == "\ntext\n\n"
    assert sections["beta"] == "\nmore\n"


def test_enhancement_ready_passes():
    result = evaluate_readiness(ENHANCEMENT_READY, ["enhancement"])
    assert result.ready is True
    assert result.reasons == []


def test_enhancement_missing_acceptance_criteria_fails():
    body = "### Desired Behavior\n\nSome desired change.\n"
    result = evaluate_readiness(body, ["enhancement"])
    assert result.ready is False
    assert any("Acceptance Criteria" in r for r in result.reasons)


def test_enhancement_missing_desired_behavior_fails():
    desired_section = (
        "### Desired Behavior\n\n"
        "The trigger schema accepts an IANA `timezone` field and the scheduler "
        "honors it.\n\n"
    )
    body = ENHANCEMENT_READY.replace(desired_section, "")
    result = evaluate_readiness(body, ["enhancement"])
    assert result.ready is False
    assert any("Desired Behavior" in r for r in result.reasons)


def test_bug_ready_passes():
    result = evaluate_readiness(BUG_READY, ["bug"])
    assert result.ready is True
    assert result.reasons == []


def test_bug_missing_run_method_fails():
    body = BUG_READY.replace(
        "Running `uv run pytest tests/test_scheduler.py` fails",
        "Running the service test harness fails",
    )
    result = evaluate_readiness(body, ["bug"])
    assert result.ready is False
    assert any("reproducible command" in r for r in result.reasons)


def test_bug_curl_reproduction_is_a_valid_run_method():
    body = BUG_READY.replace(
        "Running `uv run pytest tests/test_scheduler.py` fails with a KeyError",
        "Calling `curl -X POST /v1/preset/prompt` returns a 500",
    )
    result = evaluate_readiness(body, ["bug"])
    assert result.ready is True
    assert result.reasons == []


def test_bug_backticked_python_is_a_valid_run_method():
    body = BUG_READY.replace(
        "Running `uv run pytest tests/test_scheduler.py` fails with a KeyError",
        "Running `python main.py` inside the sandbox fails",
    )
    result = evaluate_readiness(body, ["bug"])
    assert result.ready is True
    assert result.reasons == []


def test_bug_acceptance_needs_checklist_item():
    body = BUG_READY.replace("- [ ] No `KeyError`", "Fix the KeyError")
    result = evaluate_readiness(body, ["bug"])
    assert result.ready is False
    assert any("checklist item" in r for r in result.reasons)


def test_no_bug_or_enhancement_label_not_ready():
    result = evaluate_readiness(ENHANCEMENT_READY, [])
    assert result.ready is False
    assert any("bug" in r and "enhancement" in r for r in result.reasons)


def test_no_response_field_counts_as_empty():
    actual_text = (
        "Running `uv run pytest tests/test_scheduler.py` fails with a KeyError "
        "when the\ncron schedule has an empty day-of-week field."
    )
    body = BUG_READY.replace(actual_text, "_No response_")
    result = evaluate_readiness(body, ["bug"])
    assert result.ready is False
    assert any("Actual Behavior" in r for r in result.reasons)


def test_json_mode_exits_zero_for_not_ready(tmp_path, capsys):
    body_file = tmp_path / "issue.md"
    body_file.write_text("no sections here")
    argv = sys.argv
    sys.argv = [
        "check_issue_readiness.py",
        "--body-file",
        str(body_file),
        "--labels",
        "bug",
        "--json",
    ]
    try:
        # A not-ready result must not be a process failure: the workflow runs
        # under `set -euo pipefail` and still needs to remove the label and
        # post the feedback comment.
        assert _prod.main() == 0
    finally:
        sys.argv = argv

    out = json.loads(capsys.readouterr().out)
    assert out["ready"] is False
    assert out["reasons"]
