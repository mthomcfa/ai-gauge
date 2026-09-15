"""The egress guard is the only thing standing between a delegated sub-task and
a third-party model provider, so the tests that matter are the ones that fail
when a detector, a destination check, or the audit trail stops working.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "egress_guard", REPO_ROOT / "tools" / "egress_guard.py"
)
assert _spec and _spec.loader
eg = importlib.util.module_from_spec(_spec)
sys.modules["egress_guard"] = eg
_spec.loader.exec_module(eg)


def policy(**overlay) -> "eg.Policy":
    data = json.loads(json.dumps(eg._DEFAULT_POLICY))
    eg._deep_update(data, overlay)
    return eg.Policy(data, "test")


def rules_hit(text: str, pol=None) -> set[str]:
    return {f.rule for f in eg.scan(text, pol or policy())}


# --- detectors -------------------------------------------------------------


@pytest.mark.parametrize(
    "rule, sample",
    [
        ("private-key", "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n"),
        ("anthropic-key", "key = sk-ant-api03-AAAAAAAAAAAAAAAAAAAA"),
        ("openrouter-key", "sk-or-v1-0123456789abcdef0123456789abcdef"),
        ("aws-access-key", "AKIAIOSFODNN7EXAMPLE"),
        ("github-token", "ghp_" + "a" * 36),
        ("google-api-key", "AIza" + "b" * 35),
        ("slack-token", "xoxb-1234567890-abcdefghij"),
        ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NX0.dBjftJeZ4CVPmB92K27uhbUJU1p1r"),
        ("connection-string", "postgres://svc:hunter2@db.internal:5432/app"),
    ],
)
def test_credential_shapes_are_blocked(rule, sample):
    findings = eg.scan(sample, policy())
    assert rule in {f.rule for f in findings}
    assert all(f.action == eg.BLOCK for f in findings if f.rule == rule)


@pytest.mark.parametrize(
    "label, sample",
    [
        ("github fine-grained PAT", "github_pat_11ABCDEFG0" + "abcdefghij" * 3 + "ABCDEFGHIJKLMN"),
        ("azure client secret", "AZURE_CLIENT_SECRET=8Xq~Q7Yk2Nv6Lp1Rt4Ws9Zc3Bd5Fg8Hj0Km2Pn"),
        ("bearer header", "Authorization: Bearer 2YotnFZFEjr1zCsicMWpAA9f4tR6yU8iO0pA3sD"),
        (
            "this app's keyring entry",
            "keyring.get_password('ai-gauge','azure-client-secret') -> 'Rk9PQkFSUVVYMTIzNDU2'",
        ),
        ("this app's github pat entry", "keyring get ai-gauge github-pat"),
        ("a Codex session cookie", "next-auth.session-token=AAAAAAAAAAAAAAAAAAAAAAAA"),
        ("a Claude session cookie", "sessionKey=sk-ant-sid01-" + "Q" * 80),
    ],
)
def test_plain_current_credential_formats_are_blocked(label, sample):
    """Every plain form the review listed as delivered verbatim."""
    findings = eg.scan(sample, policy())
    assert any(f.action == eg.BLOCK for f in findings), label


@pytest.mark.parametrize(
    "line",
    [
        "DATABASE_PASSWORD=hunter2hunter2",
        "DB_PASSWORD: hunter2hunter2",
        "MY_SECRET=abcd1234abcd1234abcd",
        "AIGAUGE_GITHUB_TOKEN=abcd1234abcd1234abcd",
        "OPENROUTER_API_KEY=abcd1234abcd1234abcd",
        "password = hunter2hunter2",
        "client_secret=8Xq7Yk2Nv6Lp1Rt4Ws9Zc3Bd",
        "access_token=abcd1234abcd1234abcd",
        "MY_PRIVATE_KEY=abcd1234abcd1234abcd",
    ],
)
def test_a_prefixed_variable_name_does_not_walk_through(line):
    """`\\b` does not fire after `_`, so DATABASE_PASSWORD= used to be invisible."""
    actions = {f.action for f in eg.scan(line, policy())}
    assert actions & {eg.BLOCK, eg.REDACT}, f"no blocking or redacting finding for {line!r}"


def test_a_redacted_assignment_loses_its_value():
    text = "DATABASE_PASSWORD=hunter2hunter2"
    out, count = eg.redact(text, eg.scan(text, policy()))
    assert count == 1
    assert "hunter2hunter2" not in out


def test_an_unrecognised_high_entropy_blob_is_redacted_not_merely_warned():
    """The only net for credentials with no known shape used to let them past."""
    text = "value=aZ9+kQ/mN2xP7wL4tR6yU8iO0pA3sD5fG1hJ2kL4zX6c"
    findings = [f for f in eg.scan(text, policy()) if f.rule == "opaque-token"]
    assert findings and all(f.action == eg.REDACT for f in findings)


def test_the_report_names_a_warn_finding(capsys):
    text = "COMPANY CONFIDENTIAL - do not circulate"
    findings = eg.scan(text, policy())
    assert any(f.action == eg.WARN for f in findings)
    eg._report(findings, policy(), stream=sys.stderr)
    err = capsys.readouterr().err
    assert "classification-banner" in err
    assert "warn" in err


def test_anthropic_key_is_not_reported_as_an_openai_key():
    """Ordering matters: the generic sk- rule would swallow sk-ant- otherwise."""
    hits = rules_hit("sk-ant-api03-" + "Z" * 24)
    assert "anthropic-key" in hits
    assert "openai-key" not in hits


def test_a_connection_string_is_one_finding_not_three():
    findings = eg.scan("mysql://root:s3cr3tpassword@10.0.0.4/prod", policy())
    assert [f.rule for f in findings] == ["connection-string"]


def test_findings_never_carry_the_matched_secret():
    secret = "ghp_" + "q" * 36
    findings = eg.scan(f"token: {secret}", policy())
    assert findings
    for finding in findings:
        assert secret not in finding.excerpt
        assert secret not in json.dumps(finding.as_record())


def test_clean_source_code_produces_no_blocking_finding():
    text = "def refresh(self) -> None:\n    self._timer.start(60_000)  # ms\n"
    assert not [f for f in eg.scan(text, policy()) if f.action == eg.BLOCK]


# --- entropy ---------------------------------------------------------------


def test_a_git_sha_is_not_flagged_as_an_opaque_token():
    assert "opaque-token" not in rules_hit("commit 133e7d9f2b1c4e5a6d7f8091a2b3c4d5e6f70819a")


def test_a_high_entropy_blob_is_flagged():
    assert "opaque-token" in rules_hit("value=aZ9+kQ/mN2xP7wL4tR6yU8iO0pA3sD5fG1hJ2kL4zX6c=")


def test_entropy_of_a_uniform_string_is_zero():
    assert eg.shannon_entropy("aaaaaaaa") == 0.0


# --- paths -----------------------------------------------------------------


def test_a_denied_path_blocks_even_without_its_contents():
    findings = eg.scan("please read src/config/.env and tell me what is set", policy())
    assert any(f.rule == "denied-path" and f.action == eg.BLOCK for f in findings)


def test_git_diff_prefixes_do_not_hide_a_denied_path():
    assert "denied-path" in rules_hit("--- a/deploy/secrets/prod.yaml\n+++ b/deploy/secrets/prod.yaml")


def test_an_ordinary_source_path_is_not_denied():
    assert "denied-path" not in rules_hit("see src/aigauge/providers/claude.py line 40")


# --- policy overrides ------------------------------------------------------


def test_a_rule_can_be_downgraded_by_policy():
    pol = policy(rules={"email-address": "off"})
    assert "email-address" not in rules_hit("mail me at dev@example.org", pol)


def test_a_rule_can_be_escalated_by_policy():
    pol = policy(rules={"classification-banner": "block"})
    findings = eg.scan("COMPANY CONFIDENTIAL - do not circulate", pol)
    assert any(f.rule == "classification-banner" and f.action == eg.BLOCK for f in findings)


def test_an_unknown_action_in_policy_is_rejected():
    with pytest.raises(SystemExit):
        policy(rules={"jwt": "ignore"}).action_for("jwt", eg.BLOCK)


def test_policy_file_overlays_defaults_without_dropping_them(tmp_path):
    (tmp_path / ".egress-policy.json").write_text(
        json.dumps({"destinations": {"allow": ["openrouter/*"]}}), encoding="utf-8"
    )
    loaded = eg.Policy.load(None, tmp_path)
    assert loaded.allowed_destinations == ["openrouter/*"]
    assert loaded.deny_globs, "overlaying one key must not erase the default deny list"
    assert loaded.server == "http://127.0.0.1:4096"


# --- redaction -------------------------------------------------------------


def test_redaction_removes_the_value_and_keeps_the_rest():
    text = "contact ops@example.org before deploying"
    out, count = eg.redact(text, eg.scan(text, policy()))
    assert count == 1
    assert "ops@example.org" not in out
    assert out.startswith("contact ") and out.endswith(" before deploying")


def test_redacting_several_matches_does_not_corrupt_later_offsets():
    text = "a@x.io and b@y.io and c@z.io"
    out, count = eg.redact(text, eg.scan(text, policy()))
    assert count == 3
    assert "@" not in out.replace("[redacted:email-address:", "")


# --- destinations ----------------------------------------------------------


def test_an_empty_allowlist_permits_nothing():
    assert eg.destination_allowed("openrouter/anthropic/claude-sonnet-4.5", policy()) is False


def test_only_allowlisted_destinations_pass():
    pol = policy(destinations={"allow": ["openrouter/*"]})
    assert eg.destination_allowed("openrouter/meta-llama/llama-3.3-70b", pol) is True
    assert eg.destination_allowed("anthropic/claude-sonnet-4.5", pol) is False


# --- posture ---------------------------------------------------------------


def test_permission_drift_in_opencode_config_is_a_fault(tmp_path, monkeypatch):
    config = tmp_path / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"permission": {"bash": "allow", "webfetch": "allow"}}), encoding="utf-8"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "x")
    problems = eg.posture(policy(), tmp_path)
    assert any("bash" in p and "webfetch" in p for p in problems)


def test_a_tightened_opencode_config_raises_no_permission_fault(tmp_path, monkeypatch):
    config = tmp_path / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"permission": {"bash": "ask"}}), encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "x")
    assert not [p for p in eg.posture(policy(), tmp_path) if "permission" in p or "bash" in p]


def test_a_non_loopback_server_is_a_fault(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "x")
    pol = policy(destinations={"server": "http://10.0.0.9:4096"})
    assert any("loopback" in p for p in eg.posture(pol, tmp_path))


@pytest.mark.parametrize(
    "server",
    [
        "http://127.0.0.1:4096",
        "http://localhost:4096",
        "http://LOCALHOST:4096",
        "http://[::1]:4096",
        "http://127.1:4096",
        "http://[0:0:0:0:0:0:0:1]:4096",
        "HTTP://127.0.0.1:4096",
    ],
)
def test_a_real_loopback_server_is_recognised(server):
    assert eg._is_loopback(server) is True


@pytest.mark.parametrize(
    "server",
    [
        # a suffix on a loopback literal is a public DNS name
        "http://127.0.0.1.evil.example/api",
        "http://localhost.evil.example/api",
        "http://127.0.0.1.192-0-2-2.sslip.io:8765",
        # the loopback literal is userinfo, not the host
        "http://localhost@evil.example/api",
        "http://127.0.0.1:4096@evil.example/",
        "http://[::1]@evil.example/",
        "http://127.0.0.1%40evil.example/",
        # not plain http, not a host, not loopback
        "https://127.0.0.1:4096",
        "http://10.0.0.9:4096",
        "http://0.0.0.0:4096",
        "http:///nohost",
        "file:///etc/passwd",
        "",
    ],
)
def test_anything_that_is_not_the_loopback_host_is_refused(server):
    assert eg._is_loopback(server) is False


def test_the_server_endpoint_is_the_parsed_host_and_port():
    assert eg.server_endpoint("http://127.0.0.1:4096") == "127.0.0.1:4096"
    assert eg.server_endpoint("http://127.0.0.1.192-0-2-2.sslip.io:8765") == (
        "127.0.0.1.192-0-2-2.sslip.io:8765"
    )
    # userinfo names a credential, not a destination, so it must not be echoed
    # back as though it were the host
    assert "evil.example" in eg.server_endpoint("http://127.0.0.1:4096@evil.example/")
    assert eg.server_endpoint("") == "unset"


def test_a_lookalike_loopback_server_is_a_posture_fault(tmp_path, monkeypatch):
    """The sslip.io case: a public name that merely begins with 127.0.0.1."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "x")
    pol = policy(destinations={"server": "http://127.0.0.1.192-0-2-2.sslip.io:8765"})
    assert any("loopback" in p for p in eg.posture(pol, tmp_path))


