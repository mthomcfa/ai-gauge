"""Egress-guarded dispatcher for sub-tasks handed to an external coding agent.

Delegating a sub-task to OpenCode (or any other out-of-process agent) sends
whatever context you gave it to whichever model provider that agent happens to
be configured with. Nothing in the Claude Code plugins that do this delegation
inspects the payload first, and nothing pins the destination. This module is
the choke point that does both: one process every delegation goes through,
which refuses to dispatch when the payload carries credentials or marked
intellectual property, and refuses to dispatch to a provider that is not on an
allowlist.

Run from the repo root:

    python tools/egress_guard.py preflight --task "refactor the meter catalog"
    python tools/egress_guard.py dispatch  --task "..." --model openrouter/x
    git diff | python tools/egress_guard.py scan --stdin
    python tools/egress_guard.py posture

Exit codes: 0 allowed, 2 blocked by policy, 3 posture or configuration fault
(which includes an audit trail that cannot be written: nothing is sent).
Stdlib only, so it runs from a bare interpreter on Windows, macOS and Linux
without the app's dependencies.
"""
from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

BLOCK = "block"
REDACT = "redact"
WARN = "warn"
_ACTIONS = (BLOCK, REDACT, WARN)

# Every quantifier below is bounded (`{m,n}`) or possessive (`{m,}+`, Python
# 3.11+). That is not style: an unbounded quantifier over a class that the next
# term can also match backtracks, and `test_every_quantifier_is_bounded_or_possessive`
# fails the build if one is added. Round 1 bounded the *path* scanner and left
# `email-address` as `[A-Za-z0-9._%+\-]+@...`: `.`, `-`, `%` and `+` are word
# boundaries but are inside the class, so every one of them was a fresh start
# position that ran to the end of the token looking for an `@`. 400 000 bytes of
# `a.-` took 122.9 s, of `x.` 185.2 s, and a plausible `svc.0-svc.1-...` list
# 73.7 s - against the documented 10 s hook timeout, which is a payload the hook
# cannot answer for and therefore does not block.
#
# Ranked most-specific-first: sk-ant- and sk-or- must be tried before the
# generic sk- rule, or every Anthropic key is reported as an OpenAI one.
_DETECTORS: tuple[tuple[str, str, str], ...] = (
    ("private-key", BLOCK, r"-----BEGIN (?:RSA |EC |OPENSSH |PGP |DSA )?PRIVATE KEY-----"),
    ("anthropic-key", BLOCK, r"sk-ant-[A-Za-z0-9_\-]{16,}+"),
    ("openrouter-key", BLOCK, r"sk-or-v1-[A-Za-z0-9]{16,}+"),
    ("openai-key", BLOCK, r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}+"),
    ("aws-access-key", BLOCK, r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("github-token", BLOCK, r"\bgh[pousr]_[A-Za-z0-9]{36,1024}\b"),
    # Fine-grained PATs are the current default on github.com and match none of
    # the gh[pousr]_ shapes.
    ("github-fine-grained-pat", BLOCK, r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{20,}+"),
    ("google-api-key", BLOCK, r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ("slack-token", BLOCK, r"\bxox[baprs]-[0-9A-Za-z\-]{10,}+"),
    (
        "jwt",
        BLOCK,
        r"\beyJ[A-Za-z0-9_\-]{10,4096}+\.[A-Za-z0-9_\-]{10,4096}+\.[A-Za-z0-9_\-]{10,4096}+",
    ),
    (
        # The user and password runs exclude the delimiter that follows them, so
        # each is possessive without changing what matches; `[^\s/@]+:[^\s/@]+@`
        # was ambiguous in both runs and quadratic from a single `postgres://`.
        "connection-string",
        BLOCK,
        r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqps?|mssql)://"
        r"[^\s/@:]{1,256}+:[^\s/@]{1,256}+@",
    ),
    ("basic-auth-url", BLOCK, r"\bhttps?://[^\s/@:]{1,256}+:[^\s/@]{1,256}+@[^\s/]{1,256}+"),
    # A bearer header carries a live credential whatever its shape.
    (
        "bearer-header",
        BLOCK,
        r"(?i)\bauthorization[ \t]{0,16}:[ \t]{0,16}bearer[ \t]{1,16}[A-Za-z0-9._\-~+/=]{8,4096}+",
    ),
    # Entra ID client secrets are ~40 characters with a '~' a few characters in.
    # SECURITY.md names this one as a secret the app itself holds.
    (
        "azure-client-secret",
        BLOCK,
        r"(?<![A-Za-z0-9])[A-Za-z0-9._\-]{1,5}[A-Za-z0-9]~[A-Za-z0-9._\-~]{30,512}+",
    ),
    # This app's own stores: src/aigauge/config.py defines KEYRING_SERVICE
    # "ai-gauge" with the usernames below, and the per-provider session cookies
    # it keeps beside them.
    (
        "aigauge-keyring",
        BLOCK,
        r"(?i)\bai-gauge\b[^\n]{0,60}?"
        r"\b(?:github-pat|openrouter-mgmt-key|openrouter-key|azure-client-secret)\b",
    ),
    (
        "aigauge-session-cookie",
        BLOCK,
        r"(?i)\b(?:sessionkey|(?:__secure-)?next-auth\.session-token(?:\.[01])?|"
        r"opencode-session)\b[ \t]{0,16}[=:][ \t]{0,16}[\"']?[^\s\"';,]{16,1024}+",
    ),
    (
        "secret-assignment",
        REDACT,
        # Anchored on the *tail* of the name, not on a word boundary in front of
        # it: `\b` does not fire after `_`, which is why `DATABASE_PASSWORD=`,
        # `DB_PASSWORD:` and `MY_SECRET=` - the commonest shape in a .env file
        # or a CI diff - produced no finding at all. Every run is bounded, so a
        # long separator-free blob cannot make this quadratic.
        r"(?i)(?:password|passwd|secret|token|api[_-]?key|private[_-]?key|credential)"
        r"[A-Za-z0-9_]{0,64}\s{0,16}[:=]\s{0,16}[\"']?[^\s\"',;]{8,256}+",
    ),
    ("certificate", WARN, r"-----BEGIN CERTIFICATE-----"),
    (
        "classification-banner",
        WARN,
        r"(?i)\b(?:strictly confidential|company confidential|proprietary and confidential|"
        r"internal use only|not for distribution|restricted distribution)\b",
    ),
    (
        # A local part is at most 64 characters (RFC 5321), so bounding it loses
        # no address; the lookbehind puts the only start position at the start of
        # the run, which is what makes a 400 KB run of `a.-` one failed match
        # rather than 266 000 of them.
        "email-address",
        REDACT,
        r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]{1,64}+@"
        r"(?:[A-Za-z0-9\-]{1,63}+\.){1,8}[A-Za-z]{2,24}\b",
    ),
)

# Anything that looks like a packed credential but matches no named format.
# Hex runs sit at entropy 4.0 exactly, so the floor is above that to keep git
# SHAs and checksums out of the report. One maximal run per token: `\b...{32,}\b`
# had to walk back over the tail of every run that did not end on a word
# character, and a lookbehind plus a possessive run cannot.
_OPAQUE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9+/=_\-])[A-Za-z0-9+/=_\-]{32,}+")
_ENTROPY_FLOOR = 4.2

