"""File-backed configuration editing with optimistic concurrency and atomic replacement."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import threading

from .validation import object_value, validate_accounts, validate_config


class ConflictError(ValueError):
    pass


def decode_json(raw: bytes):
    def reject_constant(value):
        raise ValueError(f"Invalid JSON number: {value}")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON field: {key}")
            result[key] = value
        return result

    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("JSON numbers must be finite.")
        return parsed

    return json.loads(raw, parse_constant=reject_constant, parse_float=finite_float, object_pairs_hook=unique_object)


class ConfigRepository:
    def __init__(self, config_path: str | Path):
        self.config_path = Path(config_path).expanduser().resolve()
        self._lock = threading.RLock()

    def _accounts_path(self, config: dict) -> Path:
        object_value(config, "Configuration")
        value = config.get("accounts_file") or config.get("trading_accounts_file")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Set accounts_file in the configuration to a trading accounts JSON file.")
        path = (self.config_path.parent / value.strip()).resolve()
        if path == self.config_path:
            raise ValueError("The accounts file must be different from the main configuration file.")
        return path

    @staticmethod
    def _revision(path: Path, raw: bytes) -> str:
        return hashlib.sha256(str(path).encode() + b"\0" + raw).hexdigest()

    def _document(self, path: Path) -> dict:
        raw = path.read_bytes()
        return {"path": str(path), "revision": self._revision(path, raw), "data": decode_json(raw)}

    def read(self) -> dict:
        with self._lock:
            config = self._document(self.config_path)
            accounts = self._document(self._accounts_path(config["data"]))
            return {"config": config, "accounts": accounts}

    def admin_password(self) -> str:
        config = object_value(decode_json(self.config_path.read_bytes()), "Configuration")
        auth = object_value(config.get("auth", {}), "auth")
        users = object_value(auth.get("users", {}), "auth.users")
        admin = object_value(users.get("admin", {}), "auth.users.admin")
        password = admin.get("password")
        if not isinstance(password, str) or not password.strip():
            raise ValueError("The main configuration must define auth.users.admin.password.")
        return password

    @contextmanager
    def _file_lock(self):
        # A stable sidecar lock also serializes independent configurator processes.
        path = self.config_path.with_name(self.config_path.name + ".configurator.lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    @staticmethod
    def _atomic_write(path: Path, raw: bytes, mode: int) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), mode)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def save(self, name: str, document, revision: str) -> dict:
        if name not in ("config", "accounts"):
            raise ValueError("Unknown configuration document.")
        encoded = (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
        with self._lock, self._file_lock():
            config = decode_json(self.config_path.read_bytes())
            path = self.config_path if name == "config" else self._accounts_path(config)
            original = path.read_bytes()
            if revision != self._revision(path, original):
                raise ConflictError("This file changed since you loaded it. Copy your edits, then reload before saving.")
            if name == "config":
                validate_config(document)
                validate_accounts(decode_json(self._accounts_path(document).read_bytes()))
            else:
                validate_accounts(document)
            # Preserve file permissions. Backups are private and contain the previous bytes.
            mode = stat.S_IMODE(path.stat().st_mode)
            backup = path.with_name(path.name + ".bak")
            self._atomic_write(backup, original, 0o600)
            self._atomic_write(path, encoded, mode)
            return {"document": self._document(path), "backup_path": str(backup)}
