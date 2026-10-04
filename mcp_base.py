"""
Shared JSON-RPC 2.0 / stdio transport used by every MCP server in this
project (filesystem_mcp_server.py, resume_index_mcp_server.py). Factored out
so adding a new MCP server is just: define tool handlers + schemas, call
`serve_forever(...)`.
"""
import json
import sys
import time
import traceback

# Standard JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class RpcError(Exception):
    """Carries a JSON-RPC error {code, message, data} back to the transport loop."""

    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_error_object(self):
        error = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return error


def make_tools_call_handler(tool_dispatch, invalid_params_code=INVALID_PARAMS, unknown_tool_code=-32004,
                             tool_execution_code=-32006):
    """Build a `tools/call` handler from a {name: fn(config, arguments)} dispatch table."""

    def handle_tools_call(config, params):
        if not isinstance(params, dict) or "name" not in params:
            raise RpcError(invalid_params_code, "tools/call requires a 'name' field")

        name = params["name"]
        arguments = params.get("arguments", {}) or {}
        handler = tool_dispatch.get(name)
        if handler is None:
            raise RpcError(unknown_tool_code, f"Unknown tool: {name}")

        try:
            result = handler(config, arguments)
        except RpcError:
            raise
        except KeyError as error:
            raise RpcError(invalid_params_code, f"Missing required argument: {error}") from error
        except Exception as error:  # noqa: BLE001
            raise RpcError(tool_execution_code, f"{type(error).__name__}: {error}") from error

        return {"content": [{"type": "json", "json": result}], "isError": False}

    return handle_tools_call


def make_base_method_dispatch(server_name, server_version, tool_schemas, tool_dispatch):
    """Standard initialize / tools/list / tools/call / ping handlers every
    server needs; callers merge in their own resources/* and config/* handlers."""

    def handle_initialize(config, params):
        return {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": server_name, "version": server_version},
            "capabilities": {"tools": {}, "resources": {}},
        }

    def handle_tools_list(config, params):
        return {"tools": tool_schemas}

    def handle_ping(config, params):
        return {"ok": True, "server": server_name, "time": time.time()}

    return {
        "initialize": handle_initialize,
        "tools/list": handle_tools_list,
        "tools/call": make_tools_call_handler(tool_dispatch),
        "ping": handle_ping,
    }


def _error_response(request_id, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _success_response(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def handle_request(config, method_dispatch, request):
    request_id = request.get("id") if isinstance(request, dict) else None

    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or "method" not in request:
        return _error_response(request_id, INVALID_REQUEST, "Invalid JSON-RPC 2.0 request")

    method = request["method"]
    params = request.get("params", {})
    handler = method_dispatch.get(method)

    if handler is None:
        return _error_response(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")

    try:
        result = handler(config, params)
    except RpcError as error:
        return _error_response(request_id, error.code, error.message, error.data)
    except Exception as error:  # noqa: BLE001 - never let one bad request kill the server
        return _error_response(
            request_id, INTERNAL_ERROR, f"{type(error).__name__}: {error}",
            {"traceback": traceback.format_exc()},
        )

    if request_id is None and "id" not in request:
        return None  # JSON-RPC notification: no response
    return _success_response(request_id, result)


def serve_forever(config, method_dispatch, stdin=None, stdout=None):
    """Newline-delimited JSON-RPC 2.0 over stdio. Never write anything but
    protocol responses to stdout -- logs/diagnostics must go to stderr."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    for line in stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError as error:
            response = _error_response(None, PARSE_ERROR, f"Invalid JSON: {error}")
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
            continue

        response = handle_request(config, method_dispatch, request)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
