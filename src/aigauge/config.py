from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Annotated, Any

import keyring
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .platforms import APP_NAME, get_platform

log = logging.getLogger("aigauge.config")

DEFAULT_OPENCODE_USAGE_URL = (
    "https://opencode.ai/workspace/wrk_01KX3HT8MFWCMHR2289KGPZ1RD/go"
)

KEYRING_SERVICE = "ai-gauge"
KEYRING_GITHUB_PAT = "github-pat"
KEYRING_OPENROUTER_KEY = "openrouter-key"
KEYRING_OPENROUTER_MGMT_KEY = "openrouter-mgmt-key"
WINDOW_WIDTH = 340
WINDOW_MIN_HEIGHT = 80
WINDOW_MAX_HEIGHT = 420
WINDOW_COLLAPSED_HEIGHT = 58

# Per-provider session cookie names (HttpOnly cookies you can't read via JS).
# COOKIE_NAMES is the primary name shown in the UI. COOKIE_NAME_ALIASES covers
# provider auth migrations and split cookies copied from browser DevTools.
COOKIE_NAMES = {
    "claude": "sessionKey",
    "codex": "next-auth.session-token",
    "opencode_go": "opencode-session",
}
COOKIE_NAME_ALIASES = {
    "claude": ("sessionKey",),
    "codex": (
        "next-auth.session-token",
        "__Secure-next-auth.session-token",
        "next-auth.session-token.0",
        "next-auth.session-token.1",
        "__Secure-next-auth.session-token.0",
        "__Secure-next-auth.session-token.1",
    ),
    # OpenCode auth cookie names are not stable/documented. The paste flow
    # keeps the full Cookie header rather than relying on aliases.
    "opencode_go": ("opencode-session",),
}
COOKIE_DOMAINS = {
    "claude": ".claude.ai",
    "codex": ".chatgpt.com",
    "opencode_go": ".opencode.ai",
}


def app_data_dir() -> Path:
    """Per-OS config / log / secrets directory.

    - Windows: ``%APPDATA%/ai-gauge``
    - macOS:   ``~/Library/Application Support/ai-gauge``
    - Linux:   ``$XDG_CONFIG_HOME/ai-gauge`` (or ``~/.config/ai-gauge``)
    """
    return get_platform().app_data_dir()


# Account / profile ids become a filesystem path component under profiles/.
# Restrict them to the shape our own generators produce (slugs, hex suffixes,
# and the fixed provider ids like ``opencode_go``) so a poisoned config.json
# can never turn an id into a path-traversal payload.
_PROFILE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

# Windows treats these as device names regardless of any extension, so a
# profiles/<id> path built from one would target the device, not a directory.
# App-generated ids never collide with these; reject them for the poisoned
# -config threat model.
_WIN_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def _is_safe_profile_id(provider: str) -> bool:
    if not provider or _PROFILE_ID_RE.fullmatch(provider) is None:
        return False
    return provider.split(".", 1)[0].lower() not in _WIN_RESERVED_NAMES


def webview_profile_dir(provider: str) -> Path:
    """Path to a provider/account's QtWebEngine profile.

    Validates ``provider`` and confirms the resulting path stays inside the
    ``profiles/`` root before returning it — every profile create/open/delete
    routes through here, so this is the single traversal chokepoint.
    """
    if not _is_safe_profile_id(provider):
        raise ValueError(f"unsafe profile id: {provider!r}")
    profiles_root = app_data_dir() / "profiles"
    target = profiles_root / provider
    resolved = target.resolve()
    root_resolved = profiles_root.resolve()
    if resolved != root_resolved and not resolved.is_relative_to(root_resolved):
        raise ValueError(f"profile path escapes root: {provider!r}")
    return target


def config_path() -> Path:
    return app_data_dir() / "config.json"


class WindowState(BaseModel):
    x: int | None = None
    y: int | None = None
    width: int = WINDOW_WIDTH
    height: int = Field(default=220, ge=WINDOW_MIN_HEIGHT, le=WINDOW_MAX_HEIGHT)
    collapsed: bool = False
    always_on_top: bool = True
    opacity: float = Field(default=0.8, ge=0.3, le=1.0)
    fade_when_inactive: bool = False
    # Whole-widget zoom. >1 enlarges for high-resolution (4K) displays; <1
    # makes it more compact. Floor is 0.75 — below that the fixed 10-12px fonts
    # become illegible. Applied via Qt's QT_SCALE_FACTOR at launch — see
    # qt_scale_factor_env().
    ui_scale: float = Field(default=1.0, ge=0.75, le=4.0)


