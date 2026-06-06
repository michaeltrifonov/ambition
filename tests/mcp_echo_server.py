"""A trivial stdio MCP server used to validate uaa.mcp_client.MCPManager end-to-end."""
from mcp.server.fastmcp import FastMCP

server = FastMCP("echo")


@server.tool()
def echo(text: str) -> str:
    """Echo the given text back."""
    return f"echo: {text}"


@server.tool()
def add(a: int, b: int) -> str:
    """Add two integers."""
    return str(a + b)


if __name__ == "__main__":
    server.run()  # stdio transport by default
