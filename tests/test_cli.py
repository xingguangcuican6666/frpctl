from typer.testing import CliRunner

from frpctl.cli import app
from frpctl.inventory import InventoryStore
from frpctl.models import Client, Inventory, Mapping, Node, SSHConfig

runner = CliRunner()


def seed(tmp_path) -> InventoryStore:
    store = InventoryStore(tmp_path / "inventory.yaml", tmp_path / "secrets.yaml")
    inventory = Inventory(
        clients=[
            Client(
                "client-109",
                SSHConfig("192.168.1.109", "deploy"),
                [Mapping("ssh", "tcp", "127.0.0.1", 22, 10422)],
                ["node-123"],
            )
        ],
        nodes=[Node("node-123", SSHConfig("203.0.113.123", "root"), 17000)],
    )
    store.save(inventory, {"nodes": {"node-123": {"token": "secret"}}})
    return store


def edit(store, *args):
    return runner.invoke(
        app,
        ["node", "edit", *args, "--inventory", str(store.inventory_path)],
    )


def test_node_edit_changes_connection_info(tmp_path):
    store = seed(tmp_path)
    result = edit(
        store,
        "node-123",
        "--host",
        "203.0.113.200",
        "--user",
        "admin",
        "--ssh-port",
        "2222",
        "--frp-version",
        "0.71.0",
    )
    assert result.exit_code == 0
    node = store.load().node("node-123")
    assert (node.ssh.host, node.ssh.user, node.ssh.port) == (
        "203.0.113.200",
        "admin",
        2222,
    )
    assert node.frp_version == "0.71.0"


def test_node_edit_leaves_untouched_fields_alone(tmp_path):
    store = seed(tmp_path)
    result = edit(store, "node-123", "--user", "admin")
    assert result.exit_code == 0
    node = store.load().node("node-123")
    # Only the user changed; the host it was seeded with is preserved.
    assert node.ssh.user == "admin"
    assert node.ssh.host == "203.0.113.123"


def test_node_edit_sets_and_clears_the_ssh_proxy(tmp_path):
    store = seed(tmp_path)
    assert (
        edit(store, "node-123", "--ssh-proxy", "socks5h://127.0.0.1:1080").exit_code
        == 0
    )
    assert store.load().node("node-123").ssh.ssh_proxy == "socks5h://127.0.0.1:1080"
    # An empty string is the clear sentinel; the field goes back to None.
    assert edit(store, "node-123", "--ssh-proxy", "").exit_code == 0
    assert store.load().node("node-123").ssh.ssh_proxy is None


def test_node_edit_rejects_an_invalid_ssh_proxy(tmp_path):
    store = seed(tmp_path)
    result = edit(store, "node-123", "--ssh-proxy", "http://127.0.0.1:7890")
    assert result.exit_code == 1
    # Nothing is written when validation fails.
    assert store.load().node("node-123").ssh.ssh_proxy is None


def test_node_edit_unknown_node_fails(tmp_path):
    store = seed(tmp_path)
    result = edit(store, "ghost", "--host", "203.0.113.5")
    assert result.exit_code == 1
