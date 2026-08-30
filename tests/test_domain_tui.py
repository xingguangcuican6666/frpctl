import asyncio

from textual.widgets import DataTable, Input, Static, TabbedContent

from frpctl.domain_tui import ApplyScreen, FrpDomainApp
from frpctl.domains import DomainBinding, DomainInventory, DomainStore, NodeDomains
from frpctl.inventory import InventoryStore
from frpctl.models import Client, Inventory, Mapping, Node, SSHConfig
from frpctl.tui_widgets import ConfirmScreen


def seed(tmp_path):
    inventory = InventoryStore(tmp_path / "inventory.yaml", tmp_path / "secrets.yaml")
    inventory.save(
        Inventory(
            clients=[
                Client(
                    "client-109",
                    SSHConfig("192.168.1.109", "operator"),
                    [
                        Mapping("web", "tcp", "127.0.0.1", 80, 80),
                        Mapping("api", "tcp", "127.0.0.1", 3000, 13000),
                    ],
                    ["node-139"],
                )
            ],
            nodes=[Node("node-139", SSHConfig("198.51.100.20", "root"), 17001)],
        ),
        {"nodes": {"node-139": {"token": "secret"}}},
    )
    domains = DomainStore(tmp_path / "domains.yaml")
    domains.save(
        DomainInventory(
            nodes=[
                NodeDomains(
                    "node-139",
                    "admin@example.com",
                    bindings=[
                        DomainBinding("example.com", 80),
                        DomainBinding("api.example.com", 13000),
                    ],
                )
            ]
        )
    )
    return inventory, domains


def drive(tmp_path, scenario):
    """Run one Pilot scenario against a freshly seeded pair of stores."""
    inventory, domains = seed(tmp_path)

    async def main():
        app = FrpDomainApp(inventory.inventory_path, domains.path)
        async with app.run_test() as pilot:
            await scenario(app, pilot)

    asyncio.run(main())
    return inventory, domains


def bindings(domains, node_id="node-139"):
    node = domains.load().node(node_id)
    return {x.hostname: x.upstream_port for x in node.bindings}


def test_bindings_and_dns_tables_are_populated(tmp_path):
    async def scenario(app, pilot):
        del pilot
        table = app.query_one("#table", DataTable)
        dns = app.query_one("#dns-table", DataTable)
        assert table.row_count == 2
        assert dns.row_count == 2
        assert table.get_row("node-139/example.com") == [
            "node-139",
            "example.com",
            "80",
            "admin@example.com",
            "198.51.100.20",
        ]
        assert dns.get_row_at(1) == ["A", "api.example.com", "198.51.100.20"]
        assert app.focused is table

    drive(tmp_path, scenario)


def test_delete_key_removes_the_highlighted_binding(tmp_path):
    async def scenario(app, pilot):
        await pilot.press("d")
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await pilot.pause()
        assert app.query_one("#table", DataTable).row_count == 1

    _, domains = drive(tmp_path, scenario)
    assert bindings(domains) == {"api.example.com": 13000}


def test_escape_cancels_the_deletion(tmp_path):
    async def scenario(app, pilot):
        await pilot.press("d")
        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one("#table", DataTable).row_count == 2

    _, domains = drive(tmp_path, scenario)
    assert len(bindings(domains)) == 2


def test_edit_key_prefills_the_form_and_save_updates_the_port(tmp_path):
    async def scenario(app, pilot):
        table = app.query_one("#table", DataTable)
        table.move_cursor(row=1)
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert app.query_one("#tabs", TabbedContent).active == "binding"
        assert app.query_one("#b-node", Input).value == "node-139"
        assert app.query_one("#b-hostname", Input).value == "api.example.com"
        assert app.query_one("#b-port", Input).value == "13000"
        app.query_one("#b-port", Input).value = "13001"
        app.save_binding()
        await pilot.pause()

    _, domains = drive(tmp_path, scenario)
    assert bindings(domains) == {"example.com": 80, "api.example.com": 13001}


def test_changing_the_hostname_while_editing_renames_instead_of_duplicating(tmp_path):
    async def scenario(app, pilot):
        table = app.query_one("#table", DataTable)
        table.move_cursor(row=1)
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        app.query_one("#b-hostname", Input).value = "v2.example.com"
        app.save_binding()
        await pilot.pause()

    _, domains = drive(tmp_path, scenario)
    assert bindings(domains) == {"example.com": 80, "v2.example.com": 13000}


