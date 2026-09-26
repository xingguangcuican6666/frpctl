import asyncio

from textual.widgets import DataTable, Input, TabbedContent

from frpctl.inventory import InventoryStore
from frpctl.models import Client, Inventory, Mapping, Node, SSHConfig
from frpctl.tui import ConfirmScreen, FrpCtlApp, NodeEditScreen, ProxyScreen


def seed(tmp_path):
    store = InventoryStore(tmp_path / "inventory.yaml", tmp_path / "secrets.yaml")
    inventory = Inventory(
        clients=[
            Client(
                "client-109",
                SSHConfig("192.168.1.109", "operator"),
                [
                    Mapping("web", "tcp", "127.0.0.1", 3000, 13000),
                    Mapping(
                        "api",
                        "http",
                        "127.0.0.1",
                        8080,
                        None,
                        ["api.example.com"],
                        ["/v1"],
                        {"transport.useCompression": True},
                    ),
                ],
                ["node-a"],
            )
        ],
        nodes=[Node("node-a", SSHConfig("203.0.113.10", "root"), 17000)],
    )
    store.save(inventory, {"nodes": {"node-a": {"token": "secret"}}})
    return store


def drive(tmp_path, scenario, prepare=None):
    """Run one Pilot scenario against a freshly seeded inventory."""
    store = seed(tmp_path)
    if prepare is not None:
        prepare(store)

    async def main():
        app = FrpCtlApp(store.inventory_path)
        async with app.run_test() as pilot:
            await scenario(app, pilot)

    asyncio.run(main())
    return store


def test_overview_lists_nodes_and_mappings(tmp_path):
    async def scenario(app, pilot):
        del pilot
        nodes = app.query_one("#nodes", DataTable)
        mappings = app.query_one("#mappings", DataTable)
        assert nodes.row_count == 1
        assert mappings.row_count == 2
        assert nodes.get_row("node-a")[5] == "client-109"
        assert app.focused is nodes

    drive(tmp_path, scenario)


def test_delete_key_removes_the_highlighted_node(tmp_path):
    async def scenario(app, pilot):
        await pilot.press("d")
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await pilot.pause()
        assert app.query_one("#nodes", DataTable).row_count == 0

    store = drive(tmp_path, scenario)
    assert store.load().nodes == []
    assert store.load().clients[0].node_ids == []
    assert store.load_secrets()["nodes"] == {}


def test_escape_cancels_the_deletion(tmp_path):
    async def scenario(app, pilot):
        await pilot.press("d")
        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one("#nodes", DataTable).row_count == 1

    store = drive(tmp_path, scenario)
    assert [x.id for x in store.load().nodes] == ["node-a"]


def test_delete_key_removes_the_highlighted_mapping(tmp_path):
    async def scenario(app, pilot):
        mappings = app.query_one("#mappings", DataTable)
        mappings.focus()
        mappings.move_cursor(row=1)
        await pilot.pause()
        await pilot.press("d")
        await pilot.press("y")
        await pilot.pause()
        assert mappings.row_count == 1

    store = drive(tmp_path, scenario)
    assert [x.id for x in store.load().clients[0].mappings] == ["web"]


def test_edit_key_prefills_the_mapping_form(tmp_path):
    async def scenario(app, pilot):
        mappings = app.query_one("#mappings", DataTable)
        mappings.focus()
        mappings.move_cursor(row=1)
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert app.query_one("#tabs", TabbedContent).active == "mapping"
        assert app.query_one("#map-id", Input).value == "api"
        assert app.query_one("#map-protocol", Input).value == "http"
        assert app.query_one("#map-local-port", Input).value == "8080"
        assert app.query_one("#map-remote-port", Input).value == ""
        assert app.query_one("#map-domains", Input).value == "api.example.com"

    drive(tmp_path, scenario)


def test_saving_an_edited_mapping_keeps_imported_extras(tmp_path):
    async def scenario(app, pilot):
        mappings = app.query_one("#mappings", DataTable)
        mappings.focus()
        mappings.move_cursor(row=1)
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        app.query_one("#map-local-port", Input).value = "9090"
        app.save_mapping()
        await pilot.pause()

    store = drive(tmp_path, scenario)
    edited = next(x for x in store.load().clients[0].mappings if x.id == "api")
    assert edited.local_port == 9090
    assert edited.locations == ["/v1"]
    assert edited.extra == {"transport.useCompression": True}


