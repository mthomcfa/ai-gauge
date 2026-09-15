"""The egress guard is the only thing standing between a delegated sub-task and
a third-party model provider, so the tests that matter are the ones that fail
when a detector, a destination check, or the audit trail stops working.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import urllib.request
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


@pytest.mark.parametrize(
    "line",
    [
        "token = self.next()",
        "secret = None",
        "password: str",
        'secret = keyring.get_password(KEYRING_SERVICE, "github-pat")',
        "api_key = get_openrouter_key()",
        "token = get_token(tenant_id, azure_cfg)",
        "secret_edit = QLineEdit()",
        'log.warning("secret_storage: refusing to write secrets on this host")',
        "self._secret_cb = QCheckBox(self)",
        "MAX_TOKENS = 4096",
    ],
)
def test_source_code_that_merely_mentions_a_credential_is_not_redacted(line):
    """Matching any eight characters after the `=` made a finding of every one
    of these: 22 hits on this repository's own source, 22 of them false, and
    the lines taken out of the payload were the lines the delegate was being
    asked about."""
    hits = [f for f in eg.scan(line, policy()) if f.rule == "secret-assignment"]
    assert not hits, f"{line!r} -> {[f.rule for f in eg.scan(line, policy())]}"


@pytest.mark.parametrize(
    "line, expected",
    [
        ("keyring.get_password('ai-gauge', 'github-pat')", True),
        ("keyring get ai-gauge openrouter-key", True),
        ("Remove the 'ai-gauge' / 'github-pat' credential from your keychain.", False),
        ("ai-gauge stores an azure-client-secret somewhere on this machine", False),
    ],
)
def test_the_keyring_rule_needs_the_lookup_and_not_the_words(line, expected):
    """This app's own error message named both halves in prose and blocked one
    of this repository's own recent diffs."""
    assert ("aigauge-keyring" in rules_hit(line)) is expected


@pytest.mark.parametrize(
    "line",
    ["+@responses.activate", "+@pytest.mark.parametrize", "-@functools.cache"],
)
def test_a_decorator_on_a_diff_line_is_not_an_email_address(line):
    assert "email-address" not in rules_hit(line)


@pytest.mark.parametrize(
    "line", ["+user@example.org", "-user@example.org", "person.name+tag@sub.example.co.uk"]
)
def test_an_address_on_a_diff_line_still_is_one(line):
    assert "email-address" in rules_hit(line)


@pytest.mark.parametrize(
    "name",
    ["secret_storage.py", "settings_dialog.py", "config.py"],
)
def test_this_repos_own_source_is_not_blocked_by_its_own_guard(name):
    """A choke point that refuses the repository it ships in is a choke point
    people route around. These three files carried every false positive the
    round-2 review measured: 22 `secret-assignment`, four `aigauge-keyring` and
    one `denied-path`, none of them a credential."""
    source = (REPO_ROOT / "src" / "aigauge" / name).read_text(encoding="utf-8")
    blocking = [f for f in eg.scan(source, policy()) if f.action == eg.BLOCK]
    assert not blocking, [(f.rule, source[f.start : f.end]) for f in blocking]


def test_an_unrecognised_high_entropy_blob_is_redacted_not_merely_warned():
    """The only net for credentials with no known shape used to let them past."""
    text = "value=aZ9+kQ/mN2xP7wL4tR6yU8iO0pA3sD5fG1hJ2kL4zX6c"
    findings = [f for f in eg.scan(text, policy()) if f.rule == "opaque-token"]
    assert findings and all(f.action == eg.REDACT for f in findings)


def test_the_report_names_a_warn_finding(capsys):
    text = "COMPANY CONFIDENTIAL - do not circulate"
    findings = eg.scan(text, policy())
    warned = [f for f in findings if f.action == eg.WARN]
    assert warned
    eg._report(findings, policy(), stream=sys.stderr)
    err = capsys.readouterr().err
    assert "classification-banner" in err
    assert "warn" in err
    # The per-finding line too, not only the by-rule summary: a `warn` finding
    # that is counted but never located is most of the way back to silent.
    assert f"at {warned[0].start}:" in err


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


@pytest.mark.parametrize(
    "path",
    [
        "~/.ssh/id_rsa",
        "~/.ssh/config",
        "%APPDATA%\\ai-gauge\\profiles\\claude\\Cookies",
        "C:/Users/m/AppData/Roaming/ai-gauge/secrets.dat",
        "~/.config/opencode/auth.json",
        "~/.local/share/keyrings/login.keyring",
        "C:\\Users\\m\\.AWS\\CREDENTIALS",  # case variant
        "/home/u/.aws/credentials",
    ],
)
def test_this_repos_own_secret_stores_are_denied(path):
    """SECURITY.md names these; a guard shipping in this repo denied none of
    them, and matching was case-sensitive on POSIX."""
    assert "denied-path" in rules_hit(f"please read {path}")


@pytest.mark.parametrize(
    "text",
    [
        "please read /srv/ops@example.org/.env and summarise",
        "please read /home/ops@example.org/.aws/credentials and summarise",
        "please read deploy/secrets/QmX9vT3kLpZr7bNw2Ys5Hd8Fg4Jc6Me1Ab and summarise",
        "please read /srv/QmX9vT3kLpZr7bNw2Ys5Hd8Fg4Jc6Me1AbCd/id_rsa and summarise",
    ],
)
def test_a_block_is_not_lost_to_an_overlapping_redaction(text):
    """Putting an `@` in a directory name turned a blocked path into an allowed
    dispatch: `email-address` claimed the span first and the path went out in
    the text. A `redact` must never silence a `block`."""
    findings = eg.scan(text, policy())
    assert any(f.rule == "denied-path" and f.action == eg.BLOCK for f in findings), (
        f"no blocking denied-path finding for {text!r}: {[(f.rule, f.action) for f in findings]}"
    )
    decision = eg._decide(
        text,
        policy(
            destinations={"allow": ["openrouter/*"], "server": "http://127.0.0.1:4096"},
            posture={"require_server_password": False},
        ),
        Path("."),
        "openrouter/x",
    )
    assert decision.verdict == "blocked"
    assert decision.exit_code == 2


