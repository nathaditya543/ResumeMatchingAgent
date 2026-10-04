"""
Milestone 4 (Part A): Filesystem MCP server.

Exposes Milestone 1's file-system tools (milestone1/fs_tools.py) over the
Model Context Protocol instead of as plain Python imports. Speaks JSON-RPC
2.0 over stdio (one JSON object per line on stdin/stdout -- the standard MCP
"stdio transport" framing), so any MCP-aware client -- including our own
mcp_client.py -- can launch this file as a subprocess and talk to it without
ever importing fs_tools directly.

Why hand-rolled instead of the official `mcp` SDK: the project's venv is
pinned to Python 3.9 (see milestone1/2 dependencies) and the official SDK
requires Python >= 3.10. The wire format and method surface below follow the
MCP spec (initialize, tools/list, tools/call, resources/list, resources/read)
closely enough to swap in the official SDK later without changing callers.

Run standalone for manual testing:
    python filesystem_mcp_server.py
    {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    (paste a line of JSON, press enter, read the response line)

Protocol surface
-----------------
    initialize            -> server info + capabilities
    tools/list             -> available tools + JSON-schema-ish parameter specs
    tools/call              -> {name, arguments} -> {content, isError}
    resources/list          -> discoverable resources (configured directories)
    resources/read           -> {uri} -> file contents
    config/get                -> current server configuration
    config/set                 -> update one whitelisted config key
    ping                        -> liveness check

Tools
-----
    list_files, read_file, write_file, search_in_file, search_in_directory
        -- thin wrappers around milestone1/fs_tools.py (Milestone 1).
    watch_directory
        -- MCP-specific: polling change-detection. Each call snapshots a
           directory's (name, mtime, size) and diffs it against the
           snapshot from the previous call, returning new/modified/deleted
           files. State is kept in memory and persisted to a small JSON
           file (see config `watch_state_file`) so it survives restarts.
    batch_process
        -- MCP-specific: runs a list of {op, params} sub-operations
           (any of the tools above) in one round trip and returns
           per-operation success/error, instead of one request per file.
"""
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "milestone1"))
import fs_tools  # noqa: E402  (Milestone 1)

from mcp_base import (  # noqa: E402
    RpcError,
    make_base_method_dispatch,
    serve_forever as _serve_forever,
)
from mcp_base import INVALID_PARAMS  # noqa: E402,F401 (re-exported for readability below)

SERVER_NAME_DEFAULT = "filesystem-mcp-server"
SERVER_VERSION_DEFAULT = "1.0.0"
CONFIG_PATH = PROJECT_ROOT / "mcp_server_config.json"

DEFAULT_CONFIG = {
    "allowed_roots": ["data", "reports"],
    "watch_state_file": ".watch_state.json",
    "max_batch_size": 50,
    "server_name": SERVER_NAME_DEFAULT,
    "server_version": SERVER_VERSION_DEFAULT,
}

MUTABLE_CONFIG_KEYS = {"max_batch_size"}  # keys `config/set` is allowed to change at runtime


# ---------------------------------------------------------------------------
# Implementation-defined server errors (JSON-RPC reserves -32000..-32099).
# Standard codes (parse/invalid-request/method-not-found/invalid-params/
# internal) live in mcp_base.py and are shared across every server.
# ---------------------------------------------------------------------------
ERR_FILE_NOT_FOUND = -32001
ERR_UNSUPPORTED_FILE_TYPE = -32002
ERR_PATH_NOT_ALLOWED = -32003
ERR_UNKNOWN_TOOL = -32004
ERR_UNKNOWN_RESOURCE = -32005
ERR_TOOL_EXECUTION = -32006


# ---------------------------------------------------------------------------
# Config management
# ---------------------------------------------------------------------------
def load_config():
    if CONFIG_PATH.exists():
        try:
            on_disk = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            on_disk = {}
    else:
        on_disk = {}
    return {**DEFAULT_CONFIG, **on_disk}


