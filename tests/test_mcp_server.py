"""
Milestone 4 test scenarios: exercises filesystem_mcp_server.py (and the
bonus resume_index_mcp_server.py) through mcp_client.py exactly the way the
refactored agent does -- as a subprocess speaking JSON-RPC 2.0 over stdio,
never by importing fs_tools directly.

Run:
    venv\\Scripts\\python tests\\test_mcp_server.py
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from mcp_client import MCPClient, MCPError  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
results = []


def check(name, condition, detail=""):
    status = PASS if condition else FAIL
    results.append((status, name, detail))
    print(f"[{status}] {name}" + (f" -- {detail}" if detail and status == FAIL else ""))


def main():
    with MCPClient("filesystem_mcp_server.py") as fs:
        # 1. Resource discovery
        tools = fs.list_tools()
        tool_names = {t["name"] for t in tools}
        check(
            "tools/list exposes all Milestone 1 tools + watch_directory + batch_process",
            {"list_files", "read_file", "write_file", "search_in_file", "search_in_directory",
             "watch_directory", "batch_process"} <= tool_names,
            f"got {tool_names}",
        )

        resources = fs.list_resources()
        check("resources/list discovers sample job descriptions", any("jd_01" in r["name"] for r in resources))

        # 2. Basic tool call: list_files
        listing = fs.list_files("data/job_descriptions", extension="txt")
        check("list_files returns the 7 sample job descriptions", listing["count"] == 7, listing)

        # 3. read_file via a resource URI
        jd_resource = next(r for r in resources if "jd_01" in r["name"])
        read_via_resource = fs.read_resource(jd_resource["uri"])
        check("resources/read returns text content", len(read_via_resource["text"]) > 0)

        # 4. write_file + read_file round trip, scoped to the allowed 'reports' root
        test_path = "reports/mcp_smoke_test.txt"
        write_result = fs.write_file(test_path, "MCP smoke test content", overwrite=True)
        check("write_file reports success", write_result["message"] == "File written successfully", write_result)
        read_back = fs.read_file(test_path)
        check("read_file round-trips written content", read_back["text"] == "MCP smoke test content")

        # 5. search_in_file
        search_result = fs.search_in_file(test_path, "smoke")
        check("search_in_file finds the keyword", search_result["match_count"] == 1)

        # 6. Path safety: a path outside allowed_roots must be refused
        try:
            fs.read_file("../outside_project.txt")
            check("read_file outside allowed_roots is refused", False, "no error raised")
        except MCPError as error:
            check("read_file outside allowed_roots is refused", error.code == -32003, error)

        # 7. Error handling: unknown tool -> proper JSON-RPC error, not a crash
        try:
            fs.call_tool("does_not_exist", {})
            check("unknown tool call returns a JSON-RPC error", False, "no error raised")
        except MCPError as error:
            check("unknown tool call returns a JSON-RPC error", error.code == -32004, error)

        # 8. batch_process: several ops, one round trip, one deliberately bad path
        batch = fs.batch_process([
            {"op": "read_file", "params": {"path": test_path}},
            {"op": "search_in_directory", "params": {"directory": "data/job_descriptions", "keyword": "Python"}},
            {"op": "read_file", "params": {"path": "reports/does_not_exist.txt"}},
        ])
        check("batch_process runs all ops and isolates the failing one",
              batch["total"] == 3 and batch["succeeded"] == 2 and batch["failed"] == 1, batch)

        # 9. watch_directory: first call establishes baseline, second call (after
        #    a new file appears) reports it as new.
        fs.watch_directory("reports", extensions=["txt"], reset=True)
        marker_path = "reports/mcp_watch_marker.txt"
        fs.write_file(marker_path, "new resume-like file", overwrite=True)
        watch_result = fs.watch_directory("reports", extensions=["txt"])
        check("watch_directory detects a newly written file",
              "mcp_watch_marker.txt" in watch_result["new_files"], watch_result)

        # 10. config/get + config/set (whitelisted key only)
        config = fs.get_config()
        check("config/get returns allowed_roots", "allowed_roots" in config)
        updated = fs.set_config("max_batch_size", 25)
        check("config/set updates a mutable key", updated["max_batch_size"] == 25)
        try:
            fs.set_config("allowed_roots", ["/"])
            check("config/set refuses a non-mutable key", False, "no error raised")
        except MCPError as error:
            check("config/set refuses a non-mutable key", error.code == -32602, error)

    # 11. Bonus: a second, independent MCP server (multi-MCP integration)
    try:
        with MCPClient("resume_index_mcp_server.py") as index:
            vocab = index.call_tool("skill_vocabulary", {})
            check("resume-index MCP server (second server) responds", vocab["count"] > 0, vocab)
    except Exception as error:  # noqa: BLE001 - the embedding model download can be flaky offline
        check("resume-index MCP server (second server) responds", False, str(error))

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
