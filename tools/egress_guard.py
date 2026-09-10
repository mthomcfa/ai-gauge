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

Exit codes: 0 allowed, 2 blocked by policy, 3 posture or configuration fault.
Stdlib only, so it runs from a bare interpreter on Windows, macOS and Linux
without the app's dependencies.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

BLOCK = "block"
REDACT = "redact"
WARN = "warn"
_ACTIONS = (BLOCK, REDACT, WARN)

# Ranked most-specific-first: sk-ant- and sk-or- must be tried before the
# generic sk- rule, or every Anthropic key is reported as an OpenAI one.
_DETECTORS: tuple[tuple[str, str, str], ...] = (
    ("private-key", BLOCK, r"-----BEGIN (?:RSA |EC |OPENSSH |PGP |DSA )?PRIVATE KEY-----"),
    ("anthropic-key", BLOCK, r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    ("openrouter-key", BLOCK, r"sk-or-v1-[A-Za-z0-9]{16,}"),
    ("openai-key", BLOCK, r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}"),
    ("aws-access-key", BLOCK, r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("github-token", BLOCK, r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    ("google-api-key", BLOCK, r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ("slack-token", BLOCK, r"\bxox[baprs]-[0-9A-Za-z\-]{10,}"),
    ("jwt", BLOCK, r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    (
        "connection-string",
        BLOCK,
        r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqps?|mssql)://[^\s/@]+:[^\s/@]+@",
    ),
    ("basic-auth-url", BLOCK, r"\bhttps?://[^\s/@:]+:[^\s/@]+@[^\s/]+"),
    (
        "secret-assignment",
        REDACT,
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|client[_-]?secret|"
        r"auth[_-]?token|bearer)\b\s*[:=]\s*[\"']?([^\s\"',;]{8,})",
    ),
    ("certificate", WARN, r"-----BEGIN CERTIFICATE-----"),
    (
        "classification-banner",
        WARN,
        r"(?i)\b(?:strictly confidential|company confidential|proprietary and confidential|"
        r"internal use only|not for distribution|restricted distribution)\b",
    ),
    ("email-address", REDACT, r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
)

# Anything that looks like a packed credential but matches no named format.
# Hex runs sit at entropy 4.0 exactly, so the floor is above that to keep git
# SHAs and checksums out of the report.
_OPAQUE_TOKEN_RE = re.compile(r"\b[A-Za-z0-9+/=_\-]{32,}\b")
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
)

_PATH_LIKE_RE = re.compile(r"[A-Za-z0-9_.\-/\\]*[/\\][A-Za-z0-9_.\-/\\]+")

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
            merged = json.loads(json.dumps(_DEFAULT_POLICY))
            _deep_update(merged, json.loads(candidate.read_text(encoding="utf-8")))
            return cls(merged, label)
        if explicit:
            raise SystemExit(f"policy file not found: {explicit}")
        return cls()

    def action_for(self, rule: str, default: str) -> str:
        override = self.data.get("rules", {}).get(rule)
        if override is None:
            return default
        if override == "off":
            return "off"
        if override not in _ACTIONS:
            raise SystemExit(f"policy: rule {rule!r} has unknown action {override!r}")
        return override

    @property
    def allowed_destinations(self) -> list[str]:
        return list(self.data.get("destinations", {}).get("allow", []))

    @property
    def server(self) -> str:
        return str(self.data.get("destinations", {}).get("server") or "").rstrip("/")

    @property
    def deny_globs(self) -> list[str]:
        return list(self.data.get("paths", {}).get("deny", []))

    @property
    def workspace_roots(self) -> list[str]:
        return list(self.data.get("paths", {}).get("workspace_roots", []))

    @property
    def max_payload_bytes(self) -> int:
        return int(self.data.get("limits", {}).get("max_payload_bytes", 0))

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


def _mask(matched: str) -> str:
    """A stable, non-reversible handle for a match, so reports carry no secret."""
    digest = hashlib.sha256(matched.encode("utf-8")).hexdigest()[:8]
    return f"<{len(matched)} chars, sha256:{digest}>"


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

    entropy_action = policy.action_for("opaque-token", WARN)
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

    findings.extend(_scan_paths(text, policy))
    findings.sort(key=lambda f: f.start)
    return findings


def _scan_paths(text: str, policy: Policy) -> list[Finding]:
    globs = policy.deny_globs
    action = policy.action_for("denied-path", BLOCK)
    if not globs or action == "off":
        return []
    found: list[Finding] = []
    seen: set[str] = set()
    for match in _PATH_LIKE_RE.finditer(text):
        raw = match.group(0).replace("\\", "/")
        # git diff headers name the same file twice, as a/path and b/path.
        normalised = raw.removeprefix("a/").removeprefix("b/")
        if normalised in seen:
            continue
        for glob in globs:
            if _glob_match(normalised, glob):
                seen.add(normalised)
                found.append(Finding("denied-path", action, match.start(), match.end(), normalised))
                break
    return found


def _glob_match(path: str, glob: str) -> bool:
    if fnmatch.fnmatch(path, glob):
        return True
    # fnmatch has no ** semantics, so "**/x" also has to match a bare "x".
    if glob.startswith("**/") and fnmatch.fnmatch(path, glob[3:]):
        return True
    return False


def redact(text: str, findings: Iterable[Finding]) -> tuple[str, int]:
    """Replace redact-action matches with a stable placeholder, right to left."""
    targets = sorted((f for f in findings if f.action == REDACT), key=lambda f: f.start, reverse=True)
    out = text
    for finding in targets:
        digest = hashlib.sha256(out[finding.start : finding.end].encode("utf-8")).hexdigest()[:8]
        out = f"{out[: finding.start]}[redacted:{finding.rule}:{digest}]{out[finding.end :]}"
    return out, len(targets)


def destination_allowed(model: str, policy: Policy) -> bool:
    allowed = policy.allowed_destinations
    if not allowed:
        return False
    return any(fnmatch.fnmatch(model, pattern) for pattern in allowed)


def _get_json(url: str, timeout: float = 5.0) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback only
        return json.loads(response.read().decode("utf-8"))


def _post_json(url: str, body: dict[str, Any], timeout: float) -> Any:
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback only
        return json.loads(response.read().decode("utf-8"))


def _is_loopback(server: str) -> bool:
    return server.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]"))


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
        problems.append(f"destinations.server is not loopback: {server}")

    config_path = opencode_config_path()
    forbidden = policy.data.get("posture", {}).get("forbid_permission_allow", [])
    if config_path.is_file() and forbidden:
        try:
            permissions = json.loads(config_path.read_text(encoding="utf-8")).get("permission", {})
        except (json.JSONDecodeError, OSError) as exc:
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


def build_payload(args: argparse.Namespace, workspace: Path) -> str:
    if args.stdin:
        return sys.stdin.read()
    parts: list[str] = []
    if args.task:
        parts.append(args.task)
    if args.include_diff:
        parts.append(_git(workspace, "status", "--short", "--untracked-files=all"))
        parts.append(_git(workspace, "diff", *( [f"{args.base}...HEAD"] if args.base else [])))
    return "\n\n".join(p for p in parts if p)


def _git(workspace: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=workspace, capture_output=True, text=True, check=False
    )
    return result.stdout


def audit(policy: Policy, record: dict[str, Any]) -> None:
    path = policy.audit_path
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True) + "\n"
    # Create restricted before the first write; an existing file keeps its mode.
    if not path.exists():
        handle = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        with os.fdopen(handle, "a", encoding="utf-8") as fh:
            fh.write(line)
        return
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)


def _report(findings: list[Finding], policy: Policy, stream=sys.stderr) -> None:
    limit = int(policy.data.get("limits", {}).get("max_findings_listed", 40))
    by_rule = Counter(f.rule for f in findings)
    for rule, count in by_rule.most_common():
        action = next(f.action for f in findings if f.rule == rule)
        print(f"  {action:<7} {rule} x{count}", file=stream)
    shown = [f for f in findings if f.action in (BLOCK, REDACT)][:limit]
    for finding in shown:
        print(f"    at {finding.start}: {finding.rule} {finding.excerpt}", file=stream)


def _decide(
    payload: str, policy: Policy, workspace: Path, model: str | None
) -> tuple[str, list[Finding], list[str], str]:
    problems = posture(policy, workspace)

    size = len(payload.encode("utf-8"))
    if policy.max_payload_bytes and size > policy.max_payload_bytes:
        problems.append(f"payload is {size} bytes, over limits.max_payload_bytes")

    if model is not None and not destination_allowed(model, policy):
        problems.append(
            f"destination {model!r} is not in destinations.allow "
            f"({policy.allowed_destinations or 'empty - nothing is allowed'})"
        )

    findings = scan(payload, policy)
    redacted, _ = redact(payload, findings)

    blocked = [f for f in findings if f.action == BLOCK]
    if blocked or problems:
        return "blocked", findings, problems, redacted
    return "allowed", findings, problems, redacted


def cmd_scan(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace or os.getcwd())
    policy = Policy.load(args.policy, workspace)
    payload = build_payload(args, workspace)
    findings = scan(payload, policy)
    if args.json:
        print(json.dumps([f.as_record() for f in findings], indent=2))
    else:
        print(f"policy: {policy.source}")
        print(f"payload: {len(payload.encode('utf-8'))} bytes, {len(findings)} finding(s)")
        _report(findings, policy, stream=sys.stdout)
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


def cmd_preflight(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace or os.getcwd())
    policy = Policy.load(args.policy, workspace)
    payload = build_payload(args, workspace)
    verdict, findings, problems, _ = _decide(payload, policy, workspace, args.model)

    record = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "command": "preflight",
        "workspace": str(workspace),
        "model": args.model,
        "payload_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "payload_bytes": len(payload.encode("utf-8")),
        "verdict": verdict,
        "findings": [f.as_record() for f in findings],
        "posture": problems,
    }
    audit(policy, record)

    print(f"policy: {policy.source}")
    print(f"verdict: {verdict}")
    for problem in problems:
        print(f"  fault  {problem}", file=sys.stderr)
    _report(findings, policy, stream=sys.stderr)
    return 0 if verdict == "allowed" else 2


