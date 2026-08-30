from __future__ import annotations

import base64
import getpass
import shlex
from collections.abc import Iterable
from dataclasses import dataclass

from .errors import RemoteError
from .models import SSHConfig

# Both cases are exported: curl and apt read the lowercase names, while some
# tooling only looks at the uppercase ones.
PROXY_VARIABLES = ("http_proxy", "https_proxy", "all_proxy")
NO_PROXY = "localhost,127.0.0.1,::1"


@dataclass(slots=True)
class CommandResult:
    stdout: str
    stderr: str
    code: int

    @property
    def ok(self) -> bool:
        return self.code == 0


class SSHExecutor:
    """Small Paramiko wrapper; credentials are never retained in inventory."""

    def __init__(
        self,
        config: SSHConfig,
        password: str | None = None,
        sudo_password: str | None = None,
    ):
        self.config = config
        self.password = password
        self.sudo_password = sudo_password
        self.client = None
        self._proxy = None

    def connect(self) -> None:
        try:
            import paramiko
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RemoteError("paramiko is required for remote operations") from exc
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = {
            "hostname": self.config.host,
            "port": self.config.port,
            "username": self.config.user,
            "timeout": 15,
            "banner_timeout": 15,
            "auth_timeout": 15,
            "look_for_keys": not bool(self.password or self.config.key_file),
            "allow_agent": False,
        }
        if self.config.key_file:
            kwargs["key_filename"] = self.config.key_file
        if self.password:
            kwargs["password"] = self.password
        if self.config.proxy_jump:
            jump = self._parse_jump(self.config.proxy_jump)
            jump_client = paramiko.SSHClient()
            jump_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            jump_client.connect(
                jump["host"],
                port=jump["port"],
                username=jump["user"],
                look_for_keys=True,
            )
            self._proxy = jump_client.get_transport().open_channel(
                "direct-tcpip", (self.config.host, self.config.port), ("127.0.0.1", 0)
            )
            kwargs["sock"] = self._proxy
        try:
            self.client.connect(**kwargs)
        except Exception as exc:
            self.close()
            raise RemoteError(
                f"SSH connection to {self.config.user}@{self.config.host} failed: {exc}"
            ) from exc

    @staticmethod
    def _parse_jump(value: str) -> dict[str, object]:
        user_host, _, port_text = value.rpartition(":")
        if not port_text.isdigit():
            user_host, port_text = value, "22"
        if "@" in user_host:
            user, host = user_host.split("@", 1)
        else:
            user, host = getpass.getuser(), user_host
        return {"user": user, "host": host, "port": int(port_text)}

    def proxy_prefix(self) -> str:
        """Shell prologue exporting the target host's own outbound proxy.

        The exports are placed inside the command that ``sudo`` runs rather than
        in the caller's environment, so no ``env_keep`` entry in sudoers is
        needed and nothing has to be configured in the host's shell profile.
        """
        if not self.config.http_proxy:
            return ""
        proxy = shlex.quote(self.config.http_proxy)
        skip = shlex.quote(NO_PROXY)
        exports = [f"export {name}={proxy}" for name in PROXY_VARIABLES]
        exports += [f"export {name.upper()}={proxy}" for name in PROXY_VARIABLES]
        exports += [f"export no_proxy={skip}", f"export NO_PROXY={skip}"]
        return "; ".join(exports) + "; "

    def run(
        self,
        command: str,
        *,
        sudo: bool = False,
        check: bool = True,
        timeout: int = 60,
        proxy: bool = True,
    ) -> CommandResult:
        if self.client is None:
            self.connect()
        actual = (self.proxy_prefix() if proxy else "") + command
        if sudo:
            actual = (
                "sh -c " if self.config.user == "root" else "sudo -S -p '' sh -c "
            ) + shlex.quote(actual)
        try:
            stdin, stdout, stderr = self.client.exec_command(
                actual, timeout=timeout, get_pty=sudo
            )
            if sudo and self.config.user != "root" and self.sudo_password:
                stdin.write(self.sudo_password + "\n")
                stdin.flush()
            result = CommandResult(
                stdout.read().decode(errors="replace"),
                stderr.read().decode(errors="replace"),
                stdout.channel.recv_exit_status(),
            )
        except Exception as exc:
            raise RemoteError(
                f"remote command failed on {self.config.host}: {exc}"
            ) from exc
        if check and not result.ok:
            raise RemoteError(
                f"remote command exited {result.code}: {result.stderr.strip() or result.stdout.strip()}"
            )
        return result

    def write_atomic(
        self, path: str, content: str, *, mode: int = 0o600, sudo: bool = True
    ) -> None:
        encoded = base64.b64encode(content.encode()).decode()
        temp = f"{path}.tmp.$$.new"
        command = f"mkdir -p {shlex.quote(path.rsplit('/', 1)[0])}; echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(temp)}; chmod {mode:o} {shlex.quote(temp)}; mv -f {shlex.quote(temp)} {shlex.quote(path)}"
        self.run(command, sudo=sudo)

    def close(self) -> None:
        if self.client:
            self.client.close()
        if self._proxy:
            self._proxy.close()
        self.client = None

    def __enter__(self) -> SSHExecutor:
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def redact_args(args: Iterable[str]) -> list[str]:
    return ["<secret>" if i else value for i, value in enumerate(args)]
