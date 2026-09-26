import shlex

import pytest

from frpctl.models import SSHConfig
from frpctl.ssh import CommandResult, SSHExecutor


class FakeChannel:
    def recv_exit_status(self):
        return 0


class FakeStream:
    channel = FakeChannel()

    def read(self):
        return b""

    def write(self, _):
        return None

    def flush(self):
        return None


class FakeClient:
    def __init__(self):
        self.commands = []

    def exec_command(self, command, **kwargs):
        self.commands.append(command)
        return FakeStream(), FakeStream(), FakeStream()

    def close(self):
        return None


def executor(user: str = "root", **kwargs) -> tuple[SSHExecutor, FakeClient]:
    remote = SSHExecutor(SSHConfig("host", user, **kwargs))
    remote.client = FakeClient()
    return remote, remote.client


def test_no_proxy_configured_leaves_the_command_untouched():
    remote, client = executor()
    remote.run("curl https://example.test")
    assert client.commands == ["curl https://example.test"]


def test_proxy_is_exported_before_the_command():
    remote, client = executor(http_proxy="http://127.0.0.1:7890")
    remote.run("curl https://example.test")
    command = client.commands[0]
    assert command.startswith("export http_proxy=http://127.0.0.1:7890;")
    for name in ("https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        assert f"export {name}=http://127.0.0.1:7890" in command
    assert "export no_proxy=localhost,127.0.0.1,::1" in command
    assert command.endswith("curl https://example.test")


def test_proxy_lands_inside_the_shell_that_sudo_runs():
    """sudo resets the environment, so the exports must be in the inner shell."""
    remote, client = executor(user="ubuntu", http_proxy="http://127.0.0.1:7890")
    remote.run("apt-get update", sudo=True)
    command = client.commands[0]
    assert command.startswith("sudo -S -p '' sh -c ")
    quoted = command.removeprefix("sudo -S -p '' sh -c ")
    assert quoted.startswith("'export http_proxy=")
    assert quoted.endswith("apt-get update'")


def test_root_sudo_path_also_carries_the_proxy():
    remote, client = executor(http_proxy="http://127.0.0.1:7890")
    remote.run("apt-get update", sudo=True)
    assert client.commands[0].startswith("sh -c 'export http_proxy=")


def test_proxy_can_be_suppressed_for_one_command():
    remote, client = executor(http_proxy="http://127.0.0.1:7890")
    remote.run("curl http://example.test/probe", proxy=False)
    assert client.commands == ["curl http://example.test/probe"]


def test_proxy_url_is_shell_quoted():
    url = "http://user:p'a$s@127.0.0.1:7890"
    remote, client = executor(http_proxy=url)
    remote.run("true")
    # A POSIX lexer must recover the URL verbatim: no word splitting, no
    # expansion of the quote or the dollar sign.
    assert f"http_proxy={url};" in shlex.split(client.commands[0])


def test_command_result_ok():
    assert CommandResult("", "", 0).ok
    assert not CommandResult("", "boom", 7).ok


@pytest.mark.parametrize(
    "scheme,expected_type,expected_rdns",
    [
        ("socks4", "SOCKS4", False),
        ("socks4a", "SOCKS4", True),
        ("socks5", "SOCKS5", False),
        ("socks5h", "SOCKS5", True),
    ],
)
def test_socks_scheme_maps_to_pysocks(
    monkeypatch, scheme, expected_type, expected_rdns
):
    import socks

    captured = {}

    def fake_create_connection(dest, **kwargs):
        captured["dest"] = dest
        captured.update(kwargs)
        return "SOCKSOCK"

    monkeypatch.setattr(socks, "create_connection", fake_create_connection)
    remote, _ = executor(ssh_proxy=f"{scheme}://user:pw@10.0.0.5:1080")
    remote.config.host = "node.internal"
    remote.config.port = 2222

    assert remote._socks_socket() == "SOCKSOCK"
    assert captured["dest"] == ("node.internal", 2222)
    assert captured["proxy_addr"] == "10.0.0.5"
    assert captured["proxy_port"] == 1080
    assert captured["proxy_rdns"] is expected_rdns
    assert captured["proxy_username"] == "user"
    assert captured["proxy_password"] == "pw"
    assert captured["proxy_type"] == getattr(socks, expected_type)


def test_ssh_proxy_is_handed_to_paramiko_as_the_socket(monkeypatch):
    import paramiko
    import socks

    monkeypatch.setattr(socks, "create_connection", lambda dest, **kwargs: "SOCKSOCK")

    class FakeParamiko:
        def __init__(self):
            self.kwargs = None

        def set_missing_host_key_policy(self, _policy):
            return None

        def connect(self, **kwargs):
            self.kwargs = kwargs

        def close(self):
            return None

    monkeypatch.setattr(paramiko, "SSHClient", FakeParamiko)
    remote = SSHExecutor(
        SSHConfig("node.internal", "root", 22, ssh_proxy="socks5h://127.0.0.1:1080")
    )
    remote.connect()
    assert remote.client.kwargs["sock"] == "SOCKSOCK"


def test_ssh_proxy_takes_precedence_over_a_jump_host(monkeypatch):
    """An SSH SOCKS proxy owns the socket; the jump path must not also run."""
    import paramiko
    import socks

    monkeypatch.setattr(socks, "create_connection", lambda dest, **kwargs: "SOCKSOCK")

    class FakeParamiko:
        def set_missing_host_key_policy(self, _policy):
            return None

        def connect(self, **kwargs):
            self.kwargs = kwargs

        def close(self):
            return None

    monkeypatch.setattr(paramiko, "SSHClient", FakeParamiko)
    remote = SSHExecutor(
        SSHConfig(
            "node.internal",
            "root",
            ssh_proxy="socks5h://127.0.0.1:1080",
            proxy_jump="bastion",
        )
    )
    remote.connect()
    assert remote.client.kwargs["sock"] == "SOCKSOCK"