class ProviderToggles(BaseModel):
    claude: bool = True
    codex: bool = True
    copilot: bool = True
    openrouter: bool = False
    opencode_go: bool = False


_HEX_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")

_DEFAULT_BAND_COLORS = {
    "green_color": "#22c55e",
    "yellow_color": "#f59e0b",
    "orange_color": "#f97316",
    "red_color": "#ef4444",
}
_DEFAULT_CUTOFFS = {"green_max": 59, "yellow_max": 79, "orange_max": 94}


def _safe_repr(value: object, limit: int = 120) -> str:
    """Describe an untrusted value for a log line without ever raising.

    ``repr()`` is not total. A deeply nested structure raises ``RecursionError``
    and an integer of more than 4300 digits raises ``ValueError`` (CPython's
    int/str conversion limit). ``logging`` swallows a ``ValueError`` raised
    while formatting, but ``RecursionError`` propagates - the stack is still
    exhausted when the error handler runs - so a bare ``%r`` in a validator can
    take down the caller. These validators promise never to raise, so they
    cannot use ``%r`` on attacker-controlled input.
    """
    try:
        text = repr(value)
    except Exception:  # noqa: BLE001 - describing a value must never fail
        return type(value).__name__
    if len(text) > limit:
        return f"{type(value).__name__}, {text[:limit]}..."
    return text


class ColorThresholds(BaseModel):
    """Per-account gauge severity bands.

    Defaults reproduce the original fixed behaviour exactly: green below 60%,
    yellow 60-79%, orange 80-94%, red 95%+.

    Every value here is attacker-reachable through a hand-edited ``config.json``
    and the colours are interpolated into Qt stylesheets, so all of it is
    validated. Validation *never raises*: every check runs ``mode="before"`` and
    coerces to the band default. That matters because ``Config.load()`` catches
    any exception and returns a blank config, so one bad value in here would
    otherwise silently destroy every unrelated setting the user has.
    """

    model_config = ConfigDict(validate_assignment=True)

    green_max: int = _DEFAULT_CUTOFFS["green_max"]
    yellow_max: int = _DEFAULT_CUTOFFS["yellow_max"]
    orange_max: int = _DEFAULT_CUTOFFS["orange_max"]
    green_color: str = _DEFAULT_BAND_COLORS["green_color"]
    yellow_color: str = _DEFAULT_BAND_COLORS["yellow_color"]
    orange_color: str = _DEFAULT_BAND_COLORS["orange_color"]
    red_color: str = _DEFAULT_BAND_COLORS["red_color"]

    @field_validator("green_max", "yellow_max", "orange_max", mode="before")
    @classmethod
    def _coerce_cutoff(cls, value: object, info) -> int:
        # Runs before pydantic's int parsing and range checks, both of which
        # raise. Anything unusable becomes the default; anything numeric is
        # clamped into 0-100 rather than rejected.
        default = _DEFAULT_CUTOFFS[info.field_name]
        if isinstance(value, bool) or value is None:
            return default
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError):
            # OverflowError matters: json.loads accepts the non-standard
            # literals Infinity / -Infinity / 1e400, and int(float("inf"))
            # raises it. Anything escaping here reaches Config.load() and costs
            # the user settings they did not touch.
            log.warning(
                "config: unusable %s (%s); using %s",
                info.field_name,
                _safe_repr(value),
                default,
            )
            return default
        return max(0, min(100, number))

    @field_validator("green_color", "yellow_color", "orange_color", "red_color",
                     mode="before")
    @classmethod
    def _coerce_color(cls, value: object, info) -> str:
        # These strings are interpolated into Qt stylesheets
        # (``background:{color}``). Anything other than a plain #RRGGBB literal
        # could close the declaration and inject arbitrary QSS - including
        # url() fetches - so refuse it and fall back to the band default.
        # Non-string input is rejected here too, before pydantic's str check
        # can raise.
        default = _DEFAULT_BAND_COLORS[info.field_name]
        text = value.strip() if isinstance(value, str) else ""
        if _HEX_COLOR_RE.fullmatch(text):
            return text
        log.warning(
            "config: rejecting invalid %s (%s); using %s",
            info.field_name,
            _safe_repr(value),
            default,
        )
        return default

    @model_validator(mode="after")
    def _validate_band_order(self) -> ColorThresholds:
        # Bands must be non-decreasing or band_for_percent() produces
        # unreachable ranges. Repair in place instead of rejecting the config.
        if not (self.green_max <= self.yellow_max <= self.orange_max):
            log.warning(
                "config: gauge cutoffs out of order (%s/%s/%s); using defaults",
                self.green_max,
                self.yellow_max,
                self.orange_max,
            )
            # object.__setattr__ avoids re-entering validation under
            # validate_assignment=True.
            object.__setattr__(self, "green_max", _DEFAULT_CUTOFFS["green_max"])
            object.__setattr__(self, "yellow_max", _DEFAULT_CUTOFFS["yellow_max"])
            object.__setattr__(self, "orange_max", _DEFAULT_CUTOFFS["orange_max"])
        return self


