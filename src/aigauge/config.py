from __future__ import annotations

import json
import logging
import math
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
KEYRING_AZURE_CLIENT_SECRET = "azure-client-secret"
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
    height: int = 220
    collapsed: bool = False
    always_on_top: bool = True
    opacity: float = 0.8
    fade_when_inactive: bool = False
    # Whole-widget zoom. >1 enlarges for high-resolution (4K) displays; <1
    # makes it more compact. Floor is 0.75 — below that the fixed 10-12px fonts
    # become illegible. Applied via Qt's QT_SCALE_FACTOR at launch — see
    # qt_scale_factor_env().
    ui_scale: float = 1.0

    @field_validator("height", "opacity", "ui_scale", mode="before")
    @classmethod
    def _coerce_bounds(cls, value: object, info) -> float | int:
        spec = {
            "height": (220, float(WINDOW_MIN_HEIGHT), float(WINDOW_MAX_HEIGHT), True),
            "opacity": (0.8, 0.3, 1.0, False),
            "ui_scale": (1.0, 0.75, 4.0, False),
        }[info.field_name]
        default, minimum, maximum, integer = spec
        return _coerce_bounded_number(
            value, default=default, minimum=minimum, maximum=maximum,
            field=info.field_name, integer=integer,
        )


class ProviderToggles(BaseModel):
    claude: bool = True
    codex: bool = True
    copilot: bool = True
    openrouter: bool = False
    opencode_go: bool = False
    # Off by default: Azure needs an app registration and a subscription id
    # before it can report anything, so an enabled-by-default tile would show
    # every user an auth error they never asked for.
    azure: bool = False


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


def _coerce_bounded_number(
    value: object,
    *,
    default: float | int | None,
    minimum: float,
    maximum: float,
    field: str,
    integer: bool,
    allow_none: bool = False,
) -> float | int | None:
    """Clamp a user-editable numeric setting instead of rejecting it.

    ``Field(ge=..., le=...)`` raises, and a raise anywhere inside a model
    reaches ``Config.load()``'s per-key salvage, which discards the entire
    top-level block that field lives in. A single negative ``daily_budget``
    therefore took the user's OpenRouter gauge colours with it, silently. Every
    bounded setting coerces instead, so one bad number costs only that number.
    """
    if value is None:
        return None if allow_none else default
    # bool is an int subclass; True would otherwise clamp to a real value.
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        log.warning(
            "config: unusable %s (%s); using %s", field, _safe_repr(value), default
        )
        return default
    if not math.isfinite(number):
        # json.loads accepts Infinity / -Infinity / NaN by default.
        log.warning(
            "config: non-finite %s (%s); using %s", field, _safe_repr(value), default
        )
        return default
    number = max(minimum, min(maximum, number))
    return int(number) if integer else number


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
    monthly_quota: int = 1500  # AI credits; Pro=1500

    @field_validator("monthly_quota", mode="before")
    @classmethod
    def _coerce_quota(cls, value: object) -> int:
        return _coerce_bounded_number(
            value, default=1500, minimum=1, maximum=10_000_000,
            field="monthly_quota", integer=True,
        )


class OpenRouterConfig(BaseModel):
    colors: GaugeColors = Field(default_factory=ColorThresholds)
    daily_budget: float | None = None

    @field_validator("daily_budget", mode="before")
    @classmethod
    def _coerce_budget(cls, value: object) -> float | None:
        return _coerce_bounded_number(
            value, default=None, minimum=0.0, maximum=1_000_000.0,
            field="daily_budget", integer=False, allow_none=True,
        )


_GUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
# An ARM resource id, as pinned under the Foundry sub-heading. Anchored on the
# real shape rather than "starts with a slash": these strings are interpolated
# into request URLs and compared against ids returned by ARM, so a value that
# is not an ARM id is never useful and might be a URL in disguise.
_ARM_RESOURCE_ID_RE = re.compile(
    r"/subscriptions/(?P<sub>[^/]+)"
    r"/resourceGroups/(?P<rg>[A-Za-z0-9._()\-]{1,90})"
    r"/providers/(?P<ns>[A-Za-z0-9.]{1,64})"
    r"/(?P<type>[A-Za-z0-9]{1,64})"
    r"/(?P<name>[A-Za-z0-9._\-]{1,128})$",
    re.IGNORECASE,
)
# Azure resource group names; also the shape accepted for the optional
# resource-group filter setting.
_RESOURCE_GROUP_RE = re.compile(r"[A-Za-z0-9._()\-]{1,90}")