def save_config(config):
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Path safety: every tool that touches the filesystem is confined to the
# configured allowed_roots, resolved relative to the project root, so an MCP
# client can never read/write arbitrary paths on the host.
# ---------------------------------------------------------------------------
def resolve_allowed_path(config, raw_path):
    allowed_roots = [
        (PROJECT_ROOT / root).resolve() for root in config.get("allowed_roots", [])
    ]
    candidate = Path(raw_path)
    resolved = candidate.resolve() if candidate.is_absolute() else (PROJECT_ROOT / candidate).resolve()

    for root in allowed_roots:
        if resolved == root or root in resolved.parents:
            return resolved

    raise RpcError(
        ERR_PATH_NOT_ALLOWED,
        f"Path is outside the server's allowed roots: {raw_path}",
        {"allowed_roots": [str(r) for r in allowed_roots]},
    )


# ---------------------------------------------------------------------------
# Tool implementations (wrap fs_tools + the two new MCP-specific tools)
# ---------------------------------------------------------------------------
def tool_list_files(config, arguments):
    directory = resolve_allowed_path(config, arguments["directory"])
    try:
        files = fs_tools.list_files(str(directory), extension=arguments.get("extension"))
    except FileNotFoundError as error:
        raise RpcError(ERR_FILE_NOT_FOUND, str(error)) from error
    return {"directory": str(directory), "files": files, "count": len(files)}


def tool_read_file(config, arguments):
    path = resolve_allowed_path(config, arguments["path"])
    try:
        result = fs_tools.read_file(str(path))
    except FileNotFoundError as error:
        raise RpcError(ERR_FILE_NOT_FOUND, str(error)) from error
    except ValueError as error:
        raise RpcError(ERR_UNSUPPORTED_FILE_TYPE, str(error)) from error
    return result


def tool_write_file(config, arguments):
    path = resolve_allowed_path(config, arguments["path"])
    try:
        result = fs_tools.write_file(
            str(path),
            arguments["content"],
            overwrite=arguments.get("overwrite", True),
            create_parent_dirs=arguments.get("create_parent_dirs", True),
        )
    except FileExistsError as error:
        raise RpcError(ERR_TOOL_EXECUTION, str(error)) from error
    except (ValueError, IsADirectoryError) as error:
        raise RpcError(ERR_UNSUPPORTED_FILE_TYPE, str(error)) from error
    return result


def tool_search_in_file(config, arguments):
    filepath = resolve_allowed_path(config, arguments["filepath"])
    try:
        return fs_tools.search_in_file(str(filepath), arguments["keyword"])
    except FileNotFoundError as error:
        raise RpcError(ERR_FILE_NOT_FOUND, str(error)) from error
    except ValueError as error:
        raise RpcError(INVALID_PARAMS, str(error)) from error


def tool_search_in_directory(config, arguments):
    directory = resolve_allowed_path(config, arguments["directory"])
    try:
        return fs_tools.search_in_directory(str(directory), arguments["keyword"])
    except FileNotFoundError as error:
        raise RpcError(ERR_FILE_NOT_FOUND, str(error)) from error
    except NotADirectoryError as error:
        raise RpcError(INVALID_PARAMS, str(error)) from error


def _snapshot_directory(directory, extensions):
    snapshot = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        if extensions and path.suffix.lower() not in extensions:
            continue
        stats = path.stat()
        snapshot[path.name] = {"mtime": stats.st_mtime, "size": stats.st_size}
    return snapshot


def _load_watch_state(config):
    state_path = PROJECT_ROOT / config.get("watch_state_file", ".watch_state.json")
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8")), state_path
        except (json.JSONDecodeError, OSError):
            return {}, state_path
    return {}, state_path