def _coerce_colors_payload(value: object) -> object:
    """Accept only a mapping (or an existing model) for a ``colors`` block.

    A scalar or list in that slot would raise during validation and take the
    whole config down with it; an empty mapping yields the defaults instead.
    """
    if isinstance(value, (ColorThresholds, dict)):
        return value
    log.warning(
        "config: ignoring non-mapping colors block (%s); using defaults",
        _safe_repr(value),
    )
    return {}


# Every ``colors`` field uses this so malformed payloads degrade to defaults.
GaugeColors = Annotated[ColorThresholds, BeforeValidator(_coerce_colors_payload)]


class BrowserAccount(BaseModel):
    id: str
    kind: str
    name: str | None = None
    enabled: bool = True
    colors: GaugeColors = Field(default_factory=ColorThresholds)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        # The id is used verbatim as a profiles/ path component and as a
        # keyring/secret name; keep it to the generated slug-<hex> / fixed-id
        # shape so it can never carry a path-traversal or separator payload.
        if not _is_safe_profile_id(value):
            raise ValueError(f"unsafe browser account id: {value!r}")
        return value


class CopilotConfig(BaseModel):
    colors: GaugeColors = Field(default_factory=ColorThresholds)
    username: str | None = None
    billing_org: str | None = None
    monthly_quota: int = Field(default=1500, ge=1)  # AI credits; Pro=1500


class OpenRouterConfig(BaseModel):
    colors: GaugeColors = Field(default_factory=ColorThresholds)
    daily_budget: float | None = Field(default=None, ge=0)


def validate_opencode_usage_url(value: str) -> str:
    """Return ``value`` if it is a safe OpenCode usage URL, else raise ValueError.

    The URL is loaded into the embedded, authenticated browser, so it must be
    a plain ``https`` page on ``opencode.ai`` with no way to redirect the signed
    -in session elsewhere. Rejects non-https schemes (``file:``/``data:``/…),
    embedded credentials, an explicit port, IP-literal or look-alike hosts, and
    a bare/rootless path.
    """
    from urllib.parse import urlparse

    text = (value or "").strip()
    # Reject control characters and backslashes up front. Python's urlparse and
    # Qt's QUrl disagree on how to handle these (a backslash-injection host like
    # ``evil.com\.opencode.ai`` parses "safe" here but becomes a different/empty
    # host in QUrl); refusing them keeps the two parsers from ever diverging.
    if any(ord(ch) < 0x20 or ch in "\\ " for ch in text):
        raise ValueError("OpenCode usage URL contains illegal characters")
    parsed = urlparse(text)
    if parsed.scheme != "https":
        raise ValueError("OpenCode usage URL must use https")
    if parsed.username or parsed.password:
        raise ValueError("OpenCode usage URL must not contain embedded credentials")
    if parsed.port is not None:
        raise ValueError("OpenCode usage URL must not specify a port")
    host = (parsed.hostname or "").lower()
    # An exact host / real subdomain match also rejects IP literals and
    # look-alike hosts such as opencode.ai.evil.com.
    if host != "opencode.ai" and not host.endswith(".opencode.ai"):
        raise ValueError("OpenCode usage URL host must be opencode.ai")
    if not parsed.path or parsed.path == "/":
        raise ValueError("OpenCode usage URL must include a workspace path")
    return text


