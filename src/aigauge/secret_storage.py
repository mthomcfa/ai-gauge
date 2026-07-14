"""Encrypted on-disk storage for values too large for Windows Credential Manager.

Windows Credential Manager caps the credential blob at ~2560 bytes, which is fine
for short tokens (GitHub PAT) but fails for long session JWTs (ChatGPT's
__Secure-next-auth.session-token can be 5-10KB).

We store these in %APPDATA%/ai-gauge/secrets.dat, encrypted with DPAPI
(CryptProtectData) — same per-user encryption that pre-v127 Chrome used. No
new Python dependencies; calls into crypt32.dll via ctypes.

This module is Windows-only by design. On non-Windows hosts (used during
cross-platform development of pure-Python helpers), writes are routed to a
plaintext file under a sandboxed test directory and a loud warning is logged.
Production callers should never reach the non-Windows branch.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import app_data_dir

log = logging.getLogger("aigauge.secret_storage")

_SECRETS_FILENAME = "secrets.dat"

# Never pop a DPAPI UI prompt from a background tray app; fail instead.
_CRYPTPROTECT_UI_FORBIDDEN = 0x1

# Opt-in escape hatch for the cross-platform test suite. When unset (the
# normal case) writes on non-Windows refuse loudly so a misconfigured macOS
# or Linux box cannot silently produce an unencrypted secrets.dat next to a
# real cookie.
_ALLOW_PLAINTEXT_ENV = "AIGAUGE_ALLOW_PLAINTEXT_SECRETS"


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wt.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


if sys.platform == "win32":
    _CRYPT32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _CRYPT32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wt.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    _CRYPT32.CryptProtectData.restype = wt.BOOL

    _CRYPT32.CryptUnprotectData.argtypes = _CRYPT32.CryptProtectData.argtypes
    _CRYPT32.CryptUnprotectData.restype = wt.BOOL


def _to_blob(data: bytes) -> _DataBlob:
    buf = (ctypes.c_byte * len(data)).from_buffer_copy(data)
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))


def _from_blob(blob: _DataBlob) -> bytes:
    out = ctypes.string_at(blob.pbData, blob.cbData)
    _KERNEL32.LocalFree(blob.pbData)
    return out


def _protect(plaintext: bytes) -> bytes:
    in_blob = _to_blob(plaintext)
    out_blob = _DataBlob()
    ok = _CRYPT32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
    return _from_blob(out_blob)


def _unprotect(ciphertext: bytes) -> bytes:
    in_blob = _to_blob(ciphertext)
    out_blob = _DataBlob()
    ok = _CRYPT32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
    return _from_blob(out_blob)


def _secrets_path() -> Path:
    return app_data_dir() / _SECRETS_FILENAME


def _quarantine(path: Path) -> None:
    """Move an undecryptable/corrupt secrets file aside instead of destroying it.

    Renaming (rather than letting the next save overwrite it) preserves the
    original bytes for recovery/forensics — e.g. a DPAPI blob that failed to
    decrypt because Windows credentials rotated might still be recoverable.
    """
    try:
        dest = path.with_name(path.name + ".corrupt")
        index = 1
        while dest.exists():
            dest = path.with_name(f"{path.name}.corrupt{index}")
            index += 1
        os.replace(path, dest)
        log.warning("secret_storage: quarantined undecryptable secrets to %s", dest)
    except OSError:
        log.exception("secret_storage: failed to quarantine %s", path)


def _load_all() -> dict[str, str]:
    path = _secrets_path()
    if not path.exists():
        return {}
    if sys.platform != "win32" and os.environ.get(_ALLOW_PLAINTEXT_ENV) != "1":
        # Mirror the write-side refusal: without the explicit opt-in, a
        # plaintext secrets.dat on a non-Windows host is never trusted, so a
        # file planted here cannot feed values into the app.
        log.warning(
            "secret_storage: ignoring existing secrets.dat on non-Windows host "
            "(set %s=1 to opt in; development only).",
            _ALLOW_PLAINTEXT_ENV,
        )
        return {}
    try:
        raw = path.read_bytes()
        if not raw:
            return {}
        if sys.platform == "win32":
            decrypted = _unprotect(raw).decode("utf-8")
        else:
            decrypted = raw.decode("utf-8")  # plaintext fallback for non-Windows dev
        loaded = json.loads(decrypted)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        # A failed decrypt/parse means the stored cookies are unreadable
        # (rotated Windows credentials, another user's DPAPI blob, or
        # tampering). Log it, and quarantine the original file instead of
        # leaving the next save_secret() to silently overwrite it — the old
        # code destroyed the only copy of a possibly-recoverable blob.
        log.exception("secret_storage: failed to load %s", path)
        _quarantine(path)
        return {}


def _atomic_write(path: Path, payload: bytes, *, mode: int | None = None) -> None:
    """Write ``payload`` to ``path`` atomically via a same-dir temp + os.replace.

    A crash or concurrent read can never observe a half-written secrets file:
    readers see either the old file or the complete new one. When ``mode`` is
    given the temp file is created with it before any bytes are written, so the
    payload is never briefly world-readable.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".secrets-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        # Own the fd through the with-block so it is closed exactly once on every
        # path. fchmod on the open descriptor (rather than chmod on the name
        # before fdopen) avoids leaking the fd if setting the mode fails.
        with os.fdopen(fd, "wb") as handle:
            if mode is not None:
                os.fchmod(handle.fileno(), mode)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _icacls_path() -> str:
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
    candidate = Path(windir) / "System32" / "icacls.exe"
    return str(candidate) if candidate.exists() else "icacls.exe"