def tool_watch_directory(config, arguments):
    """Polling watch: diff the directory's current file listing against the
    snapshot saved on the previous call and report what changed. A client
    (or a scheduled/loop-based agent) calls this repeatedly to be notified
    of new resumes dropped into a directory, without the server needing a
    long-lived background thread or push transport."""
    directory = resolve_allowed_path(config, arguments["directory"])
    if not directory.is_dir():
        raise RpcError(ERR_FILE_NOT_FOUND, f"Directory not found: {directory}")

    extensions = arguments.get("extensions")
    if extensions:
        extensions = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions}

    watch_key = str(directory)
    all_state, state_path = _load_watch_state(config)
    previous = {} if arguments.get("reset") else all_state.get(watch_key, {})

    current = _snapshot_directory(directory, extensions)

    new_files = sorted(name for name in current if name not in previous)
    removed_files = sorted(name for name in previous if name not in current)
    modified_files = sorted(
        name for name in current
        if name in previous and current[name] != previous[name]
    )

    all_state[watch_key] = current
    state_path.write_text(json.dumps(all_state, indent=2), encoding="utf-8")

    return {
        "directory": str(directory),
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "new_files": new_files,
        "modified_files": modified_files,
        "removed_files": removed_files,
        "total_files_tracked": len(current),
        "has_changes": bool(new_files or modified_files or removed_files),
    }


TOOL_DISPATCH = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "search_in_file": tool_search_in_file,
    "search_in_directory": tool_search_in_directory,
    "watch_directory": tool_watch_directory,
}


def tool_batch_process(config, arguments):
    """Run several file operations in one call. Each op is one of the other
    registered tools; a partial failure in one op does not abort the rest --
    each result carries its own success/error so a client can process 50
    resumes and know exactly which ones failed and why."""
    operations = arguments.get("operations", [])
    max_batch = config.get("max_batch_size", 50)
    if len(operations) > max_batch:
        raise RpcError(
            INVALID_PARAMS,
            f"Batch of {len(operations)} exceeds max_batch_size ({max_batch})",
        )

    results = []
    for index, operation in enumerate(operations):
        op_name = operation.get("op")
        op_params = operation.get("params", {})
        handler = TOOL_DISPATCH.get(op_name)

        if handler is None:
            results.append({
                "index": index, "op": op_name, "success": False,
                "error": {"code": ERR_UNKNOWN_TOOL, "message": f"Unknown op: {op_name}"},
            })
            continue

        try:
            result = handler(config, op_params)
            results.append({"index": index, "op": op_name, "success": True, "result": result})
        except RpcError as error:
            results.append({"index": index, "op": op_name, "success": False, "error": error.to_error_object()})
        except Exception as error:  # noqa: BLE001 - one bad file must not kill the batch
            results.append({
                "index": index, "op": op_name, "success": False,
                "error": {"code": ERR_TOOL_EXECUTION, "message": str(error)},
            })

    succeeded = sum(1 for r in results if r["success"])
    return {
        "total": len(results),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
        "results": results,
    }


TOOL_DISPATCH["batch_process"] = tool_batch_process


TOOL_SCHEMAS = [
    {
        "name": "list_files",
        "description": "List files in a directory, optionally filtered by extension.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "directory": {"type": "string"},
                "extension": {"type": "string", "description": "e.g. 'pdf' or '.pdf'"},
            },
            "required": ["directory"],
        },
    },
    {
        "name": "read_file",
        "description": "Read text + metadata from a .txt/.pdf/.docx file.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "write_file",
        "description": "Write text content to a .txt or .docx file (PDFs are read-only).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "overwrite": {"type": "boolean", "default": True},
                "create_parent_dirs": {"type": "boolean", "default": True},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "search_in_file",
        "description": "Search a single file for a keyword, with surrounding context.",
        "inputSchema": {
            "type": "object",
            "properties": {"filepath": {"type": "string"}, "keyword": {"type": "string"}},
            "required": ["filepath", "keyword"],
        },
    },
    {
        "name": "search_in_directory",
        "description": "Search every readable file in a directory for a keyword.",
        "inputSchema": {
            "type": "object",
            "properties": {"directory": {"type": "string"}, "keyword": {"type": "string"}},
            "required": ["directory", "keyword"],
        },
    },
    {
        "name": "watch_directory",
        "description": (
            "Polling change-detection for a directory: returns files added/modified/"
            "removed since the last watch_directory call on that directory (e.g. new "
            "resumes dropped into data/resumes)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "directory": {"type": "string"},
                "extensions": {"type": "array", "items": {"type": "string"}},
                "reset": {"type": "boolean", "default": False, "description": "Treat every current file as new."},
            },
            "required": ["directory"],
        },
    },
    {
        "name": "batch_process",
        "description": "Run a list of {op, params} file operations in one call; returns per-op success/error.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"op": {"type": "string"}, "params": {"type": "object"}},
                        "required": ["op"],
                    },
                }
            },
            "required": ["operations"],
        },
    },
]


