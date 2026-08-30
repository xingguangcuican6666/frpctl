import pytest

from frpctl.errors import FrpCtlError
from frpctl.models import Client, Inventory, Node, SSHConfig
from frpctl.tui_widgets import resolve_client


def inventory(*node_ids_per_client: list[str]) -> Inventory:
    clients = [
        Client(f"client-{index}", SSHConfig(f"10.0.0.{index}", "operator"), [], nodes)
        for index, nodes in enumerate(node_ids_per_client)
    ]
    known = {node for nodes in node_ids_per_client for node in nodes}
    return Inventory(
        clients=clients,
        nodes=[
            Node(node, SSHConfig("203.0.113.1", "root"), 17000 + index)
            for index, node in enumerate(sorted(known))
        ],
    )


def test_single_client_needs_no_preference():
    assert resolve_client(inventory(["node-a"]), "node-a", None).id == "client-0"


def test_unattached_node_is_reported():
    with pytest.raises(FrpCtlError, match="not attached to any client"):
        resolve_client(inventory(["node-a"]), "node-b", None)


def test_preference_breaks_a_tie():
    inv = inventory(["node-a"], ["node-a"])
    assert resolve_client(inv, "node-a", "client-1").id == "client-1"


def test_ambiguous_node_without_a_usable_preference_lists_the_candidates():
    inv = inventory(["node-a"], ["node-a"])
    with pytest.raises(FrpCtlError, match="client-0, client-1"):
        resolve_client(inv, "node-a", "client-9")
