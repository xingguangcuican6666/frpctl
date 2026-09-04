from __future__ import annotations

from dataclasses import dataclass, replace

from .deploy import Check, Deployer
from .errors import FrpCtlError, ValidationError
from .inventory import InventoryStore
from .models import Client, Inventory, Node, SSHConfig
from .ssh import SSHExecutor


@dataclass(slots=True)
class Credentials:
    password: str | None = None
    sudo_password: str | None = None


def resolve_client(inventory: Inventory, node_id: str, preferred: str | None) -> Client:
    """Pick the client serving ``node_id``, using ``preferred`` to break ties."""
    clients = [x for x in inventory.clients if node_id in x.node_ids]
    if not clients:
        raise FrpCtlError(f"{node_id} is not attached to any client")
    if len(clients) == 1:
        return clients[0]
    match = next((x for x in clients if x.id == preferred), None)
    if match is None:
        names = ", ".join(x.id for x in clients)
        raise FrpCtlError(
            f"{node_id} serves several clients ({names}); name one explicitly"
        )
    return match


def cloned_ssh(source: SSHConfig, host: str, **overrides: object) -> SSHConfig:
    """Copy a host's SSH settings onto a new address.

    Every field is inherited; an override of ``None`` means "keep the source
    value", so callers pass only what actually differs. Clearing an inherited
    field is done on the result, which keeps the sentinel handling in one place.
    """
    given = {key: value for key, value in overrides.items() if value is not None}
    return replace(source, host=host, **given)


def cloned_node(source: Node, node_id: str, ssh: SSHConfig, tunnel_port: int) -> Node:
    """Build a twin of ``source`` that differs only in identity and address.

    The FRP-level settings are copied verbatim, including ``port_overrides``, so
    the twin publishes each mapping on the same effective port as the original.
    Secrets are never copied: the node token and the tunnel key are generated
    per node id during deployment, which is what keeps nodes able to fail
    independently.  ``legacy_tunnel`` is always cleared because a freshly
    deployed node uses the managed tunnel layout.
    """
    return replace(
        source,
        id=node_id,
        ssh=ssh,
        tunnel_port=tunnel_port,
        port_overrides=dict(source.port_overrides),
        legacy_tunnel=False,
    )


class Manager:
    def __init__(self, store: InventoryStore):
        self.store = store

    def add_node(
        self,
        node: Node,
        client_id: str,
        node_credentials: Credentials,
        client_credentials: Credentials,
        *,
        dry_run: bool = False,
    ) -> list[Check]:
        with self.store.lock():
            inventory = self.store.load()
            secrets_data = self.store.load_secrets()
            if any(x.id == node.id for x in inventory.nodes):
                raise ValidationError(f"node already exists: {node.id}")
            if any(x.tunnel_port == node.tunnel_port for x in inventory.nodes):
                raise ValidationError(f"tunnel port already used: {node.tunnel_port}")
            client = inventory.client(client_id)
            deployer = Deployer(inventory, secrets_data)
            with SSHExecutor(
                node.ssh, node_credentials.password, node_credentials.sudo_password
            ) as node_remote:
                client_remote = SSHExecutor(
                    client.ssh,
                    client_credentials.password,
                    client_credentials.sudo_password,
                )
                try:
                    client_remote.connect()
                    checks = (
                        deployer.checks(node, client, node_remote)
                        + deployer.client_checks(node, client, client_remote)
                        + deployer.download_checks(node, node_remote, "node")
                        + deployer.download_checks(node, client_remote, "client")
                    )
                    failed = [x for x in checks if not x.ok]
                    if failed:
                        raise ValidationError(
                            "preflight failed: "
                            + "; ".join(f"{x.name}: {x.detail}" for x in failed)
                        )
                    if not dry_run:
                        deployer.deploy_node(node, client, node_remote)
                        deployer.install_client(
                            node, client, client_remote, node_remote
                        )
                        listener_checks = deployer.verify_public_listeners(
                            node, client, node_remote
                        )
                        failed_listeners = [
                            check for check in listener_checks if not check.ok
                        ]
                        if failed_listeners:
                            raise ValidationError(
                                "public listener verification failed: "
                                + "; ".join(
                                    f"{check.name}: {check.detail}"
                                    for check in failed_listeners
                                )
                            )
                        checks.extend(listener_checks)
                except Exception:
                    if not dry_run:
                        deployer.rollback_new_node(
                            node,
                            node_remote,
                            client_remote if client_remote.client else None,
                        )
                    raise
                finally:
                    client_remote.close()
            if not dry_run:
                inventory.nodes.append(node)
                client.node_ids.append(node.id)
                inventory.validate()
                self.store.save(inventory, deployer.secrets)
            return checks

    def remove_node(self, node_id: str) -> None:
        """Remove a node from desired state only; remote cleanup is intentionally explicit."""
        with self.store.lock():
            inventory = self.store.load()
            inventory.node(node_id)
            inventory.nodes = [x for x in inventory.nodes if x.id != node_id]
            for client in inventory.clients:
                client.node_ids = [x for x in client.node_ids if x != node_id]
            secrets_data = self.store.load_secrets()
            secrets_data.get("nodes", {}).pop(node_id, None)
            self.store.save(inventory, secrets_data)

    def sync_node(
        self,
        node_id: str,
        client_id: str,
        node_credentials: Credentials,
        client_credentials: Credentials,
        *,
        dry_run: bool = False,
    ) -> list[Check]:
        with self.store.lock():
            inventory = self.store.load()
            secrets_data = self.store.load_secrets()
            node, client = inventory.node(node_id), inventory.client(client_id)
            deployer = Deployer(inventory, secrets_data)
            with SSHExecutor(
                node.ssh, node_credentials.password, node_credentials.sudo_password
            ) as node_remote:
                with SSHExecutor(
                    client.ssh,
                    client_credentials.password,
                    client_credentials.sudo_password,
                ) as client_remote:
                    checks = deployer.sync_node(
                        node, client, node_remote, client_remote, dry_run=dry_run
                    )
            if not dry_run:
                self.store.save(inventory, deployer.secrets)
            return checks
