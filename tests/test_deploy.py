import pytest

from frpctl.deploy import Deployer
from frpctl.errors import DeploymentError
from frpctl.models import Client, Inventory, Mapping, Node, SSHConfig
from frpctl.ssh import CommandResult


class FakeRemote:
    def __init__(self):
        self.writes = []
        self.commands = []

    def run(self, command, **kwargs):
        self.commands.append(command)
        if command == "uname -s":
            return CommandResult("Linux\n", "", 0)
        if command.startswith("command -v"):
            return CommandResult("/usr/bin/tool\n", "", 0)
        if command.startswith("dpkg --print-architecture"):
            return CommandResult("amd64\n", "", 0)
        if command == "ss -lntup":
            return CommandResult('tcp LISTEN 0 4096 *:22 *:* users:(("sshd"))\n', "", 0)
        if command == "ss -lnt":
            return CommandResult(
                "tcp LISTEN 0 511 0.0.0.0:80 0.0.0.0:*\ntcp LISTEN 0 128 *:22 *:*\n",
                "",
                0,
            )
        return CommandResult("", "", 0)

    def write_atomic(self, path, content, **kwargs):
        self.writes.append((path, content))


class PublicFakeRemote(FakeRemote):
    def run(self, command, **kwargs):
        if command == "ss -lntup":
            self.commands.append(command)
            return CommandResult(
                'tcp LISTEN 0 4096 0.0.0.0:10422 0.0.0.0:* users:(("frps"))\n',
                "",
                0,
            )
        return super().run(command, **kwargs)


class ProxyRemote(FakeRemote):
    """FakeRemote carrying an SSHConfig, which ``download_checks`` reads."""

    def __init__(self, http_proxy=None, *, download_code=0, curl_present=True):
        super().__init__()
        self.config = SSHConfig("host", "root", http_proxy=http_proxy)
        self.download_code = download_code
        self.curl_present = curl_present

    def run(self, command, **kwargs):
        if command.startswith("curl -fsS --max-time 25"):
            self.commands.append(command)
            if self.download_code:
                return CommandResult(
                    "", "curl: (28) Connection timed out", self.download_code
                )
            return CommandResult("", "", 0)
        if command == "command -v curl" and not self.curl_present:
            self.commands.append(command)
            return CommandResult("", "", 1)
        return super().run(command, **kwargs)


def test_download_check_probes_the_checksum_file_and_names_the_proxy():
    node = Node("n", SSHConfig("node", "root"), 17000)
    remote = ProxyRemote(http_proxy="http://127.0.0.1:7890")
    check = Deployer(Inventory()).download_checks(node, remote, "node")[0]
    assert check.name == "node-frp-download"
    assert check.ok
    assert check.detail == "reachable via http://127.0.0.1:7890"
    probe = remote.commands[-1]
    assert "/v0.70.0/frp_sha256_checksums.txt" in probe


def test_download_check_reports_curl_failure_with_a_remedy():
    node = Node("n", SSHConfig("node", "root"), 17000)
    remote = ProxyRemote(download_code=28)
    check = Deployer(Inventory()).download_checks(node, remote, "client")[0]
    assert check.name == "client-frp-download"
    assert not check.ok
    assert "curl exited 28" in check.detail
    assert "Connection timed out" in check.detail
    assert "frpctl proxy set <url> --client" in check.detail


def test_download_check_defers_when_curl_is_absent():
    node = Node("n", SSHConfig("node", "root"), 17000)
    remote = ProxyRemote(curl_present=False, download_code=28)
    check = Deployer(Inventory()).download_checks(node, remote, "node")[0]
    assert check.ok
    assert "curl missing" in check.detail


def test_unsupported_architecture_is_rejected():
    remote = FakeRemote()
    remote.run = lambda command, **kwargs: CommandResult("riscv64\n", "", 0)
    with pytest.raises(DeploymentError, match="riscv64"):
        Deployer(Inventory()).release_target(remote)


def test_preflight_and_stable_token():
    client = Client(
        "c", SSHConfig("client", "u"), [Mapping("ssh", "tcp", "127.0.0.1", 22, 10422)]
    )
    node = Node("n", SSHConfig("node", "u"), 17000)
    deployer = Deployer(Inventory(clients=[client], nodes=[node]))
    assert all(x.ok for x in deployer.checks(node, client, FakeRemote()))
    assert deployer.token_for("n") == deployer.token_for("n")


def test_legacy_sync_uses_existing_client_service():
    client = Client(
        "c",
        SSHConfig("client", "u"),
        [Mapping("ssh", "tcp", "127.0.0.1", 22, 10422)],
    )
    node = Node("n", SSHConfig("node", "u"), 17000, legacy_tunnel=True)
    deployer = Deployer(Inventory(clients=[client], nodes=[node]))
    server, local = PublicFakeRemote(), FakeRemote()
    deployer.sync_node(node, client, server, local)
    assert local.writes[0][0] == "/etc/frp/frpc.toml"
    assert any(
        "systemctl restart frpc.service" in command for command in local.commands
    )


def test_installer_keeps_release_filename_for_checksum():
    client = Client("c", SSHConfig("client", "u"))
    node = Node("n", SSHConfig("node", "root"), 17000)
    deployer = Deployer(Inventory(clients=[client], nodes=[node]))
    remote = FakeRemote()
    deployer.install_frp(node, remote)
    command = remote.commands[-1]
    assert "-o frp_0.70.0_linux_amd64.tar.gz" in command
    assert "tar -xzf frp_0.70.0_linux_amd64.tar.gz" in command


def test_client_listener_on_wildcard_counts_as_available():
    client = Client(
        "c",
        SSHConfig("client", "u"),
        [Mapping("web", "tcp", "127.0.0.1", 80, 80)],
    )
    node = Node("n", SSHConfig("node", "root"), 17001)
    checks = Deployer(Inventory()).client_checks(node, client, FakeRemote())
    targets = next(check for check in checks if check.name == "client-targets")
    assert targets.detail == "all TCP targets listening"


def test_public_listener_verification_rejects_loopback_only():
    client = Client(
        "c",
        SSHConfig("client", "u"),
        [Mapping("api", "tcp", "127.0.0.1", 3000, 13000)],
    )
    node = Node("n", SSHConfig("node", "root"), 17001)
    remote = FakeRemote()
    remote.run = lambda command, **kwargs: CommandResult(
        'tcp LISTEN 0 4096 127.0.0.1:13000 0.0.0.0:* users:(("frps"))\n',
        "",
        0,
    )
    checks = Deployer(Inventory()).verify_public_listeners(node, client, remote)
    assert checks[0].ok is False