def test_adding_a_duplicate_mapping_is_rejected(tmp_path):
    async def scenario(app, pilot):
        app.query_one("#map-id", Input).value = "web"
        app.query_one("#map-local-port", Input).value = "3000"
        app.query_one("#map-remote-port", Input).value = "13000"
        app.add_mapping()
        await pilot.pause()

    store = drive(tmp_path, scenario)
    assert len(store.load().clients[0].mappings) == 2


def test_proxy_key_sets_the_node_proxy(tmp_path):
    async def scenario(app, pilot):
        await pilot.press("p")
        assert isinstance(app.screen, ProxyScreen)
        app.screen.query_one("#proxy-url", Input).value = "http://127.0.0.1:7890"
        await pilot.press("enter")
        await pilot.pause()
        row = app.query_one("#nodes", DataTable).get_row("node-a")
        assert row[6] == "http://127.0.0.1:7890"

    store = drive(tmp_path, scenario)
    assert store.load().node("node-a").ssh.http_proxy == "http://127.0.0.1:7890"
    assert store.load().client("client-109").ssh.http_proxy is None


def test_proxy_key_on_a_mapping_row_targets_that_client(tmp_path):
    async def scenario(app, pilot):
        app.query_one("#mappings", DataTable).focus()
        await pilot.pause()
        await pilot.press("p")
        app.screen.query_one("#proxy-url", Input).value = "socks5h://127.0.0.1:1080"
        await pilot.press("enter")
        await pilot.pause()

    store = drive(tmp_path, scenario)
    assert (
        store.load().client("client-109").ssh.http_proxy == "socks5h://127.0.0.1:1080"
    )
    assert store.load().node("node-a").ssh.http_proxy is None


def test_clearing_the_proxy_stores_none_not_an_empty_string(tmp_path):
    def prepare(store):
        inventory = store.load()
        inventory.node("node-a").ssh.http_proxy = "http://127.0.0.1:7890"
        store.save(inventory)

    async def scenario(app, pilot):
        await pilot.press("p")
        assert (
            app.screen.query_one("#proxy-url", Input).value == "http://127.0.0.1:7890"
        )
        app.screen.query_one("#proxy-url", Input).value = ""
        await pilot.press("enter")
        await pilot.pause()

    store = drive(tmp_path, scenario, prepare)
    assert store.load().node("node-a").ssh.http_proxy is None


def test_an_invalid_proxy_is_rejected_and_not_saved(tmp_path):
    async def scenario(app, pilot):
        await pilot.press("p")
        app.screen.query_one("#proxy-url", Input).value = "127.0.0.1:7890"
        await pilot.press("enter")
        await pilot.pause()

    store = drive(tmp_path, scenario)
    assert store.load().node("node-a").ssh.http_proxy is None


def test_clone_key_prefills_the_add_node_form_from_the_source(tmp_path):
    def prepare(store):
        inventory = store.load()
        inventory.node("node-a").ssh.port = 2222
        inventory.node("node-a").ssh.http_proxy = "http://10.0.0.8:3128"
        inventory.node("node-a").ssh.ssh_proxy = "socks5h://127.0.0.1:1080"
        store.save(inventory)

    async def scenario(app, pilot):
        await pilot.press("c")
        await pilot.pause()
        assert app.query_one("#tabs", TabbedContent).active == "add-node"
        assert app.clone_from == "node-a"
        # Identity and address must be typed; everything else is inherited.
        assert app.query_one("#node-id", Input).value == ""
        assert app.query_one("#node-host", Input).value == ""
        assert app.query_one("#node-user", Input).value == "root"
        assert app.query_one("#node-port", Input).value == "2222"
        assert app.query_one("#node-proxy", Input).value == "http://10.0.0.8:3128"
        assert (
            app.query_one("#node-ssh-proxy", Input).value == "socks5h://127.0.0.1:1080"
        )
        assert app.query_one("#client-id", Input).value == "client-109"

    drive(tmp_path, scenario, prepare)