def validate_azure_guid(value: str, field: str = "id") -> str:
    """Return ``value`` if it is a bare GUID, else raise ValueError.

    Tenant, client, and subscription ids all go straight into a request URL or
    an OAuth form body. Requiring the canonical 8-4-4-4-12 form means a pasted
    URL, a path fragment, or a header-splitting payload can never reach either.
    """
    text = (value or "").strip()
    if _GUID_RE.fullmatch(text) is None:
        raise ValueError(f"Azure {field} must be a GUID (8-4-4-4-12 hex digits)")
    return text


def validate_azure_resource_id(value: str) -> str:
    """Return ``value`` if it is a well-formed ARM resource id, else raise."""
    text = (value or "").strip()
    if any(ord(ch) < 0x20 or ch in "\\ " for ch in text):
        raise ValueError("Azure resource id contains illegal characters")
    match = _ARM_RESOURCE_ID_RE.fullmatch(text)
    if match is None:
        raise ValueError(
            "Azure resource id must look like /subscriptions/<guid>/resourceGroups/"
            "<name>/providers/<namespace>/<type>/<name>"
        )
    validate_azure_guid(match.group("sub"), "subscription id")
    return text


def validate_azure_resource_group(value: str) -> str:
    """Return ``value`` if it is a plausible resource-group name, else raise."""
    text = (value or "").strip()
    if _RESOURCE_GROUP_RE.fullmatch(text) is None:
        raise ValueError(
            "Resource group names use letters, digits, and . _ ( ) - only"
        )
    return text