# ---------------------------------------------------------------------------
# Resource discovery: the configured data directories, exposed as browsable
# MCP resources rather than tool arguments the client has to already know.
# ---------------------------------------------------------------------------
RESOURCE_DIRS = [
    ("data/resumes", "Candidate resumes (txt/pdf/docx)"),
    ("data/job_descriptions", "Sample job descriptions"),
    ("reports", "Generated match reports"),
]


def list_resources(config):
    resources = []
    for relative, description in RESOURCE_DIRS:
        directory = (PROJECT_ROOT / relative).resolve()
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file():
                continue
            resources.append({
                "uri": path.resolve().as_uri(),
                "name": path.name,
                "description": description,
                "mimeType": _guess_mime(path),
            })
    return resources


def _guess_mime(path):
    return {
        ".txt": "text/plain",
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }.get(path.suffix.lower(), "application/octet-stream")


def read_resource(config, uri):
    if not uri.startswith("file://"):
        raise RpcError(ERR_UNKNOWN_RESOURCE, f"Unsupported resource URI scheme: {uri}")

    # Path.from_uri() isn't available pre-3.13; reconstruct the path portably instead.
    from urllib.parse import urlparse
    from urllib.request import url2pathname

    path = Path(url2pathname(urlparse(uri).path))
    resolved = resolve_allowed_path(config, path)
    try:
        result = fs_tools.read_file(str(resolved))
    except FileNotFoundError as error:
        raise RpcError(ERR_FILE_NOT_FOUND, str(error)) from error
    except ValueError as error:
        raise RpcError(ERR_UNSUPPORTED_FILE_TYPE, str(error)) from error
    return {
        "uri": uri,
        "mimeType": _guess_mime(resolved),
        "text": result["text"],
    }


# ---------------------------------------------------------------------------
# JSON-RPC method handlers (initialize / tools/list / tools/call / ping come
# from mcp_base.py; only the resource + config endpoints are specific here)
# ---------------------------------------------------------------------------
def handle_resources_list(config, params):
    return {"resources": list_resources(config)}


def handle_resources_read(config, params):
    if not isinstance(params, dict) or "uri" not in params:
        raise RpcError(INVALID_PARAMS, "resources/read requires a 'uri' field")
    return read_resource(config, params["uri"])


def handle_config_get(config, params):
    return dict(config)


def handle_config_set(config, params):
    if not isinstance(params, dict) or "key" not in params or "value" not in params:
        raise RpcError(INVALID_PARAMS, "config/set requires 'key' and 'value' fields")
    key = params["key"]
    if key not in MUTABLE_CONFIG_KEYS:
        raise RpcError(INVALID_PARAMS, f"'{key}' is not a runtime-mutable config key ({sorted(MUTABLE_CONFIG_KEYS)})")
    config[key] = params["value"]
    save_config(config)
    return dict(config)


METHOD_DISPATCH = make_base_method_dispatch(
    SERVER_NAME_DEFAULT, SERVER_VERSION_DEFAULT, TOOL_SCHEMAS, TOOL_DISPATCH
)
METHOD_DISPATCH.update({
    "resources/list": handle_resources_list,
    "resources/read": handle_resources_read,
    "config/get": handle_config_get,
    "config/set": handle_config_set,
})


def serve_forever(stdin=None, stdout=None):
    _serve_forever(load_config(), METHOD_DISPATCH, stdin=stdin, stdout=stdout)


if __name__ == "__main__":
    serve_forever()
