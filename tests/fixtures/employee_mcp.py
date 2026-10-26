from mcp.server.mcpserver import MCPServer
from pathlib import Path
import sys


class FixtureServer(MCPServer):
    async def list_tools(self):
        tools = await super().list_tools()
        if len(sys.argv) > 1:
            Path(sys.argv[1]).write_text("ready", encoding="utf-8")
        return tools


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.mcp.bridge import register_memory_tools


async def memory_call(path, payload):
    return {"path": path}


server = FixtureServer("employee_fixture", log_level="WARNING")


@server.tool()
async def fixture_sentinel() -> str:
    """MCP_PRESERVATION_SENTINEL"""
    return "fixture"


register_memory_tools(server, memory_call)


if __name__ == "__main__":
    server.run(transport="stdio")