def test_the_narrower_earlier_rule_still_wins_between_equal_claims():
    """Severity first, rule order second: ranking by severity alone would let a
    later rule of the same severity displace the one written to be specific."""
    findings = eg.scan("postgres://svc:hunter2@db.internal:5432/app", policy())
    assert [f.rule for f in findings] == ["connection-string"]


def test_a_denied_path_finding_does_not_carry_the_path(tmp_path):
    """`Finding.excerpt` promises it never carries the matched secret. The raw
    path carries the local username."""
    findings = eg.scan("please read /home/mthom/.aws/credentials now", policy())
    denied = [f for f in findings if f.rule == "denied-path"]
    assert denied
    for finding in denied:
        assert "mthom" not in finding.excerpt
        assert "/home/" not in finding.excerpt
        assert "credentials" in finding.excerpt


@pytest.mark.parametrize(
    "text, expected",
    [
        ("a/b", ["a/b"]),
        ("read deploy/secrets/x.yaml now", ["deploy/secrets/x.yaml"]),
        # The whole token, drive letter included: expanding outwards from the
        # separator stopped at the colon and reported a path nobody typed.
        (r"C:\Users\m\.ssh\id_rsa", [r"C:\Users\m\.ssh\id_rsa"]),
        ("trailing/ separator", []),
        ("no separators at all", []),
        ("please read '~/.ssh/id_rsa'", ["~/.ssh/id_rsa"]),
        ("%APPDATA%\\ai-gauge\\profiles", ["%APPDATA%\\ai-gauge\\profiles"]),
        # Sentence punctuation is not a path character, so a file named in
        # prose is not a path handed to an agent.
        ("stored in %APPDATA%/ai-gauge/secrets.dat, encrypted", []),
    ],
)
def test_path_spans_still_find_what_the_pattern_found(text, expected):
    assert [text[a:b] for a, b in eg._path_like_spans(text)] == expected


@pytest.mark.parametrize("pad", [0, 1, 100, 200, 239, 240, 245, 250, 251, 255, 300, 600, 5000])
def test_no_padding_width_hides_a_denied_path(pad):
    """The 256-character window dropped a denied path in a deterministic band:
    `/<240..250 a's>/.aws/credentials` passed the deny list, and `/home/<247..250
    a's>/.env` with it, because the window ceiling fell between the two segments
    the glob needs."""
    for text in (
        "/" + "a" * pad + "/.aws/credentials",
        "/home/" + "a" * pad + "/.env",
        "/home/" + "a" * pad + "/.ssh/id_rsa",
    ):
        assert eg._scan_paths(text, policy()), f"pad={pad} {text[:20]}... passed the deny list"


def test_a_token_longer_than_any_path_is_still_scanned():
    """A payload with no whitespace in it is one token; windowing it is what
    keeps the path at the end of it from being skipped as too long."""
    assert eg._scan_paths("x/" * 6000 + "/home/u/.aws/credentials", policy())
    assert eg._scan_paths("x/" * 6000 + "deploy/secrets/prod.yaml", policy())


def test_a_long_separator_free_run_scans_quickly():
    """The pattern this replaces took 22.2 s at 64 000 characters and did not
    finish at the tool's own 400 000-byte default cap."""
    import time

    payload = "A" * 400_000
    started = time.monotonic()
    eg.scan(payload, policy())
    # Generous by two orders of magnitude, so CI variance cannot flake it.
    assert time.monotonic() - started < 10.0


# `"A" * n` is the one filler that cannot trigger a backtracking detector: a run
# of word characters has exactly one word boundary in it. Round 1 tested with
# that filler alone and `email-address` stayed quadratic behind it - 122.9 s on
# 400 KB of `a.-`, 185.2 s on `x.`, 92.5 s on `%20x`, 73.7 s on a plausible
# `svc.0-svc.1-...` identifier list. Each filler below is a separator run the
# detector classes reach into, and the payload is the tool's own default cap.
_ADVERSARIAL_FILLERS = {
    "word run": "A",
    "dot dash": "a.-",
    "dotted": "x.",
    "url encoded": "%20x",
    "at signs": "@",
    "dots": ".",
    "dashes": "-",
    "underscores": "_",
    "equals": "=",
    "colons": ":",
    "plus": "+",
    "slashes": "a/",
    "email shaped": "a@b.c-",
    "url shaped": "http://a:b",
    "dsn shaped": "postgres://a:",
    "assignment shaped": "password=",
    "keyring shaped": "keyring ai-gauge x ",
    "mixed alphabet": "aZ9._%+-@:/=~ \t\n",
}