# Paths whose *mention* in a payload is itself the finding, because the agent
# is being pointed at them even if the contents were not pasted.
_DEFAULT_DENY_GLOBS: tuple[str, ...] = (
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.p12",
    "**/*.pfx",
    "**/*.keystore",
    "**/id_rsa",
    "**/id_ed25519",
    "**/.npmrc",
    "**/.pypirc",
    "**/credentials.json",
    "**/.aws/credentials",
    "**/.claude/.credentials.json",
    "**/secrets/**",
    # This repo's own stores, per SECURITY.md: the DPAPI cookie file, the
    # per-account browser profiles that hold live session cookies, the Secret
    # Service keyrings behind `keyring`, and the agents' own credential files.
    "**/.ssh/**",
    "**/keyrings/**",
    "**/*.keyring",
    "**/ai-gauge/profiles/**",
    "**/secrets.dat",
    "**/auth.json",
)

# Path-shaped runs are found by scanning for separators and expanding within a
# bounded window, not by a regex. The pattern this replaces,
# `[A-Za-z0-9_.\-/\\]*[/\\][A-Za-z0-9_.\-/\\]+`, backtracked from every start
# position of a separator-free run: measured 1.4 s at 16 000 characters, 22.2 s
# at 64 000, quadratic, and at the tool's own 400 000-byte default cap it does
# not finish. In the documented hook wiring - `"timeout": 10` - a hook that
# cannot answer in time returns no exit 2, so the tool call proceeds unscanned.
_PATH_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-/\\"
)
_PATH_WINDOW = 256  # characters either side of a separator; real paths are shorter

_DEFAULT_POLICY: dict[str, Any] = {
    "destinations": {
        # Empty means "refuse everything": a policy has to name where its
        # payloads may go. Globs match OpenCode's provider/model identifiers.
        "allow": [],
        "server": "http://127.0.0.1:4096",
    },
    "rules": {},  # rule name -> "block" | "redact" | "warn" | "off"
    "paths": {"deny": list(_DEFAULT_DENY_GLOBS), "workspace_roots": []},
    "limits": {"max_payload_bytes": 400_000, "max_findings_listed": 40},
    "posture": {
        "forbid_permission_allow": ["bash", "webfetch", "external_directory"],
        "require_server_password": True,
    },
    "audit": {"path": ""},
}


class Fault(SystemExit):
    """A configuration or posture fault: exit 3, one line, no traceback.

    It subclasses `SystemExit` so that anything already written to expect a
    `SystemExit` from the policy layer keeps working, and so that `main` can
    turn it into the documented exit code instead of a stack trace.
    """

    def __init__(self, message: str) -> None:
        super().__init__(3)
        self.message = message


@dataclass(frozen=True)
class Finding:
    rule: str
    action: str
    start: int
    end: int
    excerpt: str  # already masked - never carries the matched secret

    def as_record(self) -> dict[str, Any]:
        return {"rule": self.rule, "action": self.action, "at": self.start}