class OpenCodeGoConfig(BaseModel):
    colors: GaugeColors = Field(default_factory=ColorThresholds)
    usage_url: str = DEFAULT_OPENCODE_USAGE_URL

    @field_validator("usage_url")
    @classmethod
    def _validate_usage_url(cls, value: str) -> str:
        # Coerce an unsafe/invalid URL to the safe default rather than raising:
        # a raise would bubble out of Config.load()'s blanket except and reset
        # the WHOLE config (losing PAT username, budgets, named accounts, window
        # prefs). The security property still holds — the unsafe URL is never
        # loaded — and the rest of the user's settings survive. The standalone
        # validate_opencode_usage_url() still raises for the Settings dialog and
        # the runtime load guard.
        try:
            return validate_opencode_usage_url(value)
        except ValueError:
            log.warning(
                "config: rejecting unsafe OpenCode usage_url; using default"
            )
            return DEFAULT_OPENCODE_USAGE_URL


def _quarantine_config(path: Path, raw: str) -> None:
    """Keep a copy of a config file we are about to stop honouring.

    ``Config.save()`` overwrites the file on the next settings change or window
    move, so anything we discard here is gone for good otherwise. A single
    fixed suffix keeps a repeatedly-failing load from filling the directory.
    """
    backup = path.with_suffix(path.suffix + ".corrupt")
    try:
        backup.write_text(raw, encoding="utf-8")
    except OSError:
        log.exception("config: could not preserve %s", path)
        return
    log.warning("config: previous contents preserved at %s", backup)


