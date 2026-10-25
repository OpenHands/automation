"""Tests for the verify-automation skill's control CLI and feature map.

The CLI lives at `.agents/skills/verify-automation/scripts/control_automation.py`.
These tests drive it against a tiny local HTTP server (an attached run), check
that its signed deliveries and KV tokens are the ones the service accepts, run
a throwaway feature map, and validate the committed map, without launching the
service or an Agent Server.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shlex
import signal
import sys
import tarfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from openhands.automation.providers import get_verifier
from openhands.automation.utils.kv import verify_kv_token


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".agents/skills/verify-automation/scripts/control_automation.py"
SESSION_KEY = "qa-session-key-123456"


def _load():
    spec = importlib.util.spec_from_file_location("control_automation", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["control_automation"] = module
    spec.loader.exec_module(module)
    return module


ca = _load()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = {
            "status": "ok",
            "path": self.path,
            "method": self.command,
            "items": [{"id": "first"}, {"id": "second"}],
            "echo_key": self.headers.get("X-Session-API-Key"),
            "encrypted": "gAAAAA" + "A" * 64,
            "received": raw.decode(errors="replace"),
            "signature": self.headers.get("X-Signature-256"),
        }
        status = (
            404 if "/missing" in self.path else 503 if "/refused" in self.path else 200
        )
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.send_header("x-echo-key", self.headers.get("X-Session-API-Key", ""))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _reply

    def log_message(self, format, *args):  # noqa: A002
        pass


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    """A run whose two services are attached to one local HTTP server."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    home = tmp_path / "runs"
    run = home / "20260101-000000-test"
    for sub in ("private", "evidence", "fixtures", "home"):
        (run / sub).mkdir(parents=True)
    (run / "private" / "session_api_key").write_text(SESSION_KEY)
    (run / "private" / "kv_secret").write_text("qa-kv-secret-" + "k" * 24)
    data = {
        "id": run.name,
        "host": "127.0.0.1",
        "checkout": str(ROOT),
        "checkout_sha": "test",
        "agent_server": {"mode": "attach", "url": url},
        "automation": {"mode": "attach", "url": url, "extra_env": {}},
        "processes": [],
    }
    (run / "run.json").write_text(json.dumps(data))
    monkeypatch.setenv(ca.HOME_ENV, str(home))
    monkeypatch.setenv(ca.RUN_ENV, str(run))
    yield run
    server.shutdown()
    server.server_close()


def _run(capsys, *argv):
    code = ca.main(list(argv))
    return code, capsys.readouterr().out


def _printed(capsys, *argv) -> str:
    with pytest.raises(SystemExit) as exit_info:
        ca.main(list(argv))
    assert exit_info.value.code == 0
    return capsys.readouterr().out.strip()


def test_api_checks_pass_and_fail_on_the_json_body(run_dir, capsys):
    code, out = _run(
        capsys,
        "api",
        "GET",
        "/v1",
        "--check",
        "status",
        "eq",
        "ok",
        "--check",
        "items",
        "len-eq",
        "2",
        "--check",
        "nothing.here",
        "missing",
    )
    assert code == 0, out
    code, out = _run(capsys, "api", "GET", "/v1", "--check", "items", "len-ge", "3")
    assert code == 1
    assert json.loads(out)["checks"][0]["ok"] is False


def test_short_service_paths_expand_under_the_base_path(run_dir, capsys):
    assert _printed(capsys, "api", "GET", "/v1/kv", "--field", "path") == (
        "/api/automation/v1/kv"
    )
    assert _printed(capsys, "api", "GET", "/health", "--field", "path") == "/health"
    assert (
        _printed(
            capsys, "api", "--agent-server", "GET", "/v1/models", "--field", "path"
        )
        == "/v1/models"
    )


