import pytest

from frpctl.errors import ValidationError
from frpctl.models import Client, Inventory, Mapping, Node, SSHConfig


def sample_inventory() -> Inventory:
    return Inventory(
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


def test_roundtrip_and_lookup():
    inv = sample_inventory()
    restored = Inventory.from_dict(inv.to_dict())
    assert restored.client("client-109").mappings[0].remote_port == 10422
    assert restored.node("node-123").tunnel_port == 17000


def test_next_tunnel_port():
    assert sample_inventory().next_tunnel_port() == 17001


def test_duplicate_remote_port_rejected():
    inv = sample_inventory()
    inv.clients[0].mappings.append(Mapping("ssh2", "tcp", "127.0.0.1", 2222, 10422))
    with pytest.raises(ValidationError):
        inv.validate()


def test_http_requires_domain():
    with pytest.raises(ValidationError):
        Mapping("web", "http", "127.0.0.1", 80).validate()


def test_proxy_survives_a_yaml_roundtrip_on_both_sides():
    inv = sample_inventory()
    inv.clients[0].ssh.http_proxy = "socks5h://127.0.0.1:1080"
    inv.nodes[0].ssh.http_proxy = "http://127.0.0.1:7890"
    restored = Inventory.from_dict(inv.to_dict())
    assert restored.client("client-109").ssh.http_proxy == "socks5h://127.0.0.1:1080"
    assert restored.node("node-123").ssh.http_proxy == "http://127.0.0.1:7890"


def test_proxy_without_a_scheme_is_rejected():
    inv = sample_inventory()
    inv.nodes[0].ssh.http_proxy = "127.0.0.1:7890"
    with pytest.raises(ValidationError, match="invalid node node-123 proxy"):
        inv.validate()


def test_proxy_with_whitespace_is_rejected():
    inv = sample_inventory()
    inv.clients[0].ssh.http_proxy = "http://127.0.0.1:7890 --insecure"
    with pytest.raises(ValidationError, match="invalid client client-109 proxy"):
        inv.validate()


def test_ssh_proxy_survives_a_yaml_roundtrip():
    inv = sample_inventory()
    inv.nodes[0].ssh.ssh_proxy = "socks5h://127.0.0.1:1080"
    restored = Inventory.from_dict(inv.to_dict())
    assert restored.node("node-123").ssh.ssh_proxy == "socks5h://127.0.0.1:1080"


def test_ssh_proxy_defaults_to_none():
    assert SSHConfig("h", "u").ssh_proxy is None
    restored = Inventory.from_dict(
        {
            "nodes": [
                {"id": "n", "ssh": {"host": "h", "user": "u"}, "tunnel_port": 17000}
            ]
        }
    )
    assert restored.node("n").ssh.ssh_proxy is None


def test_ssh_proxy_must_be_a_socks_url():
    inv = sample_inventory()
    inv.nodes[0].ssh.ssh_proxy = "http://127.0.0.1:7890"
    with pytest.raises(ValidationError, match="invalid node node-123 ssh proxy"):
        inv.validate()


def test_ssh_proxy_and_proxy_jump_are_mutually_exclusive():
    inv = sample_inventory()
    inv.nodes[0].ssh.ssh_proxy = "socks5h://127.0.0.1:1080"
    inv.nodes[0].ssh.proxy_jump = "bastion"
    with pytest.raises(ValidationError, match="only one of ssh_proxy or proxy_jump"):
        inv.validate()


def test_missing_proxy_key_defaults_to_none():
    restored = Inventory.from_dict(
        {
            "clients": [
                {"id": "c", "ssh": {"host": "h", "user": "u"}, "node_ids": []},
            ],
            "nodes": [],
        }
    )
    assert restored.client("c").ssh.http_proxy is None


def test_http_vhost_port_conflict_rejected():
    inv = sample_inventory()
    inv.clients[0].mappings.extend(
        [
            Mapping("port-80", "tcp", "127.0.0.1", 8080, 80),
            Mapping(
                "domain",
                "http",
                "127.0.0.1",
                8081,
                custom_domains=["example.test"],
            ),
        ]
    )
    with pytest.raises(ValidationError):
        inv.validate()