def _filled(filler: str, size: int = 400_000) -> str:
    return (filler * (size // len(filler) + 1))[:size]


def _adversarial_payloads() -> list[tuple[str, str]]:
    payloads = [(label, _filled(filler)) for label, filler in _ADVERSARIAL_FILLERS.items()]
    # A payload nobody would call adversarial: a list of dotted, hyphenated
    # service identifiers. It cost 73.7 s.
    payloads.append(("identifier list", "-".join(f"svc.{i}" for i in range(80_000))[:400_000]))
    return payloads


@pytest.mark.parametrize("label, payload", _adversarial_payloads())
def test_every_scanning_pattern_is_linear_on_400kb(label, payload):
    """Every detector, every filler, `finditer` rather than `search`.

    `search` stops at the first match, which hides the cost of the failures
    after it; the scanner uses `finditer`, so the test has to.
    """
    import re
    import time

    for rule, _action, pattern in eg._DETECTORS:
        started = time.monotonic()
        list(re.finditer(pattern, payload))
        # The machine budget is 1 s; CI variance is far below the headroom
        # between that and the 122-185 s this replaces.
        assert time.monotonic() - started < 5.0, f"{rule} on {label}"
    started = time.monotonic()
    list(eg._OPAQUE_TOKEN_RE.finditer(payload))
    assert time.monotonic() - started < 5.0, f"opaque-token on {label}"
    started = time.monotonic()
    list(eg._path_like_spans(payload))
    assert time.monotonic() - started < 5.0, f"path spans on {label}"
    started = time.monotonic()
    eg._bypasses_guard(payload)
    assert time.monotonic() - started < 5.0, f"bypass regex on {label}"


@pytest.mark.parametrize("label, payload", _adversarial_payloads())
def test_scan_is_linear_on_400kb_of_every_filler(label, payload):
    import time

    started = time.monotonic()
    eg.scan(payload, policy())
    assert time.monotonic() - started < 5.0, label


@pytest.mark.parametrize("label, payload", _adversarial_payloads())
def test_the_hook_answers_a_400kb_prompt_inside_its_documented_timeout(
    label, payload, tmp_path, monkeypatch
):
    """The wiring in the doc gives the hook `"timeout": 10`. A hook killed at
    its timeout returns no exit 2, so the tool call proceeds unscanned."""
    import time

    event = json.dumps({"cwd": str(tmp_path), "tool_name": "Task",
                        "tool_input": {"prompt": payload}})
    monkeypatch.setattr(sys, "stdin", _FakeStdin(event))
    args = eg.build_parser().parse_args(["hook"])
    started = time.monotonic()
    eg.cmd_hook(args)
    assert time.monotonic() - started < 5.0, label


def _quantifiers(pattern: str, verbose: bool = False):
    """Every quantifier in `pattern`, as (position, text, bounded_or_possessive)."""
    index = 0
    in_class = False
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            index += 2
            continue
        if in_class:
            in_class = char != "]"
            index += 1
            continue
        if char == "[":
            in_class = True
            index += 1
            continue
        if verbose and char == "#":
            newline = pattern.find("\n", index)
            index = len(pattern) if newline == -1 else newline + 1
            continue
        if char == "{":
            close = pattern.find("}", index)
            body = pattern[index + 1 : close] if close != -1 else ""
            if body and all(c.isdigit() or c == "," for c in body):
                possessive = pattern[close + 1 : close + 2] == "+"
                yield index, "{" + body + "}", not body.endswith(",") or possessive
                index = close + 2 if possessive else close + 1
                continue
            index += 1
            continue
        if char in "*+" and not (index and pattern[index - 1] == "("):
            # `*+`/`++` is possessive and `*?`/`+?` lazy - both consume the next
            # character, which is not a quantifier of its own.
            modifier = pattern[index + 1 : index + 2]
            yield index, char, modifier == "+"
            index += 2 if modifier in "+?" else 1
            continue
        index += 1


@pytest.mark.parametrize(
    "label, pattern, verbose",
    [(rule, pattern, False) for rule, _a, pattern in eg._DETECTORS]
    + [
        ("opaque-token", eg._OPAQUE_TOKEN_RE.pattern, False),
        ("bypass", eg._BYPASS_RE.pattern, True),
        ("base ref", eg._BASE_REF_RE.pattern, False),
    ],
)
def test_every_quantifier_is_bounded_or_possessive(label, pattern, verbose):
    """The class-level pin, not the instance.

    A quadratic detector is a hook that cannot answer inside its timeout, which
    is a tool call that proceeds unscanned. The timing tests above catch the
    fillers someone thought of; this one catches the pattern nobody timed.
    """
    unbounded = [
        (position, text) for position, text, ok in _quantifiers(pattern, verbose) if not ok
    ]
    assert not unbounded, (
        f"{label}: unbounded quantifier(s) {unbounded} - bound it ({{m,n}}) or make it "
        f"possessive ({{m,}}+), or a run of separators makes it quadratic"
    )


def test_the_payload_is_truncated_to_the_cap_before_it_is_scanned():
    pol = policy(limits={"max_payload_bytes": 16})
    scanned, was_truncated = eg.truncate("x" * 64, pol)
    assert was_truncated is True
    assert len(scanned) == 16
    assert eg.truncate("short", pol) == ("short", False)


def test_a_credential_past_the_cap_is_not_what_gets_scanned():
    """A cap that does not gate the scan bounds nothing; one that does has to
    be honest that it did not look at the rest."""
    pol = policy(limits={"max_payload_bytes": 32})
    scanned, was_truncated = eg.truncate("A" * 64 + " AKIAIOSFODNN7EXAMPLE", pol)
    assert was_truncated is True
    assert "AKIA" not in scanned


def test_the_hook_scans_at_most_the_cap_and_blocks_what_it_could_not_read(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / ".egress-policy.json").write_text(
        json.dumps({"limits": {"max_payload_bytes": 64}}), encoding="utf-8"
    )
    event = {
        "cwd": str(tmp_path),
        "tool_name": "Agent",
        "tool_input": {"prompt": "A" * 400_000},
    }
    monkeypatch.setattr(sys, "stdin", _FakeStdin(json.dumps(event)))
    import time

    started = time.monotonic()
    code = eg.cmd_hook(eg.build_parser().parse_args(["hook"]))
    assert time.monotonic() - started < 10.0
    assert code == 2
    assert "max_payload_bytes" in capsys.readouterr().err


def test_overlapping_findings_do_not_corrupt_the_redacted_text():
    """`redact()` assumes non-overlapping spans; path findings used to skip the
    overlap filter, which chewed the placeholder of whichever was written second."""
    pol = policy(rules={"denied-path": "redact"})
    text = "backup at /srv/ops@example.org/.env now"
    out, _ = eg.redact(text, eg.scan(text, pol))
    assert out.count("[redacted:") == out.count("]")
    assert "ops@example.org" not in out or ".env" not in out
    assert "dacted:" not in out.replace("[redacted:", "")


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
    assert "@" not in out.replace("[redacted:email-address]", "")


def test_the_placeholder_sent_to_the_provider_carries_no_hash_of_the_value():
    """An unsalted sha256 prefix of a low-entropy value is a dictionary-
    verifiable oracle, handed to the party the redaction exists to keep it from."""
    import hashlib

    value = "alice.mcgregor@example.org"
    text = f"contact {value} today"
    out, _ = eg.redact(text, eg.scan(text, policy()))
    assert out == "contact [redacted:email-address] today"
    assert hashlib.sha256(value.encode()).hexdigest()[:8] not in out


def test_the_local_report_handle_is_salted_per_run_and_not_an_exact_length():
    import hashlib

    value = "alice.mcgregor@example.org"
    masked = eg._mask(value)
    assert value not in masked
    assert hashlib.sha256(value.encode()).hexdigest()[:8] not in masked
    assert f"{len(value)} chars" not in masked
    assert eg._mask(value) == masked, "stable within a run"


def test_the_dispatch_authenticates_with_the_password_posture_demands(monkeypatch):
    """posture requires OPENCODE_SERVER_PASSWORD; dispatch could not use it, so
    a server that enforced it answered 401 and the check became a ritual."""
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "hunter2")
    monkeypatch.setenv("OPENCODE_SERVER_USERNAME", "opencode")
    headers = eg._server_auth_headers()
    assert headers["Authorization"].startswith("Basic ")
    import base64

    assert base64.b64decode(headers["Authorization"].split()[1]) == b"opencode:hunter2"
    monkeypatch.delenv("OPENCODE_SERVER_PASSWORD")
    assert eg._server_auth_headers() == {}


class _FakeResponse:
    def __init__(self, url, payload=b'{"id": "s1", "ok": true}'):
        self.url = url
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    """Stands in for the module's opener and remembers every Request."""

    def __init__(self, landing_url=None):
        self.requests = []
        self._landing_url = landing_url

    def open(self, request, timeout=None):
        self.requests.append(request)
        return _FakeResponse(self._landing_url or request.full_url)


def test_a_redirect_is_refused_and_names_where_it_was_being_sent():
    """`posture` proves the configured server is loopback; it says nothing
    about where the connection ends up. A loopback server that answers
    `/session` and then 302s the message POST sent this process off the box
    with the `Authorization: Basic` header attached."""
    handler = eg._NoRedirects()
    with pytest.raises(eg._RedirectRefused) as raised:
        handler.redirect_request(
            urllib.request.Request("http://127.0.0.1:4096/session/s1/message"),
            None, 302, "Found", {},
            "http://192.0.2.2:8777/session/s1/message",
        )
    assert "192.0.2.2:8777" in str(raised.value)


def test_the_opener_refuses_redirects_and_ignores_a_proxy():
    kinds = {type(handler).__name__ for handler in eg._OPENER.handlers}
    assert "_NoRedirects" in kinds
    assert not any(
        name == "HTTPRedirectHandler" for name in kinds
    ), "a handler that follows redirects is installed"
    # An empty ProxyHandler registers no methods, so it does not appear in
    # `handlers` - what matters is that no handler carries a proxy, which is
    # what passing it kept `build_opener` from adding from the environment.
    assert not any(getattr(handler, "proxies", None) for handler in eg._OPENER.handlers)


def test_nothing_is_sent_to_a_host_that_is_not_the_pinned_one(monkeypatch):
    opener = _FakeOpener()
    monkeypatch.setattr(eg, "_OPENER", opener)
    with pytest.raises(eg._RedirectRefused):
        eg._post_json("http://192.0.2.2:8777/session", {}, 5, "127.0.0.1:4096")
    assert opener.requests == [], "the request was built and sent anyway"


def test_a_response_from_somewhere_else_is_refused(monkeypatch):
    monkeypatch.setattr(eg, "_OPENER", _FakeOpener(landing_url="http://192.0.2.2:8777/session"))
    with pytest.raises(eg._RedirectRefused) as raised:
        eg._post_json("http://127.0.0.1:4096/session", {}, 5, "127.0.0.1:4096")
    assert "192.0.2.2:8777" in str(raised.value)


def test_a_redirected_dispatch_exits_three_and_records_the_target(tmp_path, monkeypatch):
    _clean_posture(tmp_path, monkeypatch)
    pol_file = tmp_path / "policy.json"
    pol_file.write_text(
        json.dumps(
            {
                "destinations": {"allow": ["openrouter/*"], "server": "http://127.0.0.1:4096"},
                "audit": {"path": str(tmp_path / "audit.jsonl")},
            }
        ),
        encoding="utf-8",
    )

    def post(url, body, timeout, endpoint=None):
        raise eg._RedirectRefused("redirect to 192.0.2.2:8777 refused")

    monkeypatch.setattr(eg, "_post_json", post)
    monkeypatch.setattr(sys, "stdin", _FakeStdin("rename the timer field"))
    args = eg.build_parser().parse_args(
        [
            "--policy", str(pol_file), "--workspace", str(tmp_path),
            "dispatch", "--stdin", "--model", "openrouter/x", "--timeout", "5",
        ]
    )
    assert eg.cmd_dispatch(args) == 3
    result = [line for line in _audit_lines(tmp_path) if line.get("stage") == "result"]
    assert result and result[0]["verdict"] == "error"
    assert "192.0.2.2:8777" in result[0]["error"]


def test_the_dispatch_puts_the_password_posture_demands_on_the_request(tmp_path, monkeypatch):
    """The M13 test asserted on `_server_auth_headers()`'s return value, so
    deleting the header from the request survived the whole suite."""
    import base64

    _clean_posture(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "hunter2")
    monkeypatch.setenv("OPENCODE_SERVER_USERNAME", "opencode")
    opener = _FakeOpener()
    monkeypatch.setattr(eg, "_OPENER", opener)
    pol_file = tmp_path / "policy.json"
    pol_file.write_text(
        json.dumps(
            {
                "destinations": {"allow": ["openrouter/*"], "server": "http://127.0.0.1:4096"},
                "audit": {"path": str(tmp_path / "audit.jsonl")},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "stdin", _FakeStdin("rename the timer field"))
    args = eg.build_parser().parse_args(
        [
            "--policy", str(pol_file), "--workspace", str(tmp_path),
            "dispatch", "--stdin", "--model", "openrouter/x", "--timeout", "5",
        ]
    )
    assert eg.cmd_dispatch(args) == 0
    assert opener.requests, "nothing was sent"
    for request in opener.requests:
        assert eg.server_endpoint(request.full_url) == "127.0.0.1:4096"
        header = request.get_header("Authorization")
        assert header and header.startswith("Basic ")
        assert base64.b64decode(header.split()[1]) == b"opencode:hunter2"


def test_no_unused_network_primitive_ships():
    """In a tool whose mandate is a single pinned egress hop, an outbound HTTP
    helper that nothing calls should not be in the file."""
    assert not hasattr(eg, "_get_json")


# --- destinations ----------------------------------------------------------


def test_an_empty_allowlist_permits_nothing():
    assert eg.destination_allowed("openrouter/anthropic/claude-sonnet-4.5", policy()) is False


def test_only_allowlisted_destinations_pass():
    pol = policy(destinations={"allow": ["openrouter/*"]})
    assert eg.destination_allowed("openrouter/meta-llama/llama-3.3-70b", pol) is True
    assert eg.destination_allowed("anthropic/claude-sonnet-4.5", pol) is False


@pytest.mark.parametrize(
    "model, expected",
    [
        ("OPENROUTER/x", True),  # fnmatch normcased only on Windows before
        ("openrouter/x", True),
        ("", False),
        ("openrouter/x\nanthropic/evil", False),  # one allowed id smuggling another
        ("anthropic/x", False),
    ],
)
def test_destination_matching_is_the_same_on_every_platform(model, expected):
    pol = policy(destinations={"allow": ["openrouter/*"]})
    assert eg.destination_allowed(model, pol) is expected


def test_a_non_list_allowlist_is_a_fault_not_a_crash():
    with pytest.raises(SystemExit):
        eg.destination_allowed("openrouter/x", policy(destinations={"allow": None}))


def test_the_shipped_example_policy_names_no_destination():
    """A copy-and-edit starting policy must start empty: a broker-wide wildcard
    makes the recipient a routing outcome rather than a decision."""
    example = json.loads((REPO_ROOT / "tools" / "egress-policy.example.json").read_text("utf-8"))
    assert example["destinations"]["allow"] == []


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
    """The name is the claim, so the test builds a record from a real payload
    and asserts the payload is not in the line. Handing `audit()` a literal
    `{"payload_sha256": "abc"}` asserted nothing: a mutation that appended the
    whole raw payload to every line survived it."""
    import hashlib

    payload = "the quick brown MARKER-SECRET-VALUE jumps over"
    pol = policy(audit={"path": str(tmp_path / "audit.jsonl")})
    eg.audit(
        pol,
        {
            "verdict": "blocked",
            "payload_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "payload_bytes": len(payload.encode("utf-8")),
            "findings": [f.as_record() for f in eg.scan(payload, pol)],
        },
    )
    written = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert json.loads(written)["verdict"] == "blocked"
    assert "MARKER-SECRET-VALUE" not in written
    assert payload not in written


def test_a_dispatch_audit_line_never_carries_the_payload(tmp_path, monkeypatch):
    payload = "ping ops@example.org about MARKER-SECRET-VALUE"
    code, _recorder = _dispatch(tmp_path, monkeypatch, payload)
    assert code == 0
    written = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "MARKER-SECRET-VALUE" not in written
    assert "ops@example.org" not in written


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


# --- gathering the diff ----------------------------------------------------


def _git(repo: Path, *args: str):
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )


def _git_repo(tmp_path) -> Path:
    """A throwaway repository: `main` at one commit, HEAD one commit ahead of
    it, and an uncommitted change on top of that."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.org")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "one")
    _git(repo, "checkout", "-q", "-b", "work")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "two")
    (repo / "a.txt").write_text("UNCOMMITTED\n", encoding="utf-8")
    return repo


def _sentinel_script(repo: Path, sentinel: Path) -> Path:
    if sys.platform == "win32":  # pragma: no cover - the driver shape differs
        driver = repo.parent / "evil.bat"
        driver.write_text(f"@echo ran >> {sentinel}\n", encoding="utf-8")
        return driver
    driver = repo.parent / "evil.sh"
    driver.write_text(f'#!/bin/sh\necho ran >> "{sentinel}"\nexit 0\n', encoding="utf-8")
    driver.chmod(0o755)
    return driver


def _plant_external_diff(repo: Path, sentinel: Path) -> None:
    _git(repo, "config", "diff.external", str(_sentinel_script(repo, sentinel)))


# Every way a repository's own files can name a command for git to run. Round 1
# closed the first four; `core.fsmonitor`, `filter.*.clean`, `filter.*.process`
# and `core.hooksPath` each still ran a planted script, four times per run, with
# `verdict: allowed`, exit 0 and nothing in the audit trail.
_GIT_EXECUTION_VECTORS = (
    "diff.external",
    "diff.driver.command",
    "diff.driver.textconv",
    "core.pager",
    "core.fsmonitor",
    "filter.driver.clean",
    "filter.driver.process",
    "core.hooksPath",
    "core.sshCommand",
    "uploadpack.packObjectsHook",
    "GIT_EXTERNAL_DIFF",
    "GIT_CONFIG_PARAMETERS",
)


def _plant(repo: Path, sentinel: Path, vector: str, monkeypatch) -> None:
    driver = _sentinel_script(repo, sentinel)
    if vector == "diff.external":
        _git(repo, "config", "diff.external", str(driver))
    elif vector == "diff.driver.command":
        _git(repo, "config", "diff.pwn.command", str(driver))
        (repo / ".gitattributes").write_text("a.txt diff=pwn\n", encoding="utf-8")
    elif vector == "diff.driver.textconv":
        _git(repo, "config", "diff.pwn.textconv", str(driver))
        (repo / ".gitattributes").write_text("a.txt diff=pwn\n", encoding="utf-8")
    elif vector == "core.pager":
        _git(repo, "config", "core.pager", str(driver))
    elif vector == "core.fsmonitor":
        _git(repo, "config", "core.fsmonitor", str(driver))
    elif vector == "filter.driver.clean":
        _git(repo, "config", "filter.pwn.clean", str(driver))
        (repo / ".gitattributes").write_text("a.txt filter=pwn\n", encoding="utf-8")
    elif vector == "filter.driver.process":
        _git(repo, "config", "filter.pwn.process", str(driver))
        (repo / ".gitattributes").write_text("a.txt filter=pwn\n", encoding="utf-8")
    elif vector == "core.hooksPath":
        hooks = repo.parent / "hooks"
        hooks.mkdir(exist_ok=True)
        for name in ("post-index-change", "pre-auto-gc", "post-checkout"):
            hook = hooks / name
            hook.write_text(driver.read_text(encoding="utf-8"), encoding="utf-8")
            hook.chmod(0o755)
        _git(repo, "config", "core.hooksPath", str(hooks))
    elif vector == "core.sshCommand":
        _git(repo, "config", "core.sshCommand", str(driver))
    elif vector == "uploadpack.packObjectsHook":
        _git(repo, "config", "uploadpack.packObjectsHook", str(driver))
    elif vector == "GIT_EXTERNAL_DIFF":
        monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(driver))
    elif vector == "GIT_CONFIG_PARAMETERS":
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'diff.external={driver}'")
    else:  # pragma: no cover - a vector with no plant is a typo
        raise AssertionError(vector)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX driver script and exec bit")
@pytest.mark.parametrize("vector", _GIT_EXECUTION_VECTORS)
def test_include_diff_runs_nothing_the_workspace_configured(vector, tmp_path, monkeypatch):
    """`--include-diff` was arbitrary command execution in the operator's
    context: any process that can write the repo can plant one of these, and
    the delegated agent runs with `edit: allow`."""
    repo = _git_repo(tmp_path)
    sentinel = tmp_path / "ran"
    _plant(repo, sentinel, vector, monkeypatch)
    args = eg.build_parser().parse_args(["scan", "--include-diff", "--base", "main"])
    eg.build_payload(args, repo)
    assert not sentinel.exists(), f"{vector} executed a workspace-planted command"
    # And the shape that used to reach the worktree: a clean filter runs when
    # git has to turn a worktree file into a blob, which is what `git diff` with
    # no revision and `git status` both did. There is no such call now, so the
    # missing base is a fault - and still nothing runs.
    bare = eg.build_parser().parse_args(["scan", "--include-diff"])
    with pytest.raises(SystemExit):
        eg.build_payload(bare, repo)
    assert not sentinel.exists(), f"{vector} executed on the worktree path"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX driver script and exec bit")
@pytest.mark.parametrize("vector", _GIT_EXECUTION_VECTORS)
def test_a_whole_preflight_runs_nothing_the_workspace_configured(
    vector, tmp_path, monkeypatch
):
    """The same plants through the command that gathers, decides and audits:
    the round-2 review measured four executions per run on this path."""
    repo = _git_repo(tmp_path)
    sentinel = tmp_path / "ran"
    _plant(repo, sentinel, vector, monkeypatch)
    _clean_posture(tmp_path, monkeypatch)
    pol_file = tmp_path / "policy.json"
    pol_file.write_text(
        json.dumps(
            {
                "destinations": {"allow": ["openrouter/*"], "server": "http://127.0.0.1:4096"},
                "audit": {"path": str(tmp_path / "audit.jsonl")},
            }
        ),
        encoding="utf-8",
    )
    code = eg.main(
        [
            "--policy", str(pol_file), "--workspace", str(repo),
            "preflight", "--include-diff", "--base", "main", "--model", "openrouter/x",
        ]
    )
    assert code == 0
    assert not sentinel.exists(), f"{vector} executed a workspace-planted command"


def test_include_diff_without_a_base_is_a_fault(tmp_path):
    """The guard diffs commit trees, never the worktree; without a base there
    is no tree to diff, and falling back to the worktree is what ran the
    workspace's clean filter."""
    repo = _git_repo(tmp_path)
    args = eg.build_parser().parse_args(["scan", "--include-diff"])
    with pytest.raises(SystemExit):
        eg.build_payload(args, repo)


def test_the_diff_is_between_commits_and_not_the_worktree(tmp_path):
    repo = _git_repo(tmp_path)
    args = eg.build_parser().parse_args(["scan", "--include-diff", "--base", "main"])
    payload = eg.build_payload(args, repo)
    assert "two" in payload
    assert "UNCOMMITTED" not in payload


def test_git_is_never_asked_for_a_worktree_diff_or_a_status(tmp_path, monkeypatch):
    """A filter driver can be called anything, so `-c filter.<name>.clean=` can
    never be complete; not reading the worktree is what closes the class."""
    repo = _git_repo(tmp_path)
    calls: list[tuple[str, ...]] = []

    def record(workspace, *args):
        calls.append(args)
        return "0000000000000000000000000000000000000000\n" if args[0] == "rev-parse" else ""

    monkeypatch.setattr(eg, "_git", record)
    args = eg.build_parser().parse_args(["scan", "--include-diff", "--base", "main"])
    eg.build_payload(args, repo)
    assert calls, "no git call at all"
    assert not any(call[0] == "status" for call in calls)
    for call in calls:
        if call[0] == "diff":
            assert any("...HEAD" in part for part in call), call


def test_the_git_child_runs_with_no_git_environment_variables(monkeypatch):
    """`GIT_EXTERNAL_DIFF`, `GIT_PAGER`, `GIT_SSH`, `GIT_ASKPASS` and
    `GIT_CONFIG_PARAMETERS` each name a program or inject configuration."""
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", "/tmp/evil.sh")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'diff.external=/tmp/evil.sh'")
    monkeypatch.setenv("GIT_PAGER", "/tmp/evil.sh")
    monkeypatch.setenv("SSH_ASKPASS", "/tmp/evil.sh")
    env = eg._git_env()
    assert "GIT_EXTERNAL_DIFF" not in env
    assert "GIT_CONFIG_PARAMETERS" not in env
    assert "GIT_PAGER" not in env
    assert "SSH_ASKPASS" not in env
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_SYSTEM"] == os.devnull
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"


@pytest.mark.parametrize(
    "key",
    ["core.fsmonitor", "core.hooksPath", "core.pager", "diff.external", "diff.textconv"],
)
def test_every_enumerable_command_key_is_pinned_on_the_argv(key, tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    seen: list[list[str]] = []
    import subprocess as _subprocess

    def record(argv, **kwargs):
        seen.append(argv)
        return _subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(eg.subprocess, "run", record)
    eg._git(repo, "diff")
    assert seen
    pins = [argv[index + 1] for argv in seen for index, part in enumerate(argv) if part == "-c"]
    assert any(pin.startswith(f"{key}=") for pin in pins), pins


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_include_diff_does_not_rewrite_the_workspace_index(tmp_path):
    """`git status` refreshed and rewrote `.git/index` on every run - the one
    thing the tool wrote inside the repository it was asked only to read."""
    repo = _git_repo(tmp_path)
    index = repo / ".git" / "index"
    os.utime(index, (1_577_836_800, 1_577_836_800))  # 2020-01-01
    before = index.stat().st_mtime
    args = eg.build_parser().parse_args(["scan", "--include-diff", "--base", "main"])
    eg.build_payload(args, repo)
    assert index.stat().st_mtime == before


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX diff driver script")
def test_a_refused_destination_never_reaches_git(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    sentinel = tmp_path / "ran"
    _plant_external_diff(repo, sentinel)
    _clean_posture(tmp_path, monkeypatch)
    pol_file = tmp_path / "policy.json"
    pol_file.write_text(
        json.dumps(
            {
                "destinations": {"allow": ["anthropic/*"], "server": "http://127.0.0.1:4096"},
                "audit": {"path": str(tmp_path / "audit.jsonl")},
            }
        ),
        encoding="utf-8",
    )
    code = eg.main(
        [
            "--policy", str(pol_file), "--workspace", str(repo),
            "preflight", "--include-diff", "--base", "main", "--model", "openrouter/x",
        ]
    )
    assert code == 2
    assert not sentinel.exists()
    record = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip())
    assert record["payload_bytes"] is None


@pytest.mark.parametrize(
    "base",
    ["--output=OUTFILE", "--ext-diff", "-c", "origin/main HEAD", "a;b", "--", "$(id)"],
)
def test_a_base_that_is_not_a_plain_ref_is_refused(base, tmp_path):
    repo = _git_repo(tmp_path)
    args = eg.build_parser().parse_args(["scan", "--include-diff", f"--base={base}"])
    with pytest.raises(SystemExit):
        eg.build_payload(args, repo)


@pytest.mark.parametrize("base", ["--output=OUTFILE", "--ext-diff", "a;b", "$(id)"])
def test_a_base_that_is_not_a_plain_ref_never_reaches_git(base, tmp_path, monkeypatch):
    """The shape check is the control; `rev-parse` behind it is the second
    line, and a test that only sees the exception cannot tell them apart."""
    repo = _git_repo(tmp_path)
    calls: list[tuple] = []
    monkeypatch.setattr(eg, "_git", lambda workspace, *args: calls.append(args) or "")
    args = eg.build_parser().parse_args(["scan", "--include-diff", f"--base={base}"])
    with pytest.raises(SystemExit):
        eg.build_payload(args, repo)
    assert not any("rev-parse" in call for call in calls)


def test_a_base_that_names_no_commit_is_refused(tmp_path):
    repo = _git_repo(tmp_path)
    args = eg.build_parser().parse_args(["scan", "--include-diff", "--base", "no-such-ref"])
    with pytest.raises(SystemExit):
        eg.build_payload(args, repo)


def test_a_failed_git_call_is_a_fault_not_an_empty_diff(tmp_path):
    """A user who believes they preflighted a diff must not have preflighted a
    nine-byte status line."""
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    args = eg.build_parser().parse_args(["scan", "--include-diff", "--base", "main"])
    with pytest.raises(SystemExit):
        eg.build_payload(args, not_a_repo)


def test_a_good_base_still_produces_a_diff(tmp_path):
    repo = _git_repo(tmp_path)
    args = eg.build_parser().parse_args(["scan", "--include-diff", "--base", "main"])
    payload = eg.build_payload(args, repo)
    assert "a.txt" in payload


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


@pytest.mark.parametrize(
    "command",
    [
        "npx opencode-ai run x",
        "npx -y opencode-ai@latest run x",
        "bunx opencode-ai run x",
        "opencode --print-logs run x",
        "$(which opencode) run x",
        "oc=opencode; $oc run x",
        "opencode server",
        "/usr/local/bin/opencode run x",
        "cd /srv && opencode run x",
        "opencode auth login",
    ],
)
def test_hook_refuses_the_ordinary_invocation_forms_too(command):
    """`run`/`serve` had to follow `opencode` immediately, so any flag between
    them walked through, as did the npm package name."""
    assert eg._bypasses_guard(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "git diff",
        "python -m pytest",
        "node tools/build.mjs",
        "grep -r opencode docs/",
        "cat docs/opencode-plugin-evaluation.md",
    ],
)
def test_hook_leaves_ordinary_commands_alone(command):
    assert eg._bypasses_guard(command) is False


@pytest.mark.parametrize(
    "label, stdin_text",
    [
        ("empty stdin", ""),
        ("whitespace only", "   \n"),
        ("not JSON at all", "hello there"),
        ("a JSON array", "[1,2,3]"),
        ("truncated JSON", '{"tool_name": "Bash", "tool_in'),
        ("null tool_input", '{"tool_name": "Bash", "tool_input": null}'),
        ("tool_input is a string", '{"tool_name": "Bash", "tool_input": "rm -rf /"}'),
    ],
)
def test_hook_fails_closed_on_malformed_input(label, stdin_text, monkeypatch):
    """A guard that crashes, or that answers 0 because it could not parse its
    own input, is a guard that let the call through."""
    monkeypatch.setattr(sys, "stdin", _FakeStdin(stdin_text))
    assert eg.cmd_hook(eg.build_parser().parse_args(["hook"])) == 2, label


def test_hook_fails_closed_on_a_malformed_workspace_policy(tmp_path, monkeypatch):
    (tmp_path / ".egress-policy.json").write_text("{not json", encoding="utf-8")
    event = {
        "cwd": str(tmp_path),
        "tool_name": "Agent",
        "tool_input": {"prompt": "use AKIAIOSFODNN7EXAMPLE to reach the bucket"},
    }
    monkeypatch.setattr(sys, "stdin", _FakeStdin(json.dumps(event)))
    assert eg.cmd_hook(eg.build_parser().parse_args(["hook"])) == 2


def test_hook_checks_a_command_even_when_the_event_does_not_name_its_tool(monkeypatch):
    event = {"tool_input": {"command": "opencode run 'summarise this repo'"}}
    monkeypatch.setattr(sys, "stdin", _FakeStdin(json.dumps(event)))
    assert eg.cmd_hook(eg.build_parser().parse_args(["hook"])) == 2


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


# --- cmd_dispatch ----------------------------------------------------------
#
# Nothing used to invoke cmd_dispatch. Three mutations that gut the dispatch
# path - dropping the destination check from `_decide`, posting the raw payload
# instead of the redacted one, and returning 0 where it blocked - all survived
# the full suite, because the tests covering those behaviours tested a pure
# function nothing asserted was wired in, or `_decide`'s return value rather
# than what is posted.


class _Recorder:
    """Stands in for `_post_json` and remembers every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.endpoints: list[str | None] = []

    def __call__(self, url, body, timeout, endpoint=None):
        self.calls.append((url, body))
        self.endpoints.append(endpoint)
        return {"id": "s1", "ok": True}

    @property
    def sent_text(self) -> str:
        for _url, body in self.calls:
            if "parts" in body:
                return body["parts"][0]["text"]
        return ""


def _dispatch(tmp_path, monkeypatch, payload, model="openrouter/x", allow=("openrouter/*",)):
    _clean_posture(tmp_path, monkeypatch)
    pol_file = tmp_path / "policy.json"
    pol_file.write_text(
        json.dumps(
            {
                "destinations": {"allow": list(allow), "server": "http://127.0.0.1:4096"},
                "audit": {"path": str(tmp_path / "audit.jsonl")},
            }
        ),
        encoding="utf-8",
    )
    recorder = _Recorder()
    monkeypatch.setattr(eg, "_post_json", recorder)
    monkeypatch.setattr(sys, "stdin", _FakeStdin(payload))
    args = eg.build_parser().parse_args(
        [
            "--policy", str(pol_file), "--workspace", str(tmp_path),
            "dispatch", "--stdin", "--model", model, "--timeout", "5",
        ]
    )
    return eg.cmd_dispatch(args), recorder


def _audit_lines(tmp_path) -> list[dict]:
    path = tmp_path / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_dispatch_to_a_disallowed_destination_posts_nothing(tmp_path, monkeypatch):
    code, recorder = _dispatch(tmp_path, monkeypatch, "rename the timer field", allow=["anthropic/*"])
    assert code == 2
    assert recorder.calls == []
    assert _audit_lines(tmp_path)[-1]["verdict"] == "blocked"


def test_dispatch_posts_the_redacted_text_not_the_payload(tmp_path, monkeypatch):
    code, recorder = _dispatch(tmp_path, monkeypatch, "ping ops@example.org about it")
    assert code == 0
    assert recorder.sent_text
    assert "ops@example.org" not in recorder.sent_text
    assert "[redacted:email-address]" in recorder.sent_text


def test_dispatch_with_a_blocking_finding_returns_two_and_posts_nothing(tmp_path, monkeypatch):
    code, recorder = _dispatch(tmp_path, monkeypatch, "token ghp_" + "z" * 36)
    assert code == 2
    assert recorder.calls == []


def test_dispatch_names_the_finding_it_redacted_before_sending(tmp_path, monkeypatch, capsys):
    code, _recorder = _dispatch(tmp_path, monkeypatch, "ping ops@example.org about it")
    assert code == 0
    err = capsys.readouterr().err
    assert "email-address" in err
    assert "redacted before sending" in err


def test_dispatch_writes_the_audit_line_before_the_post(tmp_path, monkeypatch):
    """A payload on the wire with no record of it is the failure this prevents."""
    _clean_posture(tmp_path, monkeypatch)
    pol_file = tmp_path / "policy.json"
    pol_file.write_text(
        json.dumps(
            {
                "destinations": {"allow": ["openrouter/*"], "server": "http://127.0.0.1:4096"},
                "audit": {"path": str(tmp_path / "audit.jsonl")},
            }
        ),
        encoding="utf-8",
    )
    seen: list[int] = []

    def post(url, body, timeout, endpoint=None):
        seen.append(len(_audit_lines(tmp_path)))
        return {"id": "s1", "ok": True}

    monkeypatch.setattr(eg, "_post_json", post)
    monkeypatch.setattr(sys, "stdin", _FakeStdin("rename the timer field"))
    args = eg.build_parser().parse_args(
        [
            "--policy", str(pol_file), "--workspace", str(tmp_path),
            "dispatch", "--stdin", "--model", "openrouter/x", "--timeout", "5",
        ]
    )
    assert eg.cmd_dispatch(args) == 0
    assert seen and seen[0] >= 1, "the first POST happened before any audit line"
    assert _audit_lines(tmp_path)[0]["stage"] == "intent"


def test_dispatch_sends_nothing_when_the_audit_trail_cannot_be_written(tmp_path, monkeypatch):
    _clean_posture(tmp_path, monkeypatch)
    pol_file = tmp_path / "policy.json"
    pol_file.write_text(
        json.dumps(
            {
                "destinations": {"allow": ["openrouter/*"], "server": "http://127.0.0.1:4096"},
                "audit": {"path": str(tmp_path / "audit.jsonl")},
            }
        ),
        encoding="utf-8",
    )
    recorder = _Recorder()
    monkeypatch.setattr(eg, "_post_json", recorder)

    def explode(policy, record):
        raise OSError("no such directory")

    monkeypatch.setattr(eg, "audit", explode)
    monkeypatch.setattr(sys, "stdin", _FakeStdin("rename the timer field"))
    args = eg.build_parser().parse_args(
        [
            "--policy", str(pol_file), "--workspace", str(tmp_path),
            "dispatch", "--stdin", "--model", "openrouter/x", "--timeout", "5",
        ]
    )
    assert eg.cmd_dispatch(args) == 3
    assert recorder.calls == []


def test_dispatch_records_the_endpoint_and_the_policy_it_used(tmp_path, monkeypatch):
    code, _recorder = _dispatch(tmp_path, monkeypatch, "rename the timer field")
    assert code == 0
    record = _audit_lines(tmp_path)[0]
    assert record["server"] == "127.0.0.1:4096"
    assert record["policy_source"].endswith("policy.json")


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
