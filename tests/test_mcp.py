"""MCP parity: every CLI verb an agent needs is a tool; propose is dry-run only."""
import inspect

from defios import mcp


def test_cli_verbs_are_tools():
    assert {"defios_permit", "defios_simulate", "defios_propose", "defios_resolve", "defios_recipe",
            "defios_actions", "defios_plan", "defios_check", "defios_whois"} <= set(mcp.TOOLS)


def test_propose_tool_cannot_send():
    assert "send" not in inspect.signature(mcp.tool_propose).parameters
    assert "send=False" in inspect.getsource(mcp.tool_propose)


def test_actions_search():
    r = mcp.tool_actions("pendle any_buy_pt")
    assert r["count"] == 1 and r["matches"][0]["card"] == "pendle" and "market_address" in r["matches"][0]["params"]
    assert "borrow" in {m["action"] for m in mcp.tool_actions("morpho_markets borrow")["matches"]}


def test_recipe_list():
    assert set(mcp.tool_recipe()) == {"morpho_loop", "pendle_pt"}
