from __future__ import annotations

import logging
import shutil

from PyQt6.QtCore import QT_VERSION_STR
from PyQt6.QtWebEngineCore import (
    QWebEngineProfile,
    qWebEngineChromiumSecurityPatchVersion,
    qWebEngineChromiumVersion,
    qWebEngineVersion,
)

from ..config import app_data_dir, webview_profile_dir

log = logging.getLogger("aigauge.webview.profile")

_REALISTIC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

_profiles: dict[str, QWebEngineProfile] = {}


def get_profile(provider: str) -> QWebEngineProfile:
    """Return a per-provider persistent QWebEngineProfile.

    Profiles share the process but keep separate cookie/cache stores on disk so
    that signing into Claude doesn't overlap with ChatGPT cookies.
    """
    if provider in _profiles:
        return _profiles[provider]

    storage_dir = webview_profile_dir(provider)
    storage_dir.mkdir(parents=True, exist_ok=True)

    profile = QWebEngineProfile(f"ai-gauge-{provider}")
    profile.setPersistentStoragePath(str(storage_dir))
    profile.setCachePath(str(storage_dir / "cache"))
    profile.setPersistentCookiesPolicy(
        QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
    )
    profile.setHttpUserAgent(_REALISTIC_UA)

    log.info(
        "webengine profile created provider=%s storage=%s cache=%s ua=%r "
        "qt=%s webengine=%s chromium=%s chromium_patch=%s",
        provider,
        storage_dir,
        storage_dir / "cache",
        _REALISTIC_UA,
        QT_VERSION_STR,
        qWebEngineVersion(),
        qWebEngineChromiumVersion(),
        qWebEngineChromiumSecurityPatchVersion(),
    )

    _profiles[provider] = profile
    return profile


def purge_profile(account_id: str) -> None:
    """Tear down and delete an account's QtWebEngine profile.

    Releases the cached ``QWebEngineProfile`` (clearing its cookie store and
    HTTP cache first), then recursively removes the on-disk profile directory
    so a removed account leaves no live session, cache, or persisted cookies
    behind. The directory delete is guarded: it only proceeds for a path that
    resolves strictly inside the ``profiles/`` root.
    """
    profile = _profiles.pop(account_id, None)
    if profile is not None:
        try:
            store = profile.cookieStore()
            if store is not None:
                store.deleteAllCookies()
            profile.clearHttpCache()
        except RuntimeError:
            # The underlying C++ object may already be gone; the directory
            # delete below is what actually reclaims the data.
            pass
        profile.deleteLater()

    try:
        target = webview_profile_dir(account_id)
    except ValueError:
        log.warning("purge_profile: refusing unsafe account id %r", account_id)
        return
    profiles_root = (app_data_dir() / "profiles").resolve()
    resolved = target.resolve()
    if resolved == profiles_root or not resolved.is_relative_to(profiles_root):
        log.warning(
            "purge_profile: refusing to delete %s (outside %s)",
            resolved,
            profiles_root,
        )
        return
    if resolved.exists():
        shutil.rmtree(resolved, ignore_errors=True)
        log.info("purge_profile: deleted profile dir for account=%s", account_id)
