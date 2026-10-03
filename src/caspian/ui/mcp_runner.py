"""Starting, stopping and testing the MCP server executable from the app (#17)."""

import asyncio
import atexit
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

from caspian.mcp_server import bind_host


def mcp_command() -> list[str]:
    """How to start caspian-mcp on this PC: the exe next to the app when installed."""
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).with_name("caspian-mcp.exe"))]
    found = shutil.which("caspian-mcp") or shutil.which("caspian-mcp", path=str(Path(sys.executable).parent))
    return [found] if found else [sys.executable, "-m", "caspian.mcp_server"]


def claude_code_command(command: list[str]) -> str:
    """Ready-to-paste command for Claude Code (quotes keep «Program Files» in one piece)."""
    return "claude mcp add caspian-warehouse -- " + " ".join(f'"{part}"' for part in command)


def http_address(port: int, lan: bool) -> str:
    host = "<نشانی IP این رایانه>" if bind_host(lan) == "0.0.0.0" else "127.0.0.1"
    return f"http://{host}:{port}/mcp"


async def probe_stdio(command: list[str], timeout: float = 45.0) -> list[str]:
    """Start the server over stdio and list its tools, like an MCP client would."""
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def run() -> list[str]:
        params = StdioServerParameters(command=command[0], args=command[1:])
        async with stdio_client(params) as streams, ClientSession(*streams) as session:
            await session.initialize()
            return sorted(t.name for t in (await session.list_tools()).tools)

    return await asyncio.wait_for(run(), timeout)


async def http_running(port: int, transport: httpx.AsyncBaseTransport | None = None) -> bool:
    """True if something answers on the MCP port (without a token it must say 401)."""
    try:
        async with httpx.AsyncClient(timeout=1.5, transport=transport) as client:
            response = await client.post(f"http://127.0.0.1:{port}/mcp", json={})
    except httpx.HTTPError:
        return False
    return response.status_code in (200, 400, 401, 405, 406)


class HttpServerProcess:
    """The HTTP server started from this app; stopped with the app."""

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        atexit.register(self.stop)

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self, port: int, lan: bool) -> None:
        if self.running:
            return
        args = [*mcp_command(), "--http", "--port", str(port)] + (["--lan"] if lan else [])
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self._process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, creationflags=flags)

    def stop(self) -> None:
        if self.running:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None


SERVER = HttpServerProcess()
