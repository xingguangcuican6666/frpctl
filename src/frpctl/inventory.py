from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import yaml

from .errors import FrpCtlError
from .models import Inventory


class InventoryStore:
    def __init__(
        self,
        inventory_path: str | Path = "/etc/frp-manager/inventory.yaml",
        secrets_path: str | Path = "/etc/frp-manager/secrets.yaml",
    ) -> None:
        self.inventory_path = Path(inventory_path)
        self.secrets_path = Path(secrets_path)

    def load(self) -> Inventory:
        try:
            with self.inventory_path.open("r", encoding="utf-8") as handle:
                return Inventory.from_dict(yaml.safe_load(handle))
        except FileNotFoundError:
            return Inventory()
        except PermissionError as exc:
            raise FrpCtlError(
                f"cannot read {self.inventory_path}; run frpctl with sudo or choose a readable --inventory path"
            ) from exc

    def load_secrets(self) -> dict:
        try:
            with self.secrets_path.open("r", encoding="utf-8") as handle:
                return yaml.safe_load(handle) or {"nodes": {}}
        except FileNotFoundError:
            return {"nodes": {}}
        except PermissionError as exc:
            raise FrpCtlError(
                f"cannot read {self.secrets_path}; run frpctl with sudo or choose a readable --inventory path"
            ) from exc

    def save(self, inventory: Inventory, secrets: dict | None = None) -> None:
        inventory.validate()
        self._atomic_yaml(self.inventory_path, inventory.to_dict())
        if secrets is not None:
            self._atomic_yaml(self.secrets_path, secrets)

    @staticmethod
    def _atomic_yaml(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                yaml.safe_dump(value, handle, allow_unicode=True, sort_keys=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    @contextmanager
    def lock(self) -> Iterator[None]:
        import fcntl

        lock_path = self.inventory_path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