class Config(BaseModel):
    active_refresh_interval_minutes: int = Field(default=5, ge=1, le=180)
    refresh_interval_minutes: int = Field(default=60, ge=1, le=180)
    start_at_login: bool = False
    providers: ProviderToggles = Field(default_factory=ProviderToggles)
    browser_accounts: list[BrowserAccount] = Field(
        default_factory=lambda: [
            BrowserAccount(id="claude", kind="claude"),
            BrowserAccount(id="codex", kind="codex"),
        ]
    )
    copilot: CopilotConfig = Field(default_factory=CopilotConfig)
    openrouter: OpenRouterConfig = Field(default_factory=OpenRouterConfig)
    opencode_go: OpenCodeGoConfig = Field(default_factory=OpenCodeGoConfig)
    expanded_tiles: list[str] = Field(default_factory=list)
    collapsed_tiles: list[str] = Field(default_factory=list)
    window: WindowState = Field(default_factory=WindowState)

    @classmethod
    def load(cls) -> Config:
        path = config_path()
        if not path.exists():
            return cls()
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            log.exception("config: cannot read %s; using defaults", path)
            return cls()

        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001 - json raises several unrelated types
            # Not just JSONDecodeError: a >4300-digit number literal raises
            # ValueError and deep nesting raises RecursionError.
            log.exception("config: %s is not readable JSON", path)
            _quarantine_config(path, raw)
            return cls()

        if not isinstance(data, dict):
            log.warning("config: %s is not a JSON object; using defaults", path)
            _quarantine_config(path, raw)
            return cls()

        try:
            cls._migrate(data)
        except Exception:  # noqa: BLE001 - a failed migration must not be fatal
            log.exception("config: migration failed; loading the file as-is")

        try:
            return cls.model_validate(data)
        except Exception:  # noqa: BLE001 - fall through to per-key salvage
            log.warning("config: %s failed validation; salvaging valid settings", path)
        _quarantine_config(path, raw)
        return cls._salvage(data)

    @classmethod
    def _salvage(cls, data: dict[str, Any]) -> Config:
        """Build a Config from the subset of ``data`` that validates.

        Previously any single bad value discarded the entire file: one bogus
        ``window.height`` cost the user their named accounts, Copilot quota,
        OpenRouter budget and window geometry, and the next save made that
        permanent. Settings the user never touched must survive a bad
        neighbour, so re-validate one top-level key at a time and drop only the
        keys that are actually broken.
        """
        good: dict[str, Any] = {}
        for key, value in data.items():
            candidate = {**good, key: value}
            try:
                cls.model_validate(candidate)
            except Exception:  # noqa: BLE001 - any failure means drop this key
                log.warning("config: dropping unusable setting %s", _safe_repr(key))
                continue
            good = candidate
        try:
            return cls.model_validate(good)
        except Exception:  # noqa: BLE001 - should be unreachable; never crash
            log.exception("config: salvage failed; using defaults")
            return cls()

    @staticmethod
    def _migrate(data: dict[str, Any]) -> None:
        # 0.1.x had a single refresh_interval_minutes value. Preserve that as
        # the active cadence and let the new idle cap default to 60 minutes.
        if "active_refresh_interval_minutes" not in data:
            old_interval = data.get("refresh_interval_minutes")
            if isinstance(old_interval, int):
                data["active_refresh_interval_minutes"] = old_interval
                data["refresh_interval_minutes"] = 60
        # 0.5.x renamed start_with_windows to start_at_login (cross-platform).
        if "start_at_login" not in data and "start_with_windows" in data:
            data["start_at_login"] = bool(data.pop("start_with_windows"))
        providers = data.get("providers")
        if not isinstance(providers, dict):
            providers = {}
        if "browser_accounts" not in data:
            data["browser_accounts"] = [
                {
                    "id": "claude",
                    "kind": "claude",
                    "name": None,
                    "enabled": bool(providers.get("claude", True)),
                },
                {
                    "id": "codex",
                    "kind": "codex",
                    "name": None,
                    "enabled": bool(providers.get("codex", True)),
                },
            ]
        elif isinstance(data.get("browser_accounts"), list):
            accounts = [
                item for item in data["browser_accounts"] if isinstance(item, dict)
            ]
            # Drop entries whose id can't be a safe profiles/ path component
            # before validation runs. Otherwise one poisoned id would raise out
            # of Config.load()'s blanket except and discard the entire config;
            # dropping just the bad account preserves everything else.
            accounts = [
                item
                for item in accounts
                if _is_safe_profile_id(str(item.get("id") or ""))
            ]
            ids = {str(item.get("id") or "") for item in accounts}
            if "claude" not in ids:
                accounts.insert(
                    0,
                    {
                        "id": "claude",
                        "kind": "claude",
                        "name": None,
                        "enabled": bool(providers.get("claude", True)),
                    },
                )
            if "codex" not in ids:
                accounts.append(
                    {
                        "id": "codex",
                        "kind": "codex",
                        "name": None,
                        "enabled": bool(providers.get("codex", True)),
                    }
                )
            data["browser_accounts"] = accounts
        window = data.get("window")
        if isinstance(window, dict):
            width = window.get("width")
            height = window.get("height")
            if isinstance(width, int):
                window["width"] = WINDOW_WIDTH
            if isinstance(height, int):
                window["height"] = max(WINDOW_MIN_HEIGHT, min(height, WINDOW_MAX_HEIGHT))
        copilot = data.get("copilot")
        if isinstance(copilot, dict) and copilot.get("monthly_quota") == 300:
            copilot["monthly_quota"] = 1500

    def save(self) -> None:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.model_dump(), indent=2, default=str),
            encoding="utf-8",
        )


def qt_scale_factor_env(config: Config) -> str | None:
    """QT_SCALE_FACTOR string for the configured UI scale, or None at 1.0.

    Qt reads QT_SCALE_FACTOR once, before QApplication is constructed, and uses
    it to scale the whole (otherwise fixed-pixel) widget — the lever behind the
    Settings "UI scale" option. Returns None when the scale is effectively 1.0
    so Qt's own per-monitor DPI handling is left untouched.
    """
    scale = float(getattr(config.window, "ui_scale", 1.0) or 1.0)
    if abs(scale - 1.0) <= 1e-3:
        return None
    return f"{scale:g}"


def provider_base_name(kind: str) -> str:
    return {"claude": "Claude", "codex": "Codex"}.get(kind, kind.title())


def account_display_name(account: BrowserAccount) -> str:
    base = provider_base_name(account.kind)
    label = (account.name or "").strip()
    return f"{base} ({label})" if label else base