def test_api_expect_status_and_field_chaining(run_dir, capsys):
    code, _ = _run(capsys, "api", "GET", "/v1/missing", "--expect", "404")
    assert code == 0
    code, _ = _run(capsys, "api", "GET", "/v1/missing")
    assert code == 1
    assert _printed(capsys, "api", "GET", "/v1", "--field", "items.1.id") == "second"


def test_session_key_never_reaches_output_or_evidence(run_dir, capsys):
    code, out = _run(capsys, "api", "GET", "/v1", "--save", "F99.demo/list")
    assert code == 0
    assert SESSION_KEY not in out
    assert "<redacted:session-api-key>" in out
    saved = Path(json.loads(out)["saved"])
    assert saved.is_relative_to(run_dir / "evidence" / "F99.demo")
    assert SESSION_KEY not in saved.read_text()


@pytest.mark.parametrize(
    "flag,value,redacted",
    [
        ("--field", "echo_key", "<redacted:session-api-key>"),
        ("--field", "encrypted", "<redacted:encrypted>"),
        ("--print-header", "x-echo-key", "<redacted:session-api-key>"),
    ],
)
def test_raw_output_redacts_secrets(run_dir, capsys, flag, value, redacted):
    out = _printed(capsys, "api", "GET", "/v1", flag, value)
    assert SESSION_KEY not in out
    assert "gAAAAA" not in out
    assert redacted in out


def test_raw_secrets_require_an_explicit_opt_in(run_dir, capsys):
    out = _printed(
        capsys, "api", "GET", "/v1", "--field", "echo_key", "--unsafe-unredacted"
    )
    assert out == SESSION_KEY
    code, _ = _run(capsys, "api", "GET", "/v1", "--unsafe-unredacted")
    assert code == 2


@pytest.mark.parametrize(
    "scheme,header",
    [
        ("hmac_sha256_hex", "X-Signature-256"),
        ("standard_webhooks", "X-Signature-256"),
        ("slack_v0", "X-Slack-Signature"),
    ],
)
@pytest.mark.parametrize("secret", ["qa-plain-secret", "whsec_" + "a" * 43])
def test_signed_deliveries_verify_with_the_services_own_verifiers(
    scheme, header, secret
):
    body = b'{"type":"order.created"}'
    headers = ca.sign_delivery(scheme, secret, body, header, "msg_1", int(time.time()))
    verifier = get_verifier(scheme)
    assert verifier is not None
    assert verifier.verify(
        body=body, headers=headers, secret=secret, signature_header=header
    )
    assert not verifier.verify(
        body=body + b" ", headers=headers, secret=secret, signature_header=header
    )


def test_event_send_signs_the_exact_body_it_posts(run_dir, capsys):
    out = _printed(
        capsys,
        "event",
        "send",
        "qa-src",
        "--secret",
        "qa-plain-secret",
        "--json",
        '{"type":"x"}',
        "--field",
        ".",
    )
    echoed = json.loads(out)
    assert echoed["path"] == f"/api/automation/v1/events/{ca.LOCAL_ORG_ID}/qa-src"
    assert echoed["echo_key"] is None  # deliveries carry no API key
    verifier = get_verifier("hmac_sha256_hex")
    assert verifier is not None
    assert verifier.verify(
        body=echoed["received"].encode(),
        headers={"X-Signature-256": echoed["signature"]},
        secret="qa-plain-secret",
        signature_header="X-Signature-256",
    )


def test_kv_tokens_are_the_ones_the_service_accepts(run_dir, capsys):
    automation_id = str(uuid.uuid4())
    token = _printed(capsys, "kv", "token", "--automation-id", automation_id)
    secret = (run_dir / "private" / "kv_secret").read_text()
    assert str(verify_kv_token(secret, token).automation_id) == automation_id
    for flag in ("--expired", "--wrong-secret"):
        bad = _printed(capsys, "kv", "token", "--automation-id", automation_id, flag)
        with pytest.raises(Exception):
            verify_kv_token(secret, bad)