class AzureConfig(BaseModel):
    """Azure month-to-date spend monitor.

    Every field here is reachable from a hand-edited ``config.json``, so - like
    every other block in this file - validation *coerces* rather than raises.
    A raise would reach ``Config.load()``'s blanket handler and cost the user
    settings they never touched. An unusable id becomes ``None``, which the
    provider reports as "not configured" rather than sending anywhere.
    """

    colors: GaugeColors = Field(default_factory=ColorThresholds)
    tenant_id: str | None = None
    client_id: str | None = None
    subscription_id: str | None = None
    # In the currency the Cost Management API reports for this subscription.
    # There is no assumption that it is USD anywhere in this feature.
    monthly_allowance: float | None = None
    # Day of month the allowance resets. Capped at 28 so the boundary exists in
    # every month - a "31st" reset has no defensible meaning in February, and
    # guessing one silently mis-states the period the gauge measures.
    reset_day: int = 1
    top_rows: int = 6
    include_marketplace: bool = False
    resource_group: str | None = None
    # Foundry sub-heading: pinned resource ids, used instead of discovery when
    # the app registration lacks the Reader role needed to list accounts.
    foundry_resource_ids: list[str] = Field(default_factory=list)

    @field_validator("tenant_id", "client_id", "subscription_id", mode="before")
    @classmethod
    def _coerce_guid(cls, value: object, info) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            log.warning(
                "config: unusable azure %s (%s); ignoring",
                info.field_name,
                _safe_repr(value),
            )
            return None
        text = value.strip()
        if not text:
            return None
        try:
            return validate_azure_guid(text, info.field_name)
        except ValueError:
            log.warning("config: rejecting invalid azure %s; ignoring", info.field_name)
            return None

    @field_validator("monthly_allowance", mode="before")
    @classmethod
    def _coerce_allowance(cls, value: object) -> float | None:
        return _coerce_bounded_number(
            value, default=None, minimum=0.0, maximum=100_000_000.0,
            field="monthly_allowance", integer=False, allow_none=True,
        )

    @field_validator("reset_day", mode="before")
    @classmethod
    def _coerce_reset_day(cls, value: object) -> int:
        return _coerce_bounded_number(
            value, default=1, minimum=1, maximum=28,
            field="reset_day", integer=True,
        )

    @field_validator("top_rows", mode="before")
    @classmethod
    def _coerce_top_rows(cls, value: object) -> int:
        return _coerce_bounded_number(
            value, default=6, minimum=1, maximum=6,
            field="top_rows", integer=True,
        )

    @field_validator("include_marketplace", mode="before")
    @classmethod
    def _coerce_marketplace(cls, value: object) -> bool:
        return bool(value) if isinstance(value, bool) else False

    @field_validator("resource_group", mode="before")
    @classmethod
    def _coerce_resource_group(cls, value: object) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            return validate_azure_resource_group(value)
        except ValueError:
            log.warning("config: rejecting invalid azure resource_group; ignoring")
            return None

    @field_validator("foundry_resource_ids", mode="before")
    @classmethod
    def _coerce_resource_ids(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            if value is not None:
                log.warning("config: azure foundry_resource_ids is not a list; ignoring")
            return []
        out: list[str] = []
        # Bounded: this list is rendered as tile rows and sent as a filter, and
        # a poisoned config could otherwise make it arbitrarily long.
        for item in value[:50]:
            if not isinstance(item, str):
                continue
            try:
                out.append(validate_azure_resource_id(item))
            except ValueError:
                log.warning("config: rejecting invalid azure foundry resource id")
        return out

    def is_configured(self) -> bool:
        return bool(self.tenant_id and self.client_id and self.subscription_id)


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
    active_refresh_interval_minutes: int = 5
    refresh_interval_minutes: int = 60
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
    azure: AzureConfig = Field(default_factory=AzureConfig)
    expanded_tiles: list[str] = Field(default_factory=list)
    collapsed_tiles: list[str] = Field(default_factory=list)
    # When each provider kind last asked its page for every meter it renders,
    # ISO-8601 per kind. Empty (or missing) means the next refresh re-scans -
    # which is also how the Settings "Re-scan meters now" button works.
    meter_catalog_last_scan: dict[str, str] = Field(default_factory=dict)
    window: WindowState = Field(default_factory=WindowState)

    @field_validator("meter_catalog_last_scan", mode="before")
    @classmethod
    def _coerce_scan_stamps(cls, value: object) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        return {
            str(key): item
            for key, item in value.items()
            if isinstance(key, str) and isinstance(item, str)
        }

    @field_validator(
        "active_refresh_interval_minutes", "refresh_interval_minutes", mode="before"
    )
    @classmethod
    def _coerce_interval(cls, value: object, info) -> int:
        default = 5 if info.field_name.startswith("active") else 60
        return _coerce_bounded_number(
            value, default=default, minimum=1, maximum=180,
            field=info.field_name, integer=True,
        )

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
        "azure": "Microsoft · Azure",
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


def get_azure_client_secret() -> str | None:
    """Read the Entra ID application secret from the OS credential store.

    There is no plaintext-file fallback here, deliberately. The legacy
    ``secrets.dat`` path exists only to migrate PATs written by an older
    release; a secret introduced now has no such history and must never be
    written anywhere but the keychain.
    """
    try:
        secret = keyring.get_password(KEYRING_SERVICE, KEYRING_AZURE_CLIENT_SECRET)
        if secret:
            return secret
    except keyring.errors.KeyringError:
        pass
    return None


def set_azure_client_secret(secret: str | None) -> None:
    # A bearer token minted from the old secret is a live derived credential.
    # Both paths drop it: "Clear saved client secret" must not leave one
    # resident in memory, and a rotated secret must take effect now rather
    # than when the cached token happens to expire. Imported locally to keep
    # this module free of provider imports.
    from .providers._azure_auth import clear_cache

    if secret:
        keyring.set_password(KEYRING_SERVICE, KEYRING_AZURE_CLIENT_SECRET, secret)
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_AZURE_CLIENT_SECRET)
        except keyring.errors.KeyringError:
            pass
    clear_cache()


def _cookie_key(provider: str) -> str:
    return f"cookie-{provider}"


def get_provider_cookie(provider: str) -> str | None:
    return get_platform().load_secret(_cookie_key(provider))


def set_provider_cookie(provider: str, value: str | None) -> None:
    get_platform().save_secret(_cookie_key(provider), value)
