"""One same-directory temp file plus ``os.replace``, shared by everything here
that writes a document to app data.

It started as ``secret_storage._atomic_write`` for ``secrets.dat``, the meter
catalog took it next, and ``config.json`` is the third - so it lives here now
rather than in the Windows credential module, whose import pulls in
``ctypes.wintypes`` and ``subprocess``. ``secret_storage`` cannot be imported
from ``config`` in any case: it imports ``config`` itself.

No dependency of its own beyond the standard library, and nothing in it knows
where app data is.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write(
    path: Path,
    payload: bytes,
    *,
    mode: int | None = None,
    prefix: str = ".aigauge-",
) -> None:
    """Write ``payload`` to ``path`` atomically via a same-dir temp + os.replace.

    A crash or concurrent read can never observe a half-written file: readers
    see either the old document or the complete new one. When ``mode`` is given
    the temp file is created with it before any bytes are written, so the
    payload is never briefly world-readable.

    ``prefix`` names the temp file, so a leftover says which caller left it.

    Raises ``OSError`` for anything the filesystem refuses, and leaves no temp
    file behind on any failure path. On Windows ``os.replace`` over a file
    another process holds open raises ``PermissionError``, which is an
    ``OSError`` and so is part of the same contract.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=prefix, suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        # Own the fd through the with-block so it is closed exactly once on every
        # path. fchmod on the open descriptor (rather than chmod on the name
        # before fdopen) avoids leaking the fd if setting the mode fails.
        with os.fdopen(fd, "wb") as handle:
            if mode is not None:
                # Windows has no os.fchmod. The Windows secrets path asks for
                # no mode, so this was unreachable there - but the guard
                # belongs with the call, not in every caller: the first one to
                # forget got an AttributeError instead of a file.
                if hasattr(os, "fchmod"):
                    os.fchmod(handle.fileno(), mode)
                else:
                    os.chmod(tmp, mode)
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