@pytest.mark.parametrize("kind", sorted(ca.TARBALL_KINDS))
def test_fixture_tarballs_hold_an_executable_entrypoint(kind):
    with tarfile.open(fileobj=io.BytesIO(ca.build_tarball(kind)), mode="r:gz") as tar:
        members = {m.name: m for m in tar.getmembers()}
        assert members["run.sh"].mode & 0o111
        assert "qa_lib.py" in members
        script = tar.extractfile("run.sh")
        assert script is not None
        assert script.read().startswith(b"#!/usr/bin/env bash")


FAMILY = """# Demo family

A family used by the CLI tests.

Source: `x`

Routes: none

## Sub-features

- `F98.first`: the first bullet sets a variable.
- `F98.second`: a later bullet sees it.
- `F98.bug`: a known bug fails as expected.
- `F98.offline`: needs something this machine lacks.

## How to get to it (agent POV)

- Nowhere.

## Driving it with control-automation

Preconditions:

- Nothing.

- **First (`F98.first`).** Set a value.
  ```sh
  VALUE=$(echo carried)
  ```
- **Second (`F98.second`).** Read it back.
  Requires: `F98.first`
  ```sh
  test "$VALUE" = carried
  control-automation api GET /v1 --check status eq ok
  ```
- **Broken thing (`F98.bug`), known bug.** Asserts the correct behavior.
  Requires: `F98.second`
  ```sh
  test "$VALUE" = carried
  test "$VALUE" = fixed  # bug: exit 1
  ```
- **Offline (`F98.offline`), blocked: needs a Slack app.** Not executed.

## Gotchas

- None.
"""


@pytest.fixture
def family(tmp_path, monkeypatch):
    feature_map = tmp_path / "map"
    feature_map.mkdir()
    monkeypatch.setattr(ca, "FEATURE_MAP_DIR", feature_map)
    path = feature_map / "F98-demo.md"

    def write(content: str) -> Path:
        path.write_text(content)
        return path

    return write


def test_map_run_carries_variables_and_reports_xfail_and_blocked(
    run_dir, family, capsys
):
    path = family(FAMILY)
    code, out = _run(capsys, "map", "run", "--file", "F98", "--record")
    result = json.loads(out)
    assert code == 0, out
    assert result["summary"]["pass"] == 2
    assert result["summary"]["xfail"] == 1
    assert result["summary"]["blocked"] == 1
    ledger = (run_dir / "evidence" / "ledger.jsonl").read_text().splitlines()
    assert {json.loads(row)["feature"] for row in ledger} == {
        "F98.first",
        "F98.second",
        "F98.bug",
        "F98.offline",
    }

    path.write_text(FAMILY.replace("= fixed  # bug", "= carried  # bug"))
    code, out = _run(capsys, "map", "run", "--file", "F98")
    assert code == 1
    assert json.loads(out)["summary"]["xpass"] == 1

    path.write_text(
        FAMILY.replace(
            'test "$VALUE" = carried\n  test "$VALUE" = fixed',
            'test "$VALUE" = other\n  test "$VALUE" = fixed',
        )
    )
    code, out = _run(capsys, "map", "run", "--file", "F98")
    assert code == 1
    assert json.loads(out)["summary"]["fail"] == 1


def test_map_check_requires_a_bug_marker_in_known_bug_bullets(family, capsys):
    family(FAMILY.replace("  # bug: exit 1", ""))
    code, out = _run(capsys, "map", "check", "--file", "F98", "--no-routes")
    assert code == 1
    assert "# bug" in out