def browser_accounts(
    config: Config,
    *,
    kind: str | None = None,
    enabled_only: bool = False,
) -> list[BrowserAccount]:
    accounts = [
        account
        for account in getattr(config, "browser_accounts", [])
        if account.kind in ("claude", "codex")
    ]
    if kind is not None:
        accounts = [account for account in accounts if account.kind == kind]
    if enabled_only:
        accounts = [account for account in accounts if account.enabled]
    return accounts


def browser_account(config: Config, account_id: str) -> BrowserAccount | None:
    for account in browser_accounts(config):
        if account.id == account_id:
            return account
    return None


def account_kind(config: Config, account_id: str) -> str | None:
    account = browser_account(config, account_id)
    if account is not None:
        return account.kind
    if account_id in ("claude", "codex"):
        return account_id
    if account_id.startswith("claude-"):
        return "claude"
    if account_id.startswith("codex-"):
        return "codex"
    if account_id == "opencode_go":
        return "opencode_go"
    return None


def display_name_for_account(config: Config, account_id: str) -> str:
    account = browser_account(config, account_id)
    if account is not None:
        return account_display_name(account)
    return {
        "claude": "Claude",
        "codex": "Codex",
        "copilot": "Copilot",
        "openrouter": "OpenRouter",
        "opencode_go": "OpenCode",
    }.get(account_id, account_id)


def generate_browser_account_id(config: Config, kind: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", kind.lower()).strip("-") or "account"
    existing = {account.id for account in config.browser_accounts}
    while True:
        candidate = f"{slug}-{uuid.uuid4().hex[:8]}"
        if candidate not in existing:
            return candidate


def get_github_pat() -> str | None:
    try:
        pat = keyring.get_password(KEYRING_SERVICE, KEYRING_GITHUB_PAT)
        if pat:
            return pat
    except keyring.errors.KeyringError:
        pass
    legacy_pat = _load_legacy_github_pat()
    if not legacy_pat:
        return None
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_GITHUB_PAT, legacy_pat)
    except keyring.errors.KeyringError:
        return legacy_pat
    _delete_legacy_github_pat()
    return legacy_pat


def set_github_pat(pat: str | None) -> None:
    if pat:
        keyring.set_password(KEYRING_SERVICE, KEYRING_GITHUB_PAT, pat)
        _delete_legacy_github_pat()
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_GITHUB_PAT)
        except keyring.errors.KeyringError:
            pass
        _delete_legacy_github_pat()


def _load_legacy_github_pat() -> str | None:
    from . import secret_storage

    return secret_storage.load_secret(KEYRING_GITHUB_PAT)


def _delete_legacy_github_pat() -> None:
    from . import secret_storage

    try:
        secret_storage.save_secret(KEYRING_GITHUB_PAT, None)
    except RuntimeError:
        # Non-Windows production hosts refuse to write plaintext secrets.dat.
        # PAT storage itself has already used the system keyring; this cleanup
        # is only for the old sidecar-file migration path.
        pass


def get_openrouter_key() -> str | None:
    try:
        key = keyring.get_password(KEYRING_SERVICE, KEYRING_OPENROUTER_KEY)
        if key:
            return key
    except keyring.errors.KeyringError:
        pass
    return None


def set_openrouter_key(key: str | None) -> None:
    if key:
        keyring.set_password(KEYRING_SERVICE, KEYRING_OPENROUTER_KEY, key)
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_OPENROUTER_KEY)
        except keyring.errors.KeyringError:
            pass


def get_openrouter_mgmt_key() -> str | None:
    try:
        key = keyring.get_password(KEYRING_SERVICE, KEYRING_OPENROUTER_MGMT_KEY)
        if key:
            return key
    except keyring.errors.KeyringError:
        pass
    return None


def set_openrouter_mgmt_key(key: str | None) -> None:
    if key:
        keyring.set_password(KEYRING_SERVICE, KEYRING_OPENROUTER_MGMT_KEY, key)
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_OPENROUTER_MGMT_KEY)
        except keyring.errors.KeyringError:
            pass


def _cookie_key(provider: str) -> str:
    return f"cookie-{provider}"


def get_provider_cookie(provider: str) -> str | None:
    return get_platform().load_secret(_cookie_key(provider))


def set_provider_cookie(provider: str, value: str | None) -> None:
    get_platform().save_secret(_cookie_key(provider), value)