def test_reset_button_forgets_the_clone_source(tmp_path):
    async def scenario(app, pilot):
        await pilot.press("c")
        await pilot.pause()
        assert app.clone_from == "node-a"
        app.query_one("#node-ssh-proxy", Input).value = "socks5h://127.0.0.1:1080"
        app.reset_node_form()
        await pilot.pause()
        assert app.clone_from is None
        assert app.query_one("#node-user", Input).value == ""
        assert app.query_one("#node-port", Input).value == "22"
        assert app.query_one("#node-ssh-proxy", Input).value == ""

    drive(tmp_path, scenario)


def test_nodes_table_shows_the_ssh_proxy(tmp_path):
    def prepare(store):
        inventory = store.load()
        inventory.node("node-a").ssh.ssh_proxy = "socks5h://127.0.0.1:1080"
        store.save(inventory)

    async def scenario(app, pilot):
        del pilot
        row = app.query_one("#nodes", DataTable).get_row("node-a")
        assert row[7] == "socks5h://127.0.0.1:1080"

    drive(tmp_path, scenario, prepare)


def test_clone_needs_a_node_row(tmp_path):
    async def scenario(app, pilot):
        app.query_one("#mappings", DataTable).focus()
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        assert app.clone_from is None
        assert app.query_one("#tabs", TabbedContent).active == "overview"

    drive(tmp_path, scenario)


def test_sync_needs_a_node_row(tmp_path):
    async def scenario(app, pilot):
        app.query_one("#mappings", DataTable).focus()
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        # The mappings table cannot identify a node, so no modal is pushed.
        assert app.screen is app.query_one("#nodes", DataTable).screen

    drive(tmp_path, scenario)


def test_edit_key_on_a_node_opens_the_editor_prefilled(tmp_path):
    async def scenario(app, pilot):
        await pilot.press("e")
        assert isinstance(app.screen, NodeEditScreen)
        assert app.screen.query_one("#edit-host", Input).value == "203.0.113.10"
        assert app.screen.query_one("#edit-user", Input).value == "root"
        # The SSH port, not the tunnel port, prefills this field.
        assert app.screen.query_one("#edit-port", Input).value == "22"
        assert app.screen.query_one("#edit-frp-version", Input).value == "0.70.0"

    drive(tmp_path, scenario)


def test_saving_the_node_editor_updates_the_node(tmp_path):
    async def scenario(app, pilot):
        await pilot.press("e")
        app.screen.query_one("#edit-host", Input).value = "203.0.113.99"
        app.screen.query_one("#edit-user", Input).value = "admin"
        app.screen.query_one(
            "#edit-ssh-proxy", Input
        ).value = "socks5h://127.0.0.1:1080"
        app.screen.save()
        await pilot.pause()

    store = drive(tmp_path, scenario)
    node = store.load().node("node-a")
    assert node.ssh.host == "203.0.113.99"
    assert node.ssh.user == "admin"
    assert node.ssh.ssh_proxy == "socks5h://127.0.0.1:1080"


def test_clearing_the_ssh_proxy_in_the_editor_stores_none(tmp_path):
    def prepare(store):
        inventory = store.load()
        inventory.node("node-a").ssh.ssh_proxy = "socks5h://127.0.0.1:1080"
        store.save(inventory)

    async def scenario(app, pilot):
        await pilot.press("e")
        assert (
            app.screen.query_one("#edit-ssh-proxy", Input).value
            == "socks5h://127.0.0.1:1080"
        )
        app.screen.query_one("#edit-ssh-proxy", Input).value = ""
        app.screen.save()
        await pilot.pause()

    store = drive(tmp_path, scenario, prepare)
    assert store.load().node("node-a").ssh.ssh_proxy is None


def test_the_node_editor_rejects_an_invalid_ssh_proxy(tmp_path):
    async def scenario(app, pilot):
        await pilot.press("e")
        app.screen.query_one("#edit-ssh-proxy", Input).value = "http://127.0.0.1:7890"
        app.screen.save()
        await pilot.pause()

    store = drive(tmp_path, scenario)
    assert store.load().node("node-a").ssh.ssh_proxy is None


def test_edit_key_on_a_mapping_still_prefills_the_mapping_form(tmp_path):
    async def scenario(app, pilot):
        mappings = app.query_one("#mappings", DataTable)
        mappings.focus()
        mappings.move_cursor(row=1)
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert app.query_one("#tabs", TabbedContent).active == "mapping"
        assert app.query_one("#map-id", Input).value == "api"

    drive(tmp_path, scenario)
