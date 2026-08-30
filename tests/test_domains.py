import os

from frpctl.domains import (
    DomainBinding,
    DomainInventory,
    DomainManager,
    DomainStore,
    NodeDomains,
    relocate_listener_port,
    render_nginx,
)
from frpctl.frp import render_frpc, render_frps
from frpctl.models import Client, Inventory, Mapping, Node, SSHConfig
from frpctl.ssh import CommandResult


def inventory_fixture():
    client = Client(
        "client-109",
        SSHConfig("192.168.1.109", "user"),
        [
            Mapping("web", "tcp", "127.0.0.1", 80, 80),
            Mapping("new-api", "tcp", "127.0.0.1", 3000, 13000),
        ],
        ["node-123", "node-139"],
    )
    old = Node("node-123", SSHConfig("203.0.113.123", "root"), 17000)
    new = Node("node-139", SSHConfig("198.51.100.20", "root"), 17001)
    return Inventory(clients=[client], nodes=[old, new]), client, old, new


def test_relocation_is_node_specific():
    inventory, client, old, new = inventory_fixture()
    assert relocate_listener_port(inventory, new, client) == 10080
    assert new.port_overrides == {"web": 10080}
    assert old.port_overrides == {}
    assert "remotePort = 10080" in render_frpc(new, client, "token")
    assert "remotePort = 80" in render_frpc(old, client, "token")
    assert "{ single = 10080 }" in render_frps(new, client, "token")


def test_nginx_http_and_https_routes():
    inventory, client, _, node = inventory_fixture()
    relocate_listener_port(inventory, node, client)
    domains = NodeDomains(
        "node-139",
        "admin@example.com",
        bindings=[
            DomainBinding("example.com", 80),
            DomainBinding("api.example.com", 13000),
        ],
    )
    initial = render_nginx(node, client, domains, tls=False)
    final = render_nginx(node, client, domains, tls=True)
    assert "listen 80 default_server" in initial
    assert "proxy_pass http://127.0.0.1:10080" in initial
    assert "proxy_pass http://127.0.0.1:13000" in initial
    assert "return 301 https://$host$request_uri" in final
    assert "/etc/letsencrypt/live/frpdomain-node-139/fullchain.pem" in final
    assert "listen 443 ssl http2" in final


def test_domain_store_permissions(tmp_path):
    path = tmp_path / "domains.yaml"
    store = DomainStore(path)
    store.save(
        DomainInventory(
            nodes=[
                NodeDomains(
                    "node-139",
                    bindings=[DomainBinding("api.example.com", 13000)],
                )
            ]
        )
    )
    assert store.load().node("node-139").bindings[0].hostname == "api.example.com"
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_nginx_cleanup_releases_port_80_without_restoring_default_site():
    class Remote:
        command = ""

        def run(self, command, **kwargs):
            self.command = command
            return CommandResult("", "", 0)

    remote = Remote()
    node = Node("node-139", SSHConfig("198.51.100.20", "root"), 17001)
    DomainManager._remove_nginx_site(remote, node)
    assert "systemctl stop nginx" in remote.command
    assert "rm -f /etc/nginx/sites-enabled/default" in remote.command
    assert (
        "mv /etc/nginx/sites-enabled/default.frpdomain-disabled" not in remote.command
    )