@dataclass
class Policy:
    data: dict[str, Any] = field(default_factory=lambda: json.loads(json.dumps(_DEFAULT_POLICY)))
    source: str = "built-in defaults"

    @classmethod
    def load(cls, explicit: str | None, workspace: Path) -> Policy:
        for candidate, label in _policy_candidates(explicit, workspace):
            if candidate is None or not candidate.is_file():
                continue
            try:
                overlay = json.loads(candidate.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
                raise Fault(f"policy: cannot read {candidate}: {exc}") from exc
            if not isinstance(overlay, dict):
                raise Fault(f"policy: {candidate} is not a JSON object")
            merged = json.loads(json.dumps(_DEFAULT_POLICY))
            _deep_update(merged, overlay)
            return cls(merged, label)
        if explicit:
            raise Fault(f"policy file not found: {explicit}")
        return cls()

    def action_for(self, rule: str, default: str) -> str:
        rules = self.data.get("rules") or {}
        if not isinstance(rules, dict):
            raise Fault("policy: rules must be an object of rule -> action")
        override = rules.get(rule)
        if override is None:
            return default
        if override == "off":
            return "off"
        if override not in _ACTIONS:
            raise Fault(f"policy: rule {rule!r} has unknown action {override!r}")
        return override

    @property
    def allowed_destinations(self) -> list[str]:
        raw = self.data.get("destinations", {}).get("allow", [])
        if not isinstance(raw, list):
            raise Fault("policy: destinations.allow must be a list of globs")
        return [str(item) for item in raw]

    @property
    def server(self) -> str:
        return str(self.data.get("destinations", {}).get("server") or "").rstrip("/")

    @property
    def deny_globs(self) -> list[str]:
        raw = self.data.get("paths", {}).get("deny", [])
        if not isinstance(raw, list):
            raise Fault("policy: paths.deny must be a list of globs")
        return [str(item) for item in raw]

    @property
    def workspace_roots(self) -> list[str]:
        raw = self.data.get("paths", {}).get("workspace_roots", [])
        if not isinstance(raw, list):
            raise Fault("policy: paths.workspace_roots must be a list of paths")
        return [str(item) for item in raw]

    @property
    def max_payload_bytes(self) -> int:
        raw = self.data.get("limits", {}).get("max_payload_bytes", 0)
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise Fault(f"policy: limits.max_payload_bytes is not a number: {raw!r}") from exc

    @property
    def audit_path(self) -> Path:
        configured = self.data.get("audit", {}).get("path")
        if configured:
            return Path(os.path.expanduser(str(configured)))
        return _default_audit_path()


def _policy_candidates(explicit: str | None, workspace: Path) -> Iterable[tuple[Path | None, str]]:
    if explicit:
        yield Path(explicit), explicit
        return
    env = os.environ.get("AIGAUGE_EGRESS_POLICY")
    if env:
        yield Path(env), f"$AIGAUGE_EGRESS_POLICY ({env})"
    yield workspace / ".egress-policy.json", str(workspace / ".egress-policy.json")


def _deep_update(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value


def _default_audit_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "ai-gauge" / "egress-audit.jsonl"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ai-gauge" / "egress-audit.jsonl"
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "ai-gauge" / "egress-audit.jsonl"


def shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    counts = Counter(text)
    n = len(text)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


_RUN_SALT = secrets.token_bytes(16)  # per process, held in memory, never written


def _length_bucket(size: int) -> str:
    for edge in (8, 16, 32, 64, 128, 256):
        if size <= edge:
            return f"<={edge} chars"
    return ">256 chars"


def _mask(matched: str) -> str:
    """A stable, non-reversible handle for a match, so reports carry no secret.

    Salted per run. An unsalted `sha256(value)[:8]` is a dictionary-verifiable
    oracle for a low-entropy value - an email address, a person's name, a short
    password - and the exact character count narrows it further. The handle is
    stable within one run, which is all a report needs, and meaningless outside
    it.
    """
    digest = hashlib.sha256(_RUN_SALT + matched.encode("utf-8")).hexdigest()[:8]
    return f"<{_length_bucket(len(matched))}, run-salted:{digest}>"


def scan(text: str, policy: Policy) -> list[Finding]:
    findings: list[Finding] = []
    claimed: list[tuple[int, int]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(start < c_end and c_start < end for c_start, c_end in claimed)

    for rule, default_action, pattern in _DETECTORS:
        action = policy.action_for(rule, default_action)
        if action == "off":
            continue
        for match in re.finditer(pattern, text):
            # The narrower, earlier rule wins: a Postgres URL is one finding,
            # not also a basic-auth URL and a secret assignment.
            if overlaps(match.start(), match.end()):
                continue
            claimed.append((match.start(), match.end()))
            findings.append(
                Finding(rule, action, match.start(), match.end(), _mask(match.group(0)))
            )

    # The catch-all for credentials with no recognised shape defaults to
    # redact, not warn: a high-entropy blob the operator cannot name is the
    # case where taking it out of the payload is most obviously right, and
    # warn used to mean "send it and say nothing".
    entropy_action = policy.action_for("opaque-token", REDACT)
    if entropy_action != "off":
        for match in _OPAQUE_TOKEN_RE.finditer(text):
            token = match.group(0)
            if overlaps(match.start(), match.end()):
                continue
            if shannon_entropy(token) < _ENTROPY_FLOOR:
                continue
            claimed.append((match.start(), match.end()))
            findings.append(
                Finding("opaque-token", entropy_action, match.start(), match.end(), _mask(token))
            )

    # Path findings go through the same overlap filter as everything else.
    # They did not, and `redact()` assumes non-overlapping spans, so a denied
    # path overlapping an email address chewed the placeholder of whichever
    # was written second.
    for finding in _scan_paths(text, policy):
        if overlaps(finding.start, finding.end):
            continue
        claimed.append((finding.start, finding.end))
        findings.append(finding)

    findings.sort(key=lambda f: f.start)
    return findings


def truncate(payload: str, policy: Policy) -> tuple[str, bool]:
    """Cut the payload to `limits.max_payload_bytes` before anything scans it.

    The cap was computed and then ignored: `_decide` recorded "over
    max_payload_bytes" as a problem and scanned the whole payload anyway, and
    `scan`/`hook` applied no cap at all. A cap that does not gate the scan
    bounds nothing.
    """
    cap = policy.max_payload_bytes
    if cap <= 0:
        return payload, False
    encoded = payload.encode("utf-8")
    if len(encoded) <= cap:
        return payload, False
    return encoded[:cap].decode("utf-8", "ignore"), True


def _path_like_spans(text: str) -> Iterable[tuple[int, int]]:
    """Spans that look like a path: one pass, plus a bounded expansion.

    Each separator is found by a plain scan and grown outwards over path
    characters up to `_PATH_WINDOW` either side; separators already inside a
    span are skipped, so no character is visited more than a constant number of
    times whatever the input looks like.
    """
    length = len(text)
    index = 0
    while index < length:
        char = text[index]
        if char != "/" and char != "\\":
            index += 1
            continue
        start = index
        floor = max(0, index - _PATH_WINDOW)
        while start > floor and text[start - 1] in _PATH_CHARS:
            start -= 1
        end = index + 1
        ceiling = min(length, index + 1 + _PATH_WINDOW)
        while end < ceiling and text[end] in _PATH_CHARS:
            end += 1
        if end > index + 1:  # a separator with nothing after it is not a path
            yield start, end
        index = max(end, index + 1)


def _scan_paths(text: str, policy: Policy) -> list[Finding]:
    globs = policy.deny_globs
    action = policy.action_for("denied-path", BLOCK)
    if not globs or action == "off":
        return []
    found: list[Finding] = []
    seen: set[str] = set()
    for start, end in _path_like_spans(text):
        raw = text[start:end].replace("\\", "/")
        # git diff headers name the same file twice, as a/path and b/path.
        normalised = raw.removeprefix("a/").removeprefix("b/")
        if normalised in seen:
            continue
        for glob in globs:
            if _glob_match(normalised, glob):
                seen.add(normalised)
                found.append(
                    Finding("denied-path", action, start, end, _path_excerpt(normalised))
                )
                break
    return found


def _glob_match(path: str, glob: str) -> bool:
    # Both sides lowered: fnmatch is case-sensitive on POSIX and insensitive on
    # Windows, so `C:\Users\m\.AWS\CREDENTIALS` was denied on one platform and
    # passed on the other. A deny list that depends on the case a path was typed
    # in is not a deny list.
    lowered = path.lower()
    pattern = glob.lower()
    if fnmatch.fnmatchcase(lowered, pattern):
        return True
    # fnmatch has no ** semantics, so "**/x" also has to match a bare "x".
    if pattern.startswith("**/") and fnmatch.fnmatchcase(lowered, pattern[3:]):
        return True
    return False


def _path_excerpt(path: str) -> str:
    """The basename only. `Finding.excerpt` promises it never carries the
    matched secret, and a full path carries the local username."""
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return f".../{name}" if name else "..."


def redact(text: str, findings: Iterable[Finding]) -> tuple[str, int]:
    """Replace redact-action matches with a fixed placeholder, right to left.

    The placeholder names the detector and nothing else. It used to carry
    `sha256(value)[:8]`, unsalted, in the text that was *dispatched* - handing
    the third party the redaction exists to keep the value from a
    dictionary-verifiable oracle for it. Any hash stays in the local report,
    where `_mask` salts it per run.
    """
    targets = sorted(
        (f for f in findings if f.action == REDACT), key=lambda f: f.start, reverse=True
    )
    out = text
    for finding in targets:
        out = f"{out[: finding.start]}[redacted:{finding.rule}]{out[finding.end :]}"
    return out, len(targets)


def destination_allowed(model: str, policy: Policy) -> bool:
    # fnmatch normcases both sides, so the same allowlist matched differently on
    # Windows and on POSIX. fnmatchcase over lowered strings is one answer
    # everywhere. An empty or multi-line model is refused outright: the second
    # would let one allowed id carry another one behind a newline.
    if not model or "\n" in model or "\r" in model:
        return False
    allowed = policy.allowed_destinations
    if not allowed:
        return False
    target = model.strip().lower()
    return any(fnmatch.fnmatchcase(target, pattern.strip().lower()) for pattern in allowed)


def _server_auth_headers() -> dict[str, str]:
    """Basic auth from `OPENCODE_SERVER_PASSWORD`, which posture already demands.

    Without this the guard required a hardening step that made the guard itself
    unusable - measured against a server that enforces it: `posture ok`, then
    `dispatch failed: HTTP Error 401`. The practical resolution was to set the
    variable and leave the server unauthenticated, which turns the check into a
    ritual.
    """
    password = os.environ.get("OPENCODE_SERVER_PASSWORD")
    if not password:
        return {}
    user = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _post_json(url: str, body: dict[str, Any], timeout: float) -> Any:
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", **_server_auth_headers()},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback only
        return json.loads(response.read().decode("utf-8"))


def _split_server(server: str) -> urllib.parse.SplitResult | None:
    """Parse `destinations.server`, or None when it is not a usable http URL.

    A prefix test is not a host check. `http://127.0.0.1.evil.example` begins
    with a loopback literal and resolves wherever its owner points it, and
    `http://127.0.0.1:4096@evil.example` puts the loopback literal in the
    userinfo, where it names a *credential* and not the destination at all.
    """
    try:
        parts = urllib.parse.urlsplit(server)
        if parts.username or parts.password or "@" in parts.netloc:
            return None
        if not parts.hostname:
            return None
        parts.port  # noqa: B018 - raises ValueError on a non-numeric port
    except ValueError:
        return None
    return parts


def _as_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The host as an IP address, or None when it is a name rather than one."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    # inet_aton shorthand: 127.1 and 2130706433 are both 127.0.0.1, and both
    # reach the loopback interface. Only the decimal forms are expanded;
    # anything else stays unrecognised, which callers read as "not loopback".
    fields = host.split(".")
    if not 1 <= len(fields) <= 4:
        return None
    if not all(f.isascii() and f.isdigit() for f in fields):
        return None
    parts = [int(f) for f in fields]
    if any(p > 255 for p in parts[:-1]):
        return None
    tail_width = 4 - (len(parts) - 1)
    if parts[-1] >= 256**tail_width:
        return None
    value = 0
    for part in parts[:-1]:
        value = (value << 8) | part
    return ipaddress.IPv4Address((value << (8 * tail_width)) | parts[-1])


def server_endpoint(server: str) -> str:
    """`host:port` of `destinations.server` - where the bytes actually go.

    This, not the `--model` label, is the thing an audit trail has to record:
    the label says which model was asked for, the endpoint says who received
    the payload.
    """
    parts = _split_server(server)
    if parts is None:
        return f"unparseable:{server}" if server else "unset"
    port = parts.port
    return f"{parts.hostname}:{port}" if port else str(parts.hostname)


def _is_loopback(server: str) -> bool:
    parts = _split_server(server)
    if parts is None or parts.scheme != "http":
        return False
    host = (parts.hostname or "").strip("[]")
    if host == "localhost":  # urlsplit has already lowercased it
        return True
    address = _as_ip(host)
    return bool(address and address.is_loopback)


def opencode_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "opencode" / "opencode.json"


def posture(policy: Policy, workspace: Path) -> list[str]:
    """Local conditions that must hold before any payload is allowed to leave."""
    problems: list[str] = []

    server = policy.server
    if not server:
        problems.append("policy names no destinations.server")
    elif not _is_loopback(server):
        problems.append(
            f"destinations.server is not loopback: {server} "
            f"(resolves to endpoint {server_endpoint(server)})"
        )

    config_path = opencode_config_path()
    forbidden = policy.data.get("posture", {}).get("forbid_permission_allow", [])
    if config_path.is_file() and forbidden:
        try:
            parsed = json.loads(config_path.read_text(encoding="utf-8"))
            permissions = parsed.get("permission", {}) if isinstance(parsed, dict) else {}
            if not isinstance(permissions, dict):
                permissions = {}
        except Exception as exc:  # an unreadable config is a fault, not a crash
            problems.append(f"cannot read {config_path}: {exc}")
        else:
            drifted = [key for key in forbidden if permissions.get(key) == "allow"]
            if drifted:
                problems.append(
                    f"{config_path} grants unprompted {', '.join(sorted(drifted))} "
                    "- the delegated agent can act without asking"
                )

    if policy.data.get("posture", {}).get("require_server_password", True):
        if not os.environ.get("OPENCODE_SERVER_PASSWORD"):
            problems.append(
                "OPENCODE_SERVER_PASSWORD is unset - any local process can drive the agent server"
            )

    roots = policy.workspace_roots
    if roots:
        resolved = workspace.resolve()
        if not any(_within(resolved, Path(os.path.expanduser(r)).resolve()) for r in roots):
            problems.append(f"workspace {resolved} is outside paths.workspace_roots")

    return problems


def _within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


_BASE_REF_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._/\-^~]{0,200}\Z")


def build_payload(args: argparse.Namespace, workspace: Path) -> str:
    if args.stdin:
        return sys.stdin.read()
    parts: list[str] = []
    if args.task:
        parts.append(args.task)
    if args.include_diff:
        parts.append(_git(workspace, "status", "--short", "--untracked-files=all"))
        diff = ["diff", "--no-ext-diff", "--no-textconv"]
        if args.base:
            diff += ["--end-of-options", f"{_verified_base(workspace, args.base)}...HEAD"]
        else:
            diff.append("--end-of-options")
        parts.append(_git(workspace, *diff, "--"))
    return "\n\n".join(p for p in parts if p)


def _verified_base(workspace: Path, base: str) -> str:
    """Refuse a `--base` that is not a plain ref, then ask git whether it is one.

    argparse rejects a bare leading dash but binds the `=` form, so
    `--base=--output=PATH` reached `git diff` as an option and wrote an
    arbitrary file. A ref name is a narrow shape; anything else is refused
    before git sees it, and what is left is checked against the repository.
    """
    if base.startswith("-") or not _BASE_REF_RE.match(base):
        raise Fault(f"--base {base!r} is not a plain ref name")
    _git(workspace, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{base}^{{commit}}")
    return base


def _git(workspace: Path, *args: str) -> str:
    """Run git with the workspace's own diff drivers disabled.

    `diff.external` and `diff.textconv` are commands git runs on the operator's
    behalf. A workspace the delegated agent can write - which is the whole
    threat this tool exists for, per finding A1's `edit: allow` - is a
    workspace where one can be planted, and `--include-diff` alone was then
    enough to run it. A failed git call is a fault rather than an empty diff:
    a user who believes they preflighted a diff must not have preflighted a
    nine-byte status line.
    """
    git = shutil.which("git")
    if git is None:
        raise Fault("git was not found on PATH, so --include-diff cannot be honoured")
    result = subprocess.run(
        [git, "-c", "diff.external=", "-c", "diff.textconv=", "-c", "core.pager=cat",
         "--no-pager", *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        raise Fault(
            f"git {args[0]} failed in {workspace} "
            f"(exit {result.returncode}): {detail[-1] if detail else 'no output'}"
        )
    return result.stdout


def audit(policy: Policy, record: dict[str, Any]) -> None:
    path = policy.audit_path
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    line = json.dumps(record, sort_keys=True) + "\n"
    # Create restricted before the first write; an existing file keeps its mode
    # (os.open only applies the mode on create). O_NOFOLLOW where the platform
    # has it, so a symlink planted at the audit path cannot redirect the trail.
    flags = os.O_CREAT | os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    handle = os.open(path, flags, 0o600)
    with os.fdopen(handle, "a", encoding="utf-8") as fh:
        fh.write(line)


def _report(findings: list[Finding], policy: Policy, stream=sys.stderr) -> None:
    limit = int(policy.data.get("limits", {}).get("max_findings_listed", 40))
    by_rule = Counter(f.rule for f in findings)
    for rule, count in by_rule.most_common():
        action = next(f.action for f in findings if f.rule == rule)
        print(f"  {action:<7} {rule} x{count}", file=stream)
    # Every finding is listed, whatever its action. `warn` used to print
    # nothing on the dispatch path, which made it indistinguishable from
    # "nothing was found" - three credential findings were recorded in the
    # audit and shown to nobody.
    for finding in findings[:limit]:
        print(
            f"    at {finding.start}: {finding.action} {finding.rule} {finding.excerpt}",
            file=stream,
        )


@dataclass
class Decision:
    """What the guard decided, and which documented exit code says so.

    `problems` and `refusals` are kept apart because they mean different things
    to a caller: a posture or configuration fault is "the guard could not do
    its job" (exit 3), a refusal is "the guard did its job and said no"
    (exit 2). A wrapper keyed on `-eq 2` must not read the first as the second.
    """

    verdict: str  # "allowed" | "blocked" | "fault"
    findings: list[Finding]
    problems: list[str]  # posture / configuration -> exit 3
    refusals: list[str]  # policy said no -> exit 2
    sent: str  # the redacted text, i.e. what would actually be dispatched

    @property
    def exit_code(self) -> int:
        return {"allowed": 0, "blocked": 2, "fault": 3}[self.verdict]

    @property
    def faults(self) -> list[str]:
        """Everything worth printing, faults first."""
        return [*self.problems, *self.refusals]


def _decide(payload: str, policy: Policy, workspace: Path, model: str | None) -> Decision:
    problems = posture(policy, workspace)
    refusals: list[str] = []

    size = len(payload.encode("utf-8"))
    scanned, was_truncated = truncate(payload, policy)
    if was_truncated:
        refusals.append(
            f"payload is {size} bytes, over limits.max_payload_bytes "
            f"({policy.max_payload_bytes}); only the first {policy.max_payload_bytes} "
            "bytes were scanned"
        )

    if model is None:
        problems.append("no --model given, so no destination could be checked")
    elif not destination_allowed(model, policy):
        refusals.append(
            f"destination {model!r} is not in destinations.allow "
            f"({policy.allowed_destinations or 'empty - nothing is allowed'})"
        )

    findings = scan(scanned, policy)
    redacted, _ = redact(scanned, findings)

    blocked = [f for f in findings if f.action == BLOCK]
    if problems:
        return Decision("fault", findings, problems, refusals, redacted)
    if blocked or refusals:
        return Decision("blocked", findings, problems, refusals, redacted)
    return Decision("allowed", findings, problems, refusals, redacted)


def cmd_scan(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace or os.getcwd())
    policy = Policy.load(args.policy, workspace)
    payload = build_payload(args, workspace)
    size = len(payload.encode("utf-8"))
    scanned, was_truncated = truncate(payload, policy)
    findings = scan(scanned, policy)
    if args.json:
        print(json.dumps([f.as_record() for f in findings], indent=2))
    else:
        print(f"policy: {policy.source}")
        note = (
            f" (truncated to the first {policy.max_payload_bytes} bytes before scanning)"
            if was_truncated
            else ""
        )
        print(f"payload: {size} bytes{note}, {len(findings)} finding(s)")
        _report(findings, policy, stream=sys.stdout)
    if was_truncated:
        print(
            f"note: only the first {policy.max_payload_bytes} of {size} bytes were scanned",
            file=sys.stderr,
        )
    return 2 if any(f.action == BLOCK for f in findings) else 0


def cmd_posture(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace or os.getcwd())
    policy = Policy.load(args.policy, workspace)
    problems = posture(policy, workspace)
    if not problems:
        print("posture ok")
        return 0
    for problem in problems:
        print(f"  fault  {problem}", file=sys.stderr)
    return 3


def _destination_refusal(policy: Policy, model: str | None) -> str | None:
    """The destination decision, made before the payload is gathered.

    `--include-diff` shells out to git, and running git in a workspace the
    delegated agent can write is not a free action. The doc claims a refused
    destination is refused "before the payload is read"; it now is.
    """
    if model is None or destination_allowed(model, policy):
        return None
    return (
        f"destination {model!r} is not in destinations.allow "
        f"({policy.allowed_destinations or 'empty - nothing is allowed'})"
    )


def _refuse_before_payload(
    command: str, args: argparse.Namespace, policy: Policy, workspace: Path, refusal: str
) -> int:
    record = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "command": command,
        "workspace": str(workspace),
        "policy_source": policy.source,
        "model": args.model,
        "server": server_endpoint(policy.server),
        "payload_sha256": None,
        "payload_bytes": None,
        "verdict": "blocked",
        "findings": [],
        "posture": [],
        "refusals": [refusal],
        "note": "the destination was refused before the payload was gathered",
    }
    print(f"policy: {policy.source}", file=sys.stderr)
    print("verdict: blocked - nothing was gathered and nothing was sent", file=sys.stderr)
    print(f"  fault  {refusal}", file=sys.stderr)
    try:
        audit(policy, record)
    except OSError as exc:
        print(f"fault: the audit trail could not be written ({exc})", file=sys.stderr)
        return 3
    return 2


def cmd_preflight(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace or os.getcwd())
    policy = Policy.load(args.policy, workspace)
    refusal = _destination_refusal(policy, args.model)
    if refusal is not None:
        return _refuse_before_payload("preflight", args, policy, workspace, refusal)
    payload = build_payload(args, workspace)
    decision = _decide(payload, policy, workspace, args.model)

    record = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "command": "preflight",
        "workspace": str(workspace),
        "model": args.model,
        "server": server_endpoint(policy.server),
        "payload_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "payload_bytes": len(payload.encode("utf-8")),
        "verdict": decision.verdict,
        "findings": [f.as_record() for f in decision.findings],
        "posture": decision.problems,
        "refusals": decision.refusals,
    }

    print(f"policy: {policy.source}")
    print(f"verdict: {decision.verdict}")
    for problem in decision.faults:
        print(f"  fault  {problem}", file=sys.stderr)
    _report(decision.findings, policy, stream=sys.stderr)
    try:
        audit(policy, record)
    except OSError as exc:
        print(f"fault: the audit trail could not be written ({exc})", file=sys.stderr)
        return 3
    return decision.exit_code


def cmd_dispatch(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace or os.getcwd())
    policy = Policy.load(args.policy, workspace)
    refusal = _destination_refusal(policy, args.model)
    if refusal is not None:
        return _refuse_before_payload("dispatch", args, policy, workspace, refusal)
    payload = build_payload(args, workspace)
    decision = _decide(payload, policy, workspace, args.model)

    started = time.time()
    record: dict[str, Any] = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "command": "dispatch",
        "workspace": str(workspace),
        "policy_source": policy.source,
        "model": args.model,
        "server": server_endpoint(policy.server),
        "payload_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "payload_bytes": len(payload.encode("utf-8")),
        "verdict": decision.verdict,
        "findings": [f.as_record() for f in decision.findings],
        "posture": decision.problems,
        "refusals": decision.refusals,
    }

    print(f"policy: {policy.source}", file=sys.stderr)
    if decision.verdict != "allowed":
        print("verdict: blocked - nothing was sent", file=sys.stderr)
        for problem in decision.faults:
            print(f"  fault  {problem}", file=sys.stderr)
        _report(decision.findings, policy, stream=sys.stderr)
        # An unwritable audit path is a configuration fault in both directions:
        # the refusal still stands, but the operator is told the code means
        # "the guard could not finish its job", not "policy said no".
        try:
            audit(policy, record)
        except OSError as exc:
            print(f"fault: the audit trail could not be written ({exc})", file=sys.stderr)
            return 3
        return decision.exit_code

    sent = decision.sent
    record["sent_sha256"] = hashlib.sha256(sent.encode("utf-8")).hexdigest()
    record["sent_bytes"] = len(sent.encode("utf-8"))

    # The allowed branch reports too, and before the POST: an allowed dispatch
    # that found and redacted a credential has to say so, or `warn` and
    # `redact` are silent exactly where it matters.
    redacted_count = sum(1 for f in decision.findings if f.action == REDACT)
    print(
        f"verdict: allowed - {len(decision.findings)} finding(s), "
        f"{redacted_count} redacted before sending",
        file=sys.stderr,
    )
    _report(decision.findings, policy, stream=sys.stderr)

    # The audit line goes down BEFORE the POST. A dispatch that is killed, or
    # whose audit write fails, must not be a payload on the wire with no record
    # of it; if the trail cannot be written, nothing is sent.
    try:
        audit(policy, dict(record, stage="intent"))
    except OSError as exc:
        print(f"blocked: the audit trail could not be written ({exc})", file=sys.stderr)
        print("nothing was sent", file=sys.stderr)
        return 3

    try:
        session = _post_json(f"{policy.server}/session", {"title": args.title}, args.timeout)
        body: dict[str, Any] = {"parts": [{"type": "text", "text": sent}]}
        if args.model:
            body["model"] = args.model
        if args.agent:
            body["agent"] = args.agent
        response = _post_json(
            f"{policy.server}/session/{session['id']}/message", body, args.timeout
        )
    except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError) as exc:
        record["verdict"] = "error"
        record["error"] = str(exc)
        record["elapsed_s"] = round(time.time() - started, 3)
        _audit_best_effort(policy, dict(record, stage="result"))
        print(f"dispatch failed: {exc}", file=sys.stderr)
        return 3

    record["elapsed_s"] = round(time.time() - started, 3)
    _audit_best_effort(policy, dict(record, stage="result"))
    print(json.dumps(response, indent=2))
    return 0


def _audit_best_effort(policy: Policy, record: dict[str, Any]) -> None:
    """Record the outcome. The payload is already on the wire by this point, so
    a failure here is reported rather than turned into a refusal."""
    try:
        audit(policy, record)
    except OSError as exc:
        print(f"warning: outcome not recorded in the audit trail ({exc})", file=sys.stderr)


def cmd_hook(args: argparse.Namespace) -> int:
    """Claude Code PreToolUse hook: keep delegation on the guarded path.

    A guard only guards what goes through it, so this refuses Bash calls that
    invoke a delegating agent directly, and scans prompts handed to sub-agents.

    Every error path blocks. A hook that crashes, or that answers 0 because it
    could not parse its own input, is a hook that let the call through: empty
    stdin, a JSON array, a truncated event and a malformed workspace policy all
    used to allow, and one of them raised an AttributeError traceback.
    """
    try:
        return _hook(args)
    except (Exception, SystemExit) as exc:
        print(
            f"Blocked: the egress guard could not evaluate this call ({exc}).",
            file=sys.stderr,
        )
        return 2


def _hook(args: argparse.Namespace) -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        raise ValueError("no hook event on stdin")
    event = json.loads(raw)
    if not isinstance(event, dict):
        raise ValueError("the hook event is not a JSON object")

    workspace = Path(event.get("cwd") or os.getcwd())
    policy = Policy.load(args.policy, workspace)
    tool = event.get("tool_name", "")
    tool_input = event.get("tool_input", {})
    if tool_input is None or not isinstance(tool_input, dict):
        raise ValueError(f"tool_input is {type(tool_input).__name__}, not an object")

    # Keyed on the presence of a command rather than on tool_name: an event that
    # does not name its tool still must not smuggle a direct invocation past.
    if tool == "Bash" or "command" in tool_input:
        command = str(tool_input.get("command", ""))
        if _bypasses_guard(command):
            print(
                "Blocked: dispatch external agents through tools/egress_guard.py, "
                "which scans the payload and pins the destination.",
                file=sys.stderr,
            )
            return 2
        return 0

    prompt = str(tool_input.get("prompt") or tool_input.get("description") or "")
    if not prompt:
        return 0
    # Bounded before the scan, so a prompt padded past the hook's configured
    # timeout cannot make the guard too slow to answer.
    scanned, was_truncated = truncate(prompt, policy)
    findings = scan(scanned, policy)
    blocked = [f for f in findings if f.action == BLOCK]
    if blocked:
        rules = ", ".join(sorted({f.rule for f in blocked}))
        print(f"Blocked: sub-task prompt carries {rules}.", file=sys.stderr)
        return 2
    if was_truncated:
        print(
            f"Blocked: sub-task prompt is over limits.max_payload_bytes "
            f"({policy.max_payload_bytes}), so it could not be scanned in full.",
            file=sys.stderr,
        )
        return 2
    return 0


# Over-blocking is the acceptable direction here: a refused Bash call costs a
# retry through the guard, an unrecognised one costs the guard's whole purpose.
# The old pattern required `run`/`serve` to follow `opencode` immediately, so
# `opencode --print-logs run`, `npx opencode-ai run`, `$(which opencode) run`
# and a bare `opencode server` all walked through.
_BYPASS_RE = re.compile(
    r"""
      (?:opencode|codex)-companion\.mjs             # the plugins' own launchers
    | \bopencode(?:-ai)?\b[^\n]{0,200}?\b(?:run|serve|server)\b   # any flags in between
    | (?:^|[;|&(`\n]|\$\()[ \t]{0,16}(?:[\w.\-/\\]{0,200}[/\\])?opencode(?:-ai)?(?:@[\w.\-]{1,40})?\b
    | \b(?:npx|bunx|pnpx|dlx|exec|sudo|command)[ \t]{1,16}
      (?:-\S{1,40}[ \t]{1,16}){0,8}opencode(?:-ai)?(?:@[\w.\-]{1,40})?\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _bypasses_guard(command: str) -> bool:
    return bool(_BYPASS_RE.search(command))


def _add_payload_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task", help="natural-language task text")
    parser.add_argument("--stdin", action="store_true", help="read the payload from stdin")
    parser.add_argument("--include-diff", action="store_true", help="append git status and diff")
    parser.add_argument("--base", help="base ref for the diff, e.g. origin/main")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="egress_guard", description=__doc__.splitlines()[0])
    parser.add_argument("--policy", help="path to a policy JSON file")
    parser.add_argument("--workspace", help="workspace root (default: cwd)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="report findings without dispatching")
    _add_payload_args(p_scan)
    p_scan.add_argument("--json", action="store_true")
    p_scan.set_defaults(func=cmd_scan)

    p_posture = sub.add_parser("posture", help="check local conditions only")
    p_posture.set_defaults(func=cmd_posture)

    p_pre = sub.add_parser("preflight", help="decide, audit, but do not dispatch")
    _add_payload_args(p_pre)
    p_pre.add_argument("--model", help="provider/model the payload would go to")
    p_pre.set_defaults(func=cmd_preflight)

    p_dispatch = sub.add_parser("dispatch", help="send the payload if policy allows")
    _add_payload_args(p_dispatch)
    p_dispatch.add_argument("--model", required=True, help="provider/model to dispatch to")
    p_dispatch.add_argument("--agent", help="agent name understood by the server")
    p_dispatch.add_argument("--title", default="guarded sub-task")
    p_dispatch.add_argument("--timeout", type=float, default=900.0)
    p_dispatch.set_defaults(func=cmd_dispatch)

    p_hook = sub.add_parser("hook", help="PreToolUse hook mode, reads the event on stdin")
    p_hook.set_defaults(func=cmd_hook)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Fault as fault:
        print(f"fault: {fault.message}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # a fault is a one-line message, never a traceback
        print(f"fault: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