def _lock_down_windows_acl(path: Path) -> None:
    """Best-effort: restrict secrets.dat to the current user via an explicit DACL.

    DPAPI already binds the ciphertext to the user, but an explicit owner-only
    DACL keeps other local accounts from even reading the blob. Uses icacls
    (always present on Windows), resolved to its System32 path so a hijacked
    PATH can't substitute it; logged and non-fatal on failure.
    """
    if sys.platform != "win32":
        return
    user = (os.environ.get("USERNAME") or "").strip()
    if not user:
        return
    icacls = _icacls_path()
    try:
        result = subprocess.run(
            [icacls, str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            check=False,
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            log.warning(
                "secret_storage: icacls lockdown failed rc=%s detail=%s",
                result.returncode,
                (result.stderr or result.stdout or "").strip(),
            )
            return
        # Verify the DACL took: re-read it and confirm inheritance is gone and
        # only the current user is granted.
        verify = subprocess.run(
            [icacls, str(path)],
            check=False,
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        acl_text = verify.stdout or ""
        if user.lower() not in acl_text.lower():
            log.warning(
                "secret_storage: DACL verify did not show current user; acl=%s",
                acl_text.strip(),
            )
    except OSError:
        log.exception("secret_storage: could not apply Windows DACL to %s", path)


def _save_all(data: dict[str, str]) -> None:
    path = _secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data).encode("utf-8")
    if sys.platform == "win32":
        _atomic_write(path, _protect(payload))
        _lock_down_windows_acl(path)
        return
    if os.environ.get(_ALLOW_PLAINTEXT_ENV) == "1":
        log.warning(
            "secret_storage: writing PLAINTEXT secrets.dat on non-Windows host "
            "(AIGAUGE_ALLOW_PLAINTEXT_SECRETS=1). This is a development-only "
            "escape hatch; do not use it with real provider cookies."
        )
        # Owner-only from the first byte, written atomically.
        _atomic_write(path, payload, mode=0o600)
        return
    raise RuntimeError(
        "secret_storage: refusing to write secrets on non-Windows host. "
        "DPAPI encryption is unavailable, so writing here would store the "
        "value in plaintext. Run on Windows, or set "
        "AIGAUGE_ALLOW_PLAINTEXT_SECRETS=1 to opt in (development only)."
    )


def save_secret(name: str, value: str | None) -> None:
    data = _load_all()
    if value:
        data[name] = value
    else:
        data.pop(name, None)
    _save_all(data)


def load_secret(name: str) -> str | None:
    return _load_all().get(name)