@pytest.mark.parametrize(
    "marker, problem",
    [
        ("# bug", "needs the documented failure"),
        ("# bug: exit", "write `exit N`"),
        ("# bug: exit 0", "from 1 to 255"),
        ("# bug: exit 1 && status eq 500", "stands alone"),
        ("# bug: exit eq 2", "reserved for the marked command's status"),
        (
            "# bug: status eq 500 && exit eq 2",
            "reserved for the marked command's status",
        ),
        ("# bug: status is 500", "is not PATH OP"),
        ("# bug: status eq", "takes one value"),
        ("# bug: detail missing 500", "takes no value"),
        ("# bug: detail missing", "needs a clause naming"),
        ("# bug: detail contains 'unclosed", "cannot parse"),
        ("# bug: status eq 500", "does not call control-automation"),
    ],
)
def test_map_check_rejects_a_missing_or_malformed_bug_predicate(
    family, capsys, marker, problem
):
    family(FAMILY.replace("# bug: exit 1", marker))
    code, out = _run(capsys, "map", "check", "--file", "F98", "--no-routes")
    assert code == 1, out
    assert problem in out


@pytest.mark.parametrize(
    "line",
    [
        'if test "$VALUE" = carried; then false; fi  # bug: exit 1',
        '! test "$VALUE" = carried  # bug: exit 1',
        'test ! -e "$VALUE"  # bug: exit 1',
    ],
)
def test_map_check_rejects_negative_bug_lines(family, capsys, line):
    family(FAMILY.replace('test "$VALUE" = fixed  # bug: exit 1', line))
    code, out = _run(capsys, "map", "check", "--file", "F98", "--no-routes")
    assert code == 1, out
    assert "positively" in out


def test_assert_names_a_computed_value(tmp_path, capsys, monkeypatch):
    code, out = _run(capsys, "assert", "--value", "1700", "--check", ".", "le", "1500")
    result = json.loads(out)
    assert code == 1
    assert result["value"] == 1700
    assert ca.bug_predicate_matches(ca.parse_bug_predicate("value gt 1500"), 1, result)
    saved = tmp_path / "saved.json"
    saved.write_text('{"status": 500}')
    code, out = _run(
        capsys, "assert", "--json", str(saved), "--check", "status", "eq", "409"
    )
    assert code == 1
    assert json.loads(out)["checks"][0]["actual"] == "500"
    monkeypatch.setattr("sys.stdin", io.StringIO("qa-text\n"))
    code, out = _run(capsys, "assert", "--check", ".", "eq", "qa-text")
    assert code == 0, out


BUG_API = "control-automation api GET {path} --expect 200"
DOCUMENTED = "  # bug: status eq 404 && response.body.path eq /missing"


def _bug_row(out: str) -> dict:
    return next(r for r in json.loads(out)["bullets"] if r.get("ids") == ["F98.bug"])


@pytest.mark.parametrize(
    "path, status",
    [
        # The documented failure: the marked command answers 404.
        ("/missing", "xfail"),
        # An unrelated error at the same marked command is not the known bug.
        ("/refused", "fail"),
        # Correct behavior: the bug no longer reproduces.
        ("/v1", "xpass"),
    ],
)
def test_known_bug_xfails_only_on_the_documented_failure(
    run_dir, family, capsys, path, status
):
    family(
        FAMILY.replace(
            '  test "$VALUE" = fixed  # bug: exit 1',
            "  " + BUG_API.format(path=path) + DOCUMENTED,
        )
    )
    code, out = _run(capsys, "map", "run", "--file", "F98", "--record")
    bug = _bug_row(out)
    assert bug["status"] == status, out
    assert code == (0 if status == "xfail" else 1)
    if status == "fail":
        assert bug["failed_status"] == 1
        assert bug["observed"]["status"] == 503
        assert "not with the documented failure" in bug["hint"]
        ledger = [
            json.loads(line)
            for line in (run_dir / "evidence" / "ledger.jsonl").read_text().splitlines()
        ]
        assert {r["feature"]: r["result"] for r in ledger}["F98.bug"] == "fail"


def test_known_bug_matches_the_failure_not_a_later_cleanup_call(
    run_dir, family, capsys
):
    # A recipe's EXIT cleanup calls the CLI after the marked command failed;
    # the predicate still sees what the marked command observed.
    family(
        FAMILY.replace(
            '  test "$VALUE" = fixed  # bug: exit 1',
            "  trap 'control-automation api GET /v1 >/dev/null' EXIT\n  "
            + BUG_API.format(path="/missing")
            + DOCUMENTED,
        )
    )
    code, out = _run(capsys, "map", "run", "--file", "F98")
    assert _bug_row(out)["status"] == "xfail", out


