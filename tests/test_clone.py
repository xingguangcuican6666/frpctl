import pytest

from frpctl.domains import (
    DomainBinding,
    DomainInventory,
    NodeDomains,
    clone_node_domains,
)
from frpctl.errors import ValidationError
from frpctl.models import Node, SSHConfig
from frpctl.service import cloned_node, cloned_ssh


def source_node() -> Node:
    return Node(
        "node-a",
        SSHConfig(
            "203.0.113.10",
            "ubuntu",
            2222,
            key_file="/root/.ssh/fleet",
            proxy_jump="bastion",
            http_proxy="http://127.0.0.1:7890",
        ),
        17000,
        frp_version="0.71.1",
        frps_bind_port=7100,
        vhost_http_port=8080,
        vhost_https_port=8443,
        port_overrides={"web": 10080},
        legacy_tunnel=True,
    )


def test_cloned_ssh_inherits_everything_but_the_host():
    clone = cloned_ssh(source_node().ssh, "198.51.100.20")
    assert clone.host == "198.51.100.20"
    assert clone.user == "ubuntu"
    assert clone.port == 2222
    assert clone.key_file == "/root/.ssh/fleet"
    assert clone.proxy_jump == "bastion"
    assert clone.http_proxy == "http://127.0.0.1:7890"


def test_cloned_ssh_overrides_only_what_is_given():
    clone = cloned_ssh(
        source_node().ssh, "198.51.100.20", user="root", port=None, http_proxy=None
    )
    assert clone.user == "root"
    # None means "keep the source value", so these are still inherited.
    assert clone.port == 2222
    assert clone.http_proxy == "http://127.0.0.1:7890"


def test_cloned_ssh_does_not_alias_the_source():
    source = source_node().ssh
    clone = cloned_ssh(source, "198.51.100.20")
    clone.http_proxy = None
    assert source.http_proxy == "http://127.0.0.1:7890"


def test_cloned_node_copies_every_frp_setting():
    source = source_node()
    ssh = cloned_ssh(source.ssh, "198.51.100.20")
    clone = cloned_node(source, "node-b", ssh, 17001)
    assert clone.id == "node-b"
    assert clone.tunnel_port == 17001
    assert clone.ssh.host == "198.51.100.20"
    assert clone.frp_version == "0.71.1"
    assert clone.frps_bind_port == 7100
    assert clone.vhost_http_port == 8080
    assert clone.vhost_https_port == 8443
    assert clone.port_overrides == {"web": 10080}


def test_cloned_node_always_uses_the_managed_tunnel_layout():
    source = source_node()
    assert source.legacy_tunnel is True
    clone = cloned_node(
        source, "node-b", cloned_ssh(source.ssh, "198.51.100.20"), 17001
    )
    assert clone.legacy_tunnel is False


def test_cloned_node_copies_port_overrides_by_value():
    source = source_node()
    clone = cloned_node(
        source, "node-b", cloned_ssh(source.ssh, "198.51.100.20"), 17001
    )
    clone.port_overrides["api"] = 13000
    assert source.port_overrides == {"web": 10080}


def domains_fixture() -> DomainInventory:
    return DomainInventory(
        nodes=[
            NodeDomains(
                "node-a",
                "admin@example.com",
                fallback_port=8080,
                bindings=[
                    DomainBinding("example.com", 80),
                    DomainBinding("api.example.com", 13000),
                ],
            )
        ]
    )


def test_clone_domains_copies_bindings_email_and_fallback():
    inventory = domains_fixture()
    target = clone_node_domains(inventory, "node-a", "node-b")
    assert target.node_id == "node-b"
    assert target.email == "admin@example.com"
    assert target.fallback_port == 8080
    assert [(x.hostname, x.upstream_port) for x in target.bindings] == [
        ("example.com", 80),
        ("api.example.com", 13000),
    ]


def test_clone_domains_copies_bindings_by_value():
    inventory = domains_fixture()
    target = clone_node_domains(inventory, "node-a", "node-b")
    target.bindings[0].upstream_port = 19999
    assert inventory.node("node-a").bindings[0].upstream_port == 80


def test_clone_domains_refuses_to_overwrite_by_default():
    inventory = domains_fixture()
    inventory.nodes.append(
        NodeDomains("node-b", bindings=[DomainBinding("keep.example.com", 9000)])
    )
    with pytest.raises(ValidationError, match="pass --overwrite"):
        clone_node_domains(inventory, "node-a", "node-b")
    assert [x.hostname for x in inventory.node("node-b").bindings] == [
        "keep.example.com"
    ]


def test_clone_domains_overwrite_replaces_the_target():
    inventory = domains_fixture()
    inventory.nodes.append(
        NodeDomains("node-b", bindings=[DomainBinding("keep.example.com", 9000)])
    )
    clone_node_domains(inventory, "node-a", "node-b", overwrite=True)
    assert [x.hostname for x in inventory.node("node-b").bindings] == [
        "example.com",
        "api.example.com",
    ]


def test_clone_domains_rejects_the_same_node():
    with pytest.raises(ValidationError, match="must differ"):
        clone_node_domains(domains_fixture(), "node-a", "node-a")


def test_clone_domains_rejects_an_empty_source():
    inventory = DomainInventory(nodes=[NodeDomains("node-a")])
    with pytest.raises(ValidationError, match="no domain bindings"):
        clone_node_domains(inventory, "node-a", "node-b")


def test_clone_domains_rejects_an_unknown_source():
    with pytest.raises(ValidationError, match="no domain configuration"):
        clone_node_domains(domains_fixture(), "node-z", "node-b")