def cmd_dispatch(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace or os.getcwd())
    policy = Policy.load(args.policy, workspace)
    payload = build_payload(args, workspace)
    verdict, findings, problems, redacted = _decide(payload, policy, workspace, args.model)

    started = time.time()
    record: dict[str, Any] = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "command": "dispatch",
        "workspace": str(workspace),
        "model": args.model,
        "payload_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "payload_bytes": len(payload.encode("utf-8")),
        "verdict": verdict,
        "findings": [f.as_record() for f in findings],
        "posture": problems,
    }

    if verdict != "allowed":
        audit(policy, record)
        print("verdict: blocked - nothing was sent", file=sys.stderr)
        for problem in problems:
            print(f"  fault  {problem}", file=sys.stderr)
        _report(findings, policy, stream=sys.stderr)
        return 2

    sent = redacted
    record["sent_sha256"] = hashlib.sha256(sent.encode("utf-8")).hexdigest()
    record["sent_bytes"] = len(sent.encode("utf-8"))

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
        audit(policy, record)
        print(f"dispatch failed: {exc}", file=sys.stderr)
        return 3

    record["elapsed_s"] = round(time.time() - started, 3)
    audit(policy, record)
    print(json.dumps(response, indent=2))
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    """Claude Code PreToolUse hook: keep delegation on the guarded path.

    A guard only guards what goes through it, so this refuses Bash calls that
    invoke a delegating agent directly, and scans prompts handed to sub-agents.
    """
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0

    workspace = Path(event.get("cwd") or os.getcwd())
    policy = Policy.load(args.policy, workspace)
    tool = event.get("tool_name", "")
    tool_input = event.get("tool_input", {}) or {}

    if tool == "Bash":
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
    findings = scan(prompt, policy)
    blocked = [f for f in findings if f.action == BLOCK]
    if blocked:
        rules = ", ".join(sorted({f.rule for f in blocked}))
        print(f"Blocked: sub-task prompt carries {rules}.", file=sys.stderr)
        return 2
    return 0


_BYPASS_RE = re.compile(
    r"opencode-companion\.mjs|\bopencode\s+(?:run|serve)\b|codex-companion\.mjs", re.IGNORECASE
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
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