def test_known_bug_pass_after_a_broken_arrange_is_not_a_fix(run_dir, family, capsys):
    # With --keep-going, a recipe whose prerequisite failed can pass on empty
    # variables; that is no evidence the bug is fixed.
    family(
        FAMILY.replace(
            "VALUE=$(echo carried)", "VALUE=$(echo carried)\n  false"
        ).replace(
            '  test "$VALUE" = carried\n  test "$VALUE" = fixed  # bug: exit 1',
            "  true  # bug: exit 1",
        )
    )
    code, out = _run(capsys, "map", "run", "--file", "F98", "--keep-going")
    bug = _bug_row(out)
    assert bug["status"] == "fail", out
    assert "F98.second" in bug["hint"]


def test_known_bug_pass_after_a_broken_indirect_arrange_is_not_a_fix(
    run_dir, family, capsys
):
    # F98.second passes on its own, but it needs F98.first, which failed; the
    # known bug needs F98.second, so its pass is no evidence either.
    family(
        FAMILY.replace("VALUE=$(echo carried)", "VALUE=$(echo carried)\n  false")
        .replace(
            '  test "$VALUE" = carried\n  control-automation api GET /v1',
            "  control-automation api GET /v1",
        )
        .replace(
            '  test "$VALUE" = carried\n  test "$VALUE" = fixed  # bug: exit 1',
            "  true  # bug: exit 1",
        )
    )
    code, out = _run(capsys, "map", "run", "--file", "F98", "--keep-going")
    rows = {tuple(r.get("ids") or []): r for r in json.loads(out)["bullets"]}
    assert rows[("F98.second",)]["status"] == "pass", out
    bug = rows[("F98.bug",)]
    assert bug["status"] == "fail", out
    assert "F98.first" in bug["hint"]


@pytest.mark.parametrize(
    "stored, status",
    [
        # The documented failure: the file is there and has no key.
        ('{"agent": {"model": "m"}}', "xfail"),
        # The file is missing: an error object has no `checks`, so the
        # `eq null` clause must not read its absence as the bug.
        (None, "fail"),
    ],
)
def test_known_bug_predicate_needs_its_paths_present(
    run_dir, family, capsys, stored, status
):
    if stored is not None:
        (run_dir / "state.json").write_text(stored)
    family(
        FAMILY.replace(
            '  test "$VALUE" = fixed  # bug: exit 1',
            "  control-automation state cat state.json --check agent.key exists"
            "  # bug: checks.0.actual eq null",
        )
    )
    code, out = _run(capsys, "map", "run", "--file", "F98")
    assert _bug_row(out)["status"] == status, out


def test_known_bug_exit_predicate_tells_a_false_test_from_an_error(
    run_dir, family, capsys
):
    # `test` exits 1 when the comparison is false and 2 when it cannot run.
    family(
        FAMILY.replace(
            '  test "$VALUE" = fixed  # bug: exit 1',
            '  test "$VALUE" -le 3  # bug: exit 1',
        )
    )
    code, out = _run(capsys, "map", "run", "--file", "F98")
    bug = _bug_row(out)
    assert bug["status"] == "fail", out
    assert bug["failed_status"] == 2


def test_the_marked_call_replaces_an_earlier_observation(run_dir, family, capsys):
    # The marked line's own call replaces what an earlier call observed, so an
    # earlier 404 cannot stand in for the marked command's failure.
    family(
        FAMILY.replace(
            '  test "$VALUE" = fixed  # bug: exit 1',
            "  control-automation api GET /missing --expect 404\n"
            "  control-automation api GET /refused --expect 200" + DOCUMENTED,
        )
    )
    code, out = _run(capsys, "map", "run", "--file", "F98")
    bug = _bug_row(out)
    assert bug["status"] == "fail", out
    assert bug["observed"]["status"] == 503


