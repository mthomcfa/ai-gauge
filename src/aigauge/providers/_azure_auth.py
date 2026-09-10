"""Entra ID client-credentials tokens for the Azure Resource Manager APIs.

Deliberately no MSAL dependency. The client-credentials flow is one form POST
and a bearer string, and every other REST provider in this app (Copilot,
OpenRouter) speaks plain ``requests``. Adding an authentication SDK to this
tree would mean auditing it and restating the fork's dependency claims in
``SECURITY.md`` for no capability we use.

The token is cached in memory only - never written to disk - and is discarded
early (``_EXPIRY_SKEW``) so a token can't expire mid-request.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests

log = logging.getLogger("aigauge.providers.azure.auth")

LOGIN_HOST = "https://login.microsoftonline.com"
MANAGEMENT_SCOPE = "https://management.azure.com/.default"
REQUEST_TIMEOUT = 15
# Drop a token this long before it actually expires, so a request that starts
# just under the wire cannot be rejected mid-flight.
_EXPIRY_SKEW = timedelta(minutes=5)
# The AAD error code is documentation's "short code", e.g. invalid_client - but
# nothing upstream guarantees that shape, and the value lands verbatim in
# ai-gauge.log (a newline in it forges whole log lines), in snapshot.error, and
# in a RichText dialog header. Reduce it to the alphabet a code can have.
_CODE_RE = re.compile(r"[^A-Za-z0-9_.\-]")
_CODE_MAX_LEN = 64


class AzureAuthError(Exception):
    """Entra ID refused the client credentials.

    ``status`` is the HTTP status; ``code`` is the AAD error code (e.g.
    ``invalid_client``) when one was returned. The AAD *description* is
    deliberately not carried: it embeds tenant and application GUIDs, and this
    string reaches the tile, the log, and the diagnostics clipboard.
    """

    def __init__(self, message: str, *, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass
class _CachedToken:
    token: str
    expires_at: datetime


# Keyed by (tenant_id, client_id). Module-level, so the cache survives the
# provider rebuild that App does on every settings change - otherwise every
# OK press would mint a fresh token.
_CACHE: dict[tuple[str, str], _CachedToken] = {}
_LOCK = threading.Lock()


def token_endpoint(tenant_id: str) -> str:
    return f"{LOGIN_HOST}/{tenant_id}/oauth2/v2.0/token"


def clear_cache() -> None:
    """Forget every cached token (settings change, tests)."""
    with _LOCK:
        _CACHE.clear()


def get_token(
    tenant_id: str,
    client_id: str,
    client_secret: str,
    *,
    now: datetime | None = None,
) -> str:
    """Return a bearer token for ARM, minting one only when the cache is cold.

    Raises AzureAuthError when Entra ID rejects the credentials and
    requests.RequestException on a transport failure.
    """
    now = now or datetime.now()
    key = (tenant_id, client_id)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached.expires_at > now:
            return cached.token

    response = requests.post(
        token_endpoint(tenant_id),
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": MANAGEMENT_SCOPE,
            "grant_type": "client_credentials",
        },
        headers={"Accept": "application/json"},
        timeout=REQUEST_TIMEOUT,
        # A redirect would mean talking to a host other than Entra ID.
        allow_redirects=False,
    )
    if response.status_code != 200:
        code = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                code = _CODE_RE.sub("", str(payload.get("error") or ""))[
                    :_CODE_MAX_LEN
                ]
        except ValueError:
            pass
        log.warning(
            "provider api diagnosis provider=azure classification=token_error "
            "status=%s error_code=%s",
            response.status_code,
            code or "unknown",
        )
        raise AzureAuthError(
            f"Entra ID rejected the app registration ({response.status_code}"
            + (f", {code}" if code else "")
            + ").",
            status=response.status_code,
            code=code,
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise AzureAuthError("Entra ID returned a non-JSON token response.") from exc
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not token or not isinstance(token, str):
        raise AzureAuthError("Entra ID returned no access_token.")

    try:
        expires_in = int(payload.get("expires_in", 0) or 0)
    except (TypeError, ValueError):
        expires_in = 0
    # A missing/unusable expires_in must not produce an immortal cache entry.
    lifetime = timedelta(seconds=expires_in) if expires_in > 0 else timedelta(minutes=10)
    expires_at = now + max(timedelta(minutes=1), lifetime - _EXPIRY_SKEW)

    with _LOCK:
        _CACHE[(tenant_id, client_id)] = _CachedToken(token=token, expires_at=expires_at)
    log.debug(
        "provider api diagnosis provider=azure classification=token_ok "
        "expires_in=%s cached_for_s=%s",
        expires_in,
        int((expires_at - now).total_seconds()),
    )
    return token
