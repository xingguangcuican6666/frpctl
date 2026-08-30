from frpctl.frp import import_frpc_text, redact_toml, render_frpc, render_frps
from frpctl.models import Client, Mapping, Node, SSHConfig


def fixtures():
    client = Client(
        "client-109",
        SSHConfig("192.168.1.109", "user"),
        [
            Mapping("web", "tcp", "127.0.0.1", 80, 10080),
            Mapping(
                "site", "http", "127.0.0.1", 8080, custom_domains=["a.example.com"]
            ),
        ],
    )
    node = Node("node-139", SSHConfig("198.51.100.20", "ubuntu"), 17001)
    return client, node


def test_render_configs():
    client, node = fixtures()
    server = render_frps(node, client, "sensitive")
    client_text = render_frpc(node, client, "sensitive")
    assert 'bindAddr = "127.0.0.1"' in server
    assert 'proxyBindAddr = "0.0.0.0"' in server
    assert "{ single = 10080 }" in server
    assert "vhostHTTPPort = 80" in server
    assert "serverPort = 17001" in client_text
    assert 'customDomains = ["a.example.com"]' in client_text


def test_import_current_shape():
    source = """
serverAddr = "127.0.0.1"
serverPort = 17000
auth.token = "secret"
[[proxies]]
name = "ssh"
type = "tcp"
localIP = "127.0.0.1"
localPort = 22
remotePort = 10422
"""
    metadata, mappings = import_frpc_text(source)
    assert metadata["token"] == "secret"
    assert mappings[0].id == "ssh"
    assert mappings[0].remote_port == 10422


def test_redaction():
    assert "sensitive" not in redact_toml('auth.token = "sensitive"\n')
