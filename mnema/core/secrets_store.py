"""Kod dlya parolya vstrechi: pwd khranitsya kak pwd_ref, v logi ne pishetsya (6.2/HT-3).

v1 — prostoy reference-store v BDD-zavisimom fayle (ne git). Pri podklyuchenii
vault prosto menyayetsya etot modul'.

Zapis' atomarnaya (temp + os.replace) i pod flock: parallel'nye store/delete
(planirovschik + web API) nesut vse paroli vmeste — bez lokov last-writer-wins
teryal entries, a nesmyvshiysya write mog prevratit' fayl v pustoy JSON.
"""
from __future__ import annotations

import json
import os
import secrets
from contextlib import contextmanager
from pathlib import Path


def _store_path() -> Path:
    base = os.environ.get("DATA_DIR", "/data")
    p = Path(base) / "secrets"
    p.mkdir(parents=True, exist_ok=True)
    return p / "meeting_pwd.json"


@contextmanager
def _locked(path: Path):
    """Kross-processnyy advisory-lock nad sosednim .lock faylom."""
    import fcntl
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}  # pobytiy/pustoy fayl — nachaem s pustogo, ne padem


def _write_locked(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    # 0600 pri sozdanii (a ne chmod posle): okno chitaemosti parolen svoditsya k 0
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def store_pwd(job_key: str, pwd: str) -> str:
    ref = f"pwdref:{secrets.token_hex(8)}"
    path = _store_path()
    with _locked(path):
        data = _read(path)
        data[ref] = pwd
        _write_locked(path, data)
    return ref


def get_pwd(pwd_ref: str | None) -> str | None:
    if not pwd_ref:
        return None
    path = _store_path()
    if not path.exists():
        return None
    return _read(path).get(pwd_ref)


def delete_pwd(pwd_ref: str | None) -> None:
    if not pwd_ref:
        return
    path = _store_path()
    if not path.exists():
        return
    with _locked(path):
        data = _read(path)
        if pwd_ref in data:
            del data[pwd_ref]
            _write_locked(path, data)
