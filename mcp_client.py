"""
Milestone 4 (Part B): a minimal synchronous MCP client.

Launches an MCP server (filesystem_mcp_server.py, stats_mcp_server.py, ...)
as a subprocess and speaks the same newline-delimited JSON-RPC 2.0 protocol
those servers implement. This replaces `import fs_tools` in the agent: the
agent no longer touches the filesystem directly, it asks the filesystem MCP
server to do it, over a real process boundary.

Usage:
    client = MCPClient("filesystem_mcp_server.py")
    client.start()
    client.read_file("data/resumes/synth_01_software_engineer_python.txt")
    client.stop()

or as a context manager:
    with MCPClient("filesystem_mcp_server.py") as client:
        client.list_files("data/resumes", extension="txt")

`get_fs_client()` / `get_client(server_script)` below give the rest of the
codebase a lazily-started, process-wide singleton per server script, mirroring
how tools.py already lazily builds the JobMatcher singleton -- so tools.py and
matching_agent.py can just call `get_fs_client().read_file(...)` without each
one managing its own subprocess.
"""
import atexit
import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent


class MCPError(Exception):
    """Raised for any JSON-RPC error response (bad params, unknown tool,
    path outside allowed roots, file not found, ...). Mirrors the
    {code, message, data} shape the server sends."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"[MCP error {code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class MCPClient:
    def __init__(self, server_script: str, python_executable: Optional[str] = None):
        self.server_path = str((PROJECT_ROOT / server_script).resolve())
        self.python_executable = python_executable or sys.executable
        self._process: Optional[subprocess.Popen] = None
        self._next_id = 1
        self._lock = threading.Lock()  # one request in flight at a time, matching-call/response

    # -- lifecycle ----------------------------------------------------------
    def start(self):
        if self._process is not None:
            return self
        self._process = subprocess.Popen(
            [self.python_executable, self.server_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
            cwd=str(PROJECT_ROOT),
        )
        self._request("initialize", {})
        return self

    def stop(self):
        if self._process is None:
            return
        try:
            self._process.stdin.close()
        except Exception:
            pass
        self._process.terminate()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()
        self._process = None

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    # -- transport ------------------------------------------------------------
    def _request(self, method: str, params: Dict[str, Any]) -> Any:
        if self._process is None:
            self.start()
            if method == "initialize":
                return {}  # start() already sent this exact call

        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}

            assert self._process.stdin is not None and self._process.stdout is not None
            self._process.stdin.write(json.dumps(payload) + "\n")
            self._process.stdin.flush()

            line = self._process.stdout.readline()
            if not line:
                stderr_tail = ""
                if self._process.stderr is not None:
                    stderr_tail = self._process.stderr.read()
                raise MCPError(-1, f"MCP server '{self.server_path}' closed the connection. stderr: {stderr_tail}")

            response = json.loads(line)

        if "error" in response:
            error = response["error"]
            raise MCPError(error.get("code", -1), error.get("message", "Unknown MCP error"), error.get("data"))
        return response.get("result")

    # -- generic protocol methods --------------------------------------------
    def list_tools(self) -> List[Dict[str, Any]]:
        return self._request("tools/list", {})["tools"]

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        content = result["content"][0]
        return content["json"] if content.get("type") == "json" else content

    def list_resources(self) -> List[Dict[str, Any]]:
        return self._request("resources/list", {})["resources"]

    def read_resource(self, uri: str) -> Dict[str, Any]:
        return self._request("resources/read", {"uri": uri})

    def get_config(self) -> Dict[str, Any]:
        return self._request("config/get", {})

    def set_config(self, key: str, value: Any) -> Dict[str, Any]:
        return self._request("config/set", {"key": key, "value": value})

    def ping(self) -> Dict[str, Any]:
        return self._request("ping", {})

    # -- convenience wrappers around the filesystem tools --------------------
    def list_files(self, directory: str, extension: Optional[str] = None) -> Dict[str, Any]:
        return self.call_tool("list_files", {"directory": directory, "extension": extension})

    def read_file(self, path: str) -> Dict[str, Any]:
        return self.call_tool("read_file", {"path": path})

    def write_file(self, path: str, content: str, overwrite: bool = True) -> Dict[str, Any]:
        return self.call_tool("write_file", {"path": path, "content": content, "overwrite": overwrite})

    def search_in_file(self, filepath: str, keyword: str) -> Dict[str, Any]:
        return self.call_tool("search_in_file", {"filepath": filepath, "keyword": keyword})

    def search_in_directory(self, directory: str, keyword: str) -> Dict[str, Any]:
        return self.call_tool("search_in_directory", {"directory": directory, "keyword": keyword})

    def watch_directory(self, directory: str, extensions: Optional[List[str]] = None, reset: bool = False) -> Dict[str, Any]:
        return self.call_tool("watch_directory", {"directory": directory, "extensions": extensions, "reset": reset})

    def batch_process(self, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self.call_tool("batch_process", {"operations": operations})


# ---------------------------------------------------------------------------
# Process-wide singletons, one MCPClient per server script, started lazily
# and torn down on interpreter exit.
# ---------------------------------------------------------------------------
_clients: Dict[str, MCPClient] = {}
_clients_lock = threading.Lock()


def get_client(server_script: str) -> MCPClient:
    with _clients_lock:
        client = _clients.get(server_script)
        if client is None or not client.is_running():
            client = MCPClient(server_script)
            client.start()
            _clients[server_script] = client
        return client


def get_fs_client() -> MCPClient:
    """The filesystem MCP server client used throughout the agent."""
    return get_client("filesystem_mcp_server.py")


def get_index_client() -> MCPClient:
    """The (bonus) resume-index MCP server client -- a second, independent
    MCP resource the agent can query alongside the filesystem one."""
    return get_client("resume_index_mcp_server.py")


@atexit.register
def _shutdown_all_clients():
    with _clients_lock:
        for client in _clients.values():
            client.stop()
        _clients.clear()
