import os

import pytest

from frpctl.errors import FrpCtlError
from frpctl.inventory import InventoryStore
from frpctl.models import Inventory


def test_atomic_save_permissions(tmp_path):
    store = InventoryStore(tmp_path / "inventory.yaml", tmp_path / "secrets.yaml")
    store.save(Inventory(), {"nodes": {}})
    assert store.load().version == 1
    assert store.load_secrets() == {"nodes": {}}
    assert os.stat(store.inventory_path).st_mode & 0o777 == 0o600
    assert os.stat(store.secrets_path).st_mode & 0o777 == 0o600


def test_unreadable_inventory_is_not_treated_as_missing(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    inventory = locked / "inventory.yaml"
    inventory.write_text("version: 1\n", encoding="utf-8")
    locked.chmod(0)
    try:
        with pytest.raises(FrpCtlError, match="run frpctl with sudo"):
            InventoryStore(inventory).load()
    finally:
        locked.chmod(0o700)