def test_a_new_binding_is_appended_and_normalized(tmp_path):
    async def scenario(app, pilot):
        app.query_one("#b-node", Input).value = "node-139"
        app.query_one("#b-hostname", Input).value = "WWW.EXAMPLE.COM."
        app.query_one("#b-port", Input).value = "13000"
        app.save_binding()
        await pilot.pause()
        assert app.query_one("#table", DataTable).row_count == 3

    _, domains = drive(tmp_path, scenario)
    assert bindings(domains)["www.example.com"] == 13000


def test_a_binding_for_an_unmanaged_node_is_rejected(tmp_path):
    async def scenario(app, pilot):
        app.query_one("#b-node", Input).value = "node-404"
        app.query_one("#b-hostname", Input).value = "api.example.com"
        app.query_one("#b-port", Input).value = "13000"
        app.save_binding()
        await pilot.pause()
        assert app.query_one("#table", DataTable).row_count == 2

    _, domains = drive(tmp_path, scenario)
    assert [x.node_id for x in domains.load().nodes] == ["node-139"]


def test_an_invalid_hostname_is_rejected(tmp_path):
    async def scenario(app, pilot):
        app.query_one("#b-node", Input).value = "node-139"
        app.query_one("#b-hostname", Input).value = "not a hostname"
        app.query_one("#b-port", Input).value = "13000"
        app.save_binding()
        await pilot.pause()

    _, domains = drive(tmp_path, scenario)
    assert len(bindings(domains)) == 2


def test_a_missing_upstream_port_is_rejected(tmp_path):
    async def scenario(app, pilot):
        app.query_one("#b-node", Input).value = "node-139"
        app.query_one("#b-hostname", Input).value = "new.example.com"
        app.save_binding()
        await pilot.pause()

    _, domains = drive(tmp_path, scenario)
    assert len(bindings(domains)) == 2


def test_plan_key_renders_nginx_without_touching_the_inventory(tmp_path):
    async def scenario(app, pilot):
        await pilot.press("g")
        await pilot.pause()
        assert app.query_one("#tabs", TabbedContent).active == "plan"
        rendered = str(app.query_one("#plan-text", Static).renderable)
        # Port 80 belongs to the "web" mapping, so it is relocated to 10080 and
        # Nginx proxies direct IP access back to it.
        assert "proxy_pass http://127.0.0.1:10080;" in rendered
        assert "proxy_pass http://127.0.0.1:13000;" in rendered
        assert "server_name api.example.com;" in rendered
        assert "listen 443 ssl http2;" in rendered

    inventory, _ = drive(tmp_path, scenario)
    # The preview ran on a deep copy: nothing was written back.
    assert inventory.load().node("node-139").port_overrides == {}


def test_apply_key_opens_the_form_prefilled_and_runs_nothing(tmp_path):
    async def scenario(app, pilot):
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, ApplyScreen)
        assert app.screen.query_one("#a-email", Input).value == "admin@example.com"
        assert app.screen.hostnames == ["example.com", "api.example.com"]
        await pilot.press("escape")
        await pilot.pause()
        assert app.busy is False

    inventory, domains = drive(tmp_path, scenario)
    assert inventory.load().node("node-139").port_overrides == {}
    assert len(bindings(domains)) == 2


def test_apply_refuses_without_an_email(tmp_path):
    async def scenario(app, pilot):
        await pilot.press("a")
        await pilot.pause()
        app.screen.query_one("#a-email", Input).value = "  "
        app.screen.apply()
        await pilot.pause()
        assert isinstance(app.screen, ApplyScreen)
        assert app.busy is False

    drive(tmp_path, scenario)


def test_deleting_the_last_binding_makes_apply_refuse(tmp_path):
    async def scenario(app, pilot):
        for _ in range(2):
            await pilot.press("d")
            await pilot.press("y")
            await pilot.pause()
        assert app.query_one("#table", DataTable).row_count == 0
        await pilot.press("a")
        await pilot.pause()
        assert not isinstance(app.screen, ApplyScreen)

    _, domains = drive(tmp_path, scenario)
    assert bindings(domains) == {}