def test_map_check_rejects_unknown_flags(family, capsys):
    family(FAMILY.replace("--check status eq ok", "--no-such-flag"))
    code, out = _run(capsys, "map", "check", "--file", "F98", "--no-routes")
    assert code == 1
    assert "--no-such-flag" in out


def test_committed_feature_map_is_structurally_valid(capsys):
    """Headings, IDs, index counts and every recipe command parse."""
    code, out = _run(capsys, "map", "check", "--no-routes")
    assert code == 0, out


def test_committed_feature_map_owns_and_drives_every_route(capsys):
    """Imports the app: each route has exactly one family that drives it."""
    code, out = _run(capsys, "map", "coverage")
    assert code == 0, out


def test_every_command_documents_examples(capsys):
    parser = ca.build_parser()
    for name in ("launch", "api", "doctor", "restart", "stop", "exec", "logs"):
        with pytest.raises(SystemExit):
            parser.parse_args([name, "--help"])
        assert "Examples:" in capsys.readouterr().out, name


def test_without_a_run_errors_are_one_json_object(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(ca.HOME_ENV, str(tmp_path))
    monkeypatch.delenv(ca.RUN_ENV, raising=False)
    code, out = _run(capsys, "api", "GET", "/v1")
    payload = json.loads(out)
    assert code == 3
    assert payload["ok"] is False
    assert "launch --new --print-run" in payload["hint"]


def test_state_ls_fails_required_entries_in_a_missing_directory(run_dir, capsys):
    code, out = _run(capsys, "state", "ls", "workspaces/none", "--expect", "x.txt")
    assert code == 1
    assert json.loads(out)["exists"] is False
    code, _ = _run(capsys, "state", "ls", "workspaces/none")
    assert code == 0


def test_state_cat_field_output_is_redacted(run_dir, capsys):
    out = _printed(capsys, "state", "cat", "private/session_api_key", "--field", ".")
    assert SESSION_KEY not in out
    assert out == "<redacted:session-api-key>"


def _assert_stops(pid: int, what: str) -> None:
    deadline = time.monotonic() + 5
    status = Path(f"/proc/{pid}/status")
    while time.monotonic() < deadline:
        # Gone, or a zombie waiting for a reaper: either way, not running.
        if not status.exists() or "State:\tZ" in status.read_text():
            return
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    pytest.fail(f"{what} kept running")


def test_exec_timeout_ends_the_programs_descendants(run_dir, tmp_path, capsys):
    pidfile = tmp_path / "child.pid"
    script = f"sleep 30 >/dev/null 2>&1 & echo $! > {pidfile}; sleep 30"
    code, out = _run(capsys, "exec", "--timeout", "1", "--", "sh", "-c", script)
    assert code == 1
    assert json.loads(out)["exit_code"] == 124
    _assert_stops(int(pidfile.read_text()), "the child after an exec timeout")


def test_recipe_timeout_ends_what_exec_started(run_dir, tmp_path):
    """`map run` stops a timed-out recipe's group: exec's own group goes too."""
    prog, child = tmp_path / "prog.pid", tmp_path / "child.pid"
    script = f"echo $$ > {prog}; sleep 30 >/dev/null 2>&1 & echo $! > {child}; sleep 30"
    recipe = tmp_path / "recipe.sh"
    recipe.write_text(
        f"{sys.executable} {SCRIPT} exec --timeout 60 -- sh -c {shlex.quote(script)}\n"
    )
    code, _, _, timed_out = ca.execute_recipe(recipe, dict(os.environ), timeout=3)
    assert timed_out and code == 124
    _assert_stops(int(prog.read_text()), "the exec program after a recipe timeout")
    _assert_stops(int(child.read_text()), "its child after a recipe timeout")