def test_the_audit_record_names_the_endpoint_that_received_the_payload(tmp_path, monkeypatch):
    _clean_posture(tmp_path, monkeypatch)
    pol_file = tmp_path / "p.json"
    pol_file.write_text(
        json.dumps(
            {
                "destinations": {"allow": ["openrouter/*"], "server": "http://127.0.0.1:4096"},
                "audit": {"path": str(tmp_path / "audit.jsonl")},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "stdin", _FakeStdin("ghp_" + "y" * 36))
    eg.main(
        [
            "--policy", str(pol_file), "--workspace", str(tmp_path),
            "preflight", "--stdin", "--model", "openrouter/x",
        ]
    )
    record = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip())
    assert record["server"] == "127.0.0.1:4096"


def test_an_unauthenticated_server_is_a_fault(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("OPENCODE_SERVER_PASSWORD", raising=False)
    assert any("OPENCODE_SERVER_PASSWORD" in p for p in eg.posture(policy(), tmp_path))


def test_a_workspace_outside_the_allowed_roots_is_a_fault(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "x")
    pol = policy(paths={"workspace_roots": [str(tmp_path / "allowed")]})
    (tmp_path / "allowed").mkdir()
    (tmp_path / "elsewhere").mkdir()
    assert any("workspace_roots" in p for p in eg.posture(pol, tmp_path / "elsewhere"))
    assert not [p for p in eg.posture(pol, tmp_path / "allowed") if "workspace_roots" in p]


# --- decision + audit ------------------------------------------------------


def _clean_posture(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "x")


def test_a_blocking_finding_blocks_the_dispatch(tmp_path, monkeypatch):
    _clean_posture(tmp_path, monkeypatch)
    pol = policy(destinations={"allow": ["openrouter/*"]})
    decision = eg._decide("token ghp_" + "z" * 36, pol, tmp_path, "openrouter/x")
    assert (decision.verdict, decision.exit_code) == ("blocked", 2)


def test_a_clean_payload_to_an_allowed_destination_passes(tmp_path, monkeypatch):
    _clean_posture(tmp_path, monkeypatch)
    pol = policy(destinations={"allow": ["openrouter/*"]})
    decision = eg._decide("rename the timer field", pol, tmp_path, "openrouter/x")
    assert (decision.verdict, decision.problems, decision.refusals) == ("allowed", [], [])
    assert decision.exit_code == 0


def test_an_oversized_payload_is_blocked(tmp_path, monkeypatch):
    _clean_posture(tmp_path, monkeypatch)
    pol = policy(destinations={"allow": ["openrouter/*"]}, limits={"max_payload_bytes": 16})
    decision = eg._decide("x" * 64, pol, tmp_path, "openrouter/x")
    assert decision.verdict == "blocked"
    assert any("max_payload_bytes" in r for r in decision.refusals)


def test_a_refused_destination_is_a_refusal_and_not_a_posture_fault(tmp_path, monkeypatch):
    """A wrapper keyed on `-eq 2` has to be able to tell the two apart."""
    _clean_posture(tmp_path, monkeypatch)
    pol = policy(destinations={"allow": ["anthropic/*"]})
    decision = eg._decide("rename the timer field", pol, tmp_path, "openrouter/x")
    assert decision.problems == []
    assert any("destinations.allow" in r for r in decision.refusals)
    assert (decision.verdict, decision.exit_code) == ("blocked", 2)


def test_a_posture_fault_is_a_fault_and_not_a_block(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("OPENCODE_SERVER_PASSWORD", raising=False)
    pol = policy(destinations={"allow": ["openrouter/*"]})
    decision = eg._decide("rename the timer field", pol, tmp_path, "openrouter/x")
    assert (decision.verdict, decision.exit_code) == ("fault", 3)


def test_the_dispatched_text_is_the_redacted_text(tmp_path, monkeypatch):
    _clean_posture(tmp_path, monkeypatch)
    pol = policy(destinations={"allow": ["openrouter/*"]})
    decision = eg._decide("ping ops@example.org", pol, tmp_path, "openrouter/x")
    assert "ops@example.org" not in decision.sent


# --- the documented exit-code contract -------------------------------------


def _contract_policy(tmp_path, **overlay) -> Path:
    data = {
        "destinations": {"allow": ["openrouter/*"], "server": "http://127.0.0.1:4096"},
        "audit": {"path": str(tmp_path / "audit.jsonl")},
    }
    eg._deep_update(data, overlay)
    target = tmp_path / "policy.json"
    target.write_text(json.dumps(data), encoding="utf-8")
    return target


@pytest.mark.parametrize(
    "label, overlay",
    [
        ("limits.max_payload_bytes is not a number", {"limits": {"max_payload_bytes": "lots"}}),
        ("destinations.allow is null", {"destinations": {"allow": None}}),
        ("unknown rule action", {"rules": {"jwt": "ignore"}}),
    ],
)
def test_a_malformed_policy_exits_three_without_a_traceback(
    label, overlay, tmp_path, monkeypatch, capsys
):
    _clean_posture(tmp_path, monkeypatch)
    pol_file = _contract_policy(tmp_path, **overlay)
    monkeypatch.setattr(sys, "stdin", _FakeStdin("just some prose"))
    code = eg.main(
        [
            "--policy", str(pol_file), "--workspace", str(tmp_path),
            "preflight", "--stdin", "--model", "openrouter/x",
        ]
    )
    assert code == 3, label
    assert "Traceback" not in capsys.readouterr().err


def test_malformed_policy_json_exits_three(tmp_path, monkeypatch, capsys):
    _clean_posture(tmp_path, monkeypatch)
    pol_file = tmp_path / "policy.json"
    pol_file.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", _FakeStdin("prose"))
    assert eg.main(["--policy", str(pol_file), "--workspace", str(tmp_path), "scan", "--stdin"]) == 3
    assert "Traceback" not in capsys.readouterr().err


def test_a_missing_explicit_policy_file_exits_three(tmp_path, monkeypatch):
    _clean_posture(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "stdin", _FakeStdin("prose"))
    missing = tmp_path / "nope.json"
    assert eg.main(["--policy", str(missing), "--workspace", str(tmp_path), "scan", "--stdin"]) == 3


def test_a_posture_fault_on_preflight_exits_three(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("OPENCODE_SERVER_PASSWORD", raising=False)
    pol_file = _contract_policy(tmp_path)
    monkeypatch.setattr(sys, "stdin", _FakeStdin("prose"))
    code = eg.main(
        [
            "--policy", str(pol_file), "--workspace", str(tmp_path),
            "preflight", "--stdin", "--model", "openrouter/x",
        ]
    )
    assert code == 3


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink")
def test_the_audit_path_is_not_followed_through_a_symlink(tmp_path):
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("", encoding="utf-8")
    link = tmp_path / "audit.jsonl"
    link.symlink_to(target)
    with pytest.raises(OSError):
        eg.audit(policy(audit={"path": str(link)}), {"verdict": "allowed"})
    assert target.read_text(encoding="utf-8") == ""


def test_audit_records_the_hash_and_not_the_payload(tmp_path):
    pol = policy(audit={"path": str(tmp_path / "audit.jsonl")})
    eg.audit(pol, {"verdict": "blocked", "payload_sha256": "abc", "findings": []})
    written = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert json.loads(written)["verdict"] == "blocked"


def test_audit_appends_rather_than_truncates(tmp_path):
    pol = policy(audit={"path": str(tmp_path / "audit.jsonl")})
    eg.audit(pol, {"verdict": "allowed"})
    eg.audit(pol, {"verdict": "blocked"})
    assert len((tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()) == 2


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_a_new_audit_file_is_owner_only(tmp_path):
    target = tmp_path / "audit.jsonl"
    eg.audit(policy(audit={"path": str(target)}), {"verdict": "allowed"})
    assert target.stat().st_mode & 0o077 == 0


# --- hook mode -------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        'node "/p/scripts/opencode-companion.mjs" task --model x',
        "opencode run 'summarise this repo'",
        "OPENCODE_PORT=1 opencode serve --port 4096",
    ],
)
def test_hook_refuses_a_direct_dispatch_that_would_skip_the_guard(command):
    assert eg._bypasses_guard(command) is True


@pytest.mark.parametrize("command", ["git diff", "python -m pytest", "node tools/build.mjs"])
def test_hook_leaves_ordinary_commands_alone(command):
    assert eg._bypasses_guard(command) is False


def test_hook_blocks_a_subagent_prompt_carrying_a_credential(tmp_path, monkeypatch, capsys):
    event = {
        "cwd": str(tmp_path),
        "tool_name": "Agent",
        "tool_input": {"prompt": "use AKIAIOSFODNN7EXAMPLE to reach the bucket"},
    }
    monkeypatch.setattr(sys, "stdin", _FakeStdin(json.dumps(event)))
    args = eg.build_parser().parse_args(["hook"])
    assert eg.cmd_hook(args) == 2
    assert "aws-access-key" in capsys.readouterr().err


def test_hook_passes_a_clean_subagent_prompt(tmp_path, monkeypatch):
    event = {"cwd": str(tmp_path), "tool_name": "Agent", "tool_input": {"prompt": "add a test"}}
    monkeypatch.setattr(sys, "stdin", _FakeStdin(json.dumps(event)))
    assert eg.cmd_hook(eg.build_parser().parse_args(["hook"])) == 0


class _FakeStdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


# --- cli -------------------------------------------------------------------


def test_scan_exits_non_zero_on_a_blocking_finding(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", _FakeStdin("AKIAIOSFODNN7EXAMPLE"))
    code = eg.main(["--workspace", str(tmp_path), "scan", "--stdin"])
    assert code == 2
    assert "aws-access-key" in capsys.readouterr().out


def test_scan_exits_zero_on_clean_input(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdin", _FakeStdin("just some prose"))
    assert eg.main(["--workspace", str(tmp_path), "scan", "--stdin"]) == 0


def test_dispatch_requires_a_model():
    with pytest.raises(SystemExit):
        eg.build_parser().parse_args(["dispatch", "--task", "x"])


def test_preflight_blocks_and_audits_without_sending(tmp_path, monkeypatch):
    _clean_posture(tmp_path, monkeypatch)
    pol_file = tmp_path / ".egress-policy.json"
    pol_file.write_text(
        json.dumps(
            {
                "destinations": {"allow": ["openrouter/*"]},
                "audit": {"path": str(tmp_path / "audit.jsonl")},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "stdin", _FakeStdin("ghp_" + "y" * 36))
    code = eg.main(
        [
            "--policy", str(pol_file),
            "--workspace", str(tmp_path),
            "preflight", "--stdin", "--model", "openrouter/x",
        ]
    )
    assert code == 2
    record = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip())
    assert record["verdict"] == "blocked"
    assert "github-token" in {f["rule"] for f in record["findings"]}
