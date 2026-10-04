# Resume Matching Agent (Milestone 3 + Milestone 4: MCP integration)

A LangGraph agent that screens resumes against a job description, explains
its rankings, and lets a hiring manager refine criteria conversationally.
Builds on:

- **Milestone 1** (`milestone1/fs_tools.py`) -- file system tools (read/write/
  search txt, pdf, docx).
- **Milestone 2** (`milestone2/resume_rag.py`, `vector_store.py`,
  `job_matcher.py`) -- resume chunking + embedding + a hybrid semantic/
  keyword job-matching engine.

## Project layout

```
matching_agent.py     LangGraph graph (nodes + edges) + CLI chat driver
state.py              AgentState (conversation history, requirements, shortlist, rounds)
tools.py              All agent tools: fs tools (via MCP), search_resumes, extract_requirements,
                       compare_candidates, generate_interview_questions
chat_engine.py         The "human feedback loop" conversational engine (LLM tool-calling,
                        with a deterministic fallback router if no LLM endpoint is reachable)
session.py             In-memory session (current requirements/shortlist) the tools read/write
config.py               .env loading + LLM client factory
milestone1/, milestone2/  Copied-in, lightly reused source from the prior milestones
                           (milestone1/fs_tools.py is now only imported by
                           filesystem_mcp_server.py, not by the agent -- see Milestone 4)
filesystem_mcp_server.py,
resume_index_mcp_server.py,
mcp_base.py, mcp_client.py    Milestone 4: MCP servers + client -- see "Milestone 4: MCP integration" below
data/                  Resumes + sample job descriptions (from Milestone 2)
vector_db/              Pre-built resume embeddings cache
reports/               Saved Markdown match reports (written via the MCP filesystem server's write_file tool)
tests/                 Smoke test, the 5 scripted conversation scenarios, and MCP server test scenarios
streamlit_app.py        Optional web chat UI (same graph, no code duplicated)
state_machine.png / .mmd  Auto-generated diagram of the compiled graph
mcp_workflow.mmd        Sequence diagram of the agent <-> MCP server interaction (Milestone 4)
TEST_SCENARIOS.md      5 real, captured conversation transcripts
```

## Setup

```bash
python -m venv venv
venv\Scripts\pip install -r requirements.txt
```

By default the agent talks to a local **Ollama** server (same setup as
Milestone 1), configured in `.env`:

```env
OPENAI_API_KEY=ollama
OPENAI_MODEL=qwen3.5:4b
OPENAI_BASE_URL=http://localhost:11434/v1
```

Start Ollama and pull a tool-calling-capable model (e.g. `ollama pull
qwen3.5:4b`) before running the chat, or point `OPENAI_BASE_URL` /
`OPENAI_MODEL` / `OPENAI_API_KEY` at OpenAI instead. **If no LLM endpoint is
reachable, the agent still works** -- the human-feedback loop falls back to a
deterministic keyword router (see "Robustness" below) so the full pipeline,
tool calls, and re-ranking can all be demoed/tested without a live model.

The resume vector index is pre-built (`vector_db/`); it's rebuilt
automatically from `data/resumes/` on first run if missing.

## Run it

```bash
venv\Scripts\python matching_agent.py       # CLI chat
venv\Scripts\streamlit run streamlit_app.py  # web chat UI
```

## Agent architecture

**State** (`state.py`): conversation history (`messages`, LangGraph's
`add_messages` reducer), the parsed job requirements, and the candidate
shortlist + reasoning across three rounds (`initial_pool`, `shortlist`,
`final_recommendations`).

**Graph** (see `state_machine.png`):

```
START -> parse_jd -> extract_requirements -> search_resumes ->
rank_candidates -> generate_report -> human_feedback
                        ^                        |
                        |-------- rerank ---------|
                        |-------- loop -----------|
                        -------- end ---> END
```

- `parse_jd` -- accepts a raw JD string or a file path (uses Milestone 1's
  `read_file` for pdf/docx/txt).
- `extract_requirements` -- `extract_requirements(jd)` tool: splits
  must-have vs nice-to-have skills and years-of-experience bars.
- `search_resumes` -- **Round 1** initial screen: hybrid semantic + keyword
  search (Milestone 2) over every indexed resume, top-K by score.
- `rank_candidates` -- **Round 2** deep analysis: re-reads each Round-1
  candidate's full resume (Milestone 1 `read_file`), computes strengths,
  gaps vs. requirements, and improvement suggestions for borderline scores.
- `generate_report` -- **Round 3** final hire / consider / no-hire verdicts,
  a full explainable Markdown report, saved to `reports/` (Milestone 1
  `write_file`).
- `human_feedback` -- pauses on a real `interrupt()` for the hiring
  manager's next message, then routes it through `chat_engine.py`: compare
  candidates, explain a ranking, generate interview questions, or (if the
  message changes hiring criteria) update requirements and loop back to
  `extract_requirements` to re-run the whole pipeline against the new bar --
  explaining what changed.

## Tools

| Tool | Source | Purpose |
|---|---|---|
| `list_files`, `read_file`, `search_in_file`, `search_in_directory`, `write_file` | Milestone 1 (via the Milestone 4 MCP server) | Inspect/search/write resume & report files |
| `search_resumes` | Milestone 2 | Hybrid semantic+keyword RAG search over the resume index |
| `extract_requirements(jd)` | New | Must-have vs nice-to-have parsing |
| `compare_candidates(candidate_ids)` | New | Head-to-head comparison table + explanation |
| `generate_interview_questions(candidate_id)` | New | Technical + gap-probing + behavioral questions |

## Robustness

The interactive loop tries an LLM + tool-calling turn first
(`chat_engine._run_llm_chat_turn`). If the endpoint is unreachable, the
model doesn't support tool calling, or any other error occurs, it falls back
to `_run_heuristic_chat_turn`, a small keyword router that still supports
comparisons, interview-question requests, and requirement refinement --
so the agent is testable/demoable even without a live model. The CLI/UI
show `_(fell back to rule-based routing: ...)_` when this happens.

## Known limitation

The sample dataset has 34 resumes (not 100) -- `top_k` in `search_resumes`
is configurable and everything downstream (rounds, verdicts, reports) scales
with whatever pool size is indexed.

## Milestone 4: MCP integration

The Milestone 1 file-system tools are no longer imported directly (`import
fs_tools`). They're served by a standalone **filesystem MCP server**
(`filesystem_mcp_server.py`) that the agent talks to over **JSON-RPC 2.0**
(newline-delimited JSON over stdio -- the standard MCP "stdio transport"),
via a small client (`mcp_client.py`). `matching_agent.py` and `tools.py` now
only ever call `get_fs_client().read_file(...)` / `.write_file(...)` / etc.
— there is no direct filesystem access left in the agent process.

The official `mcp` Python SDK requires Python >= 3.10; this project's venv
is pinned to 3.9 (see the milestone 1/2 dependencies), so the JSON-RPC 2.0
transport, tool/resource dispatch, and error handling are implemented by
hand in `mcp_base.py` + `filesystem_mcp_server.py`, following the same
method surface (`initialize`, `tools/list`, `tools/call`, `resources/list`,
`resources/read`) the official SDK exposes -- swapping in the real SDK later
would not require changing any caller.

```
filesystem_mcp_server.py   MCP server: wraps milestone1/fs_tools.py as MCP tools,
                            + watch_directory() and batch_process() (MCP-specific),
                            + resource discovery, config management, path-safety
                            sandboxing (every path is checked against allowed_roots).
resume_index_mcp_server.py  Second, independent MCP server (bonus): read-only stats/
                            queries over the Milestone 2 resume vector index
                            (index_stats, list_candidates, skill_vocabulary).
mcp_base.py                 Shared JSON-RPC 2.0 / stdio transport + error codes used
                            by both servers.
mcp_client.py                Synchronous MCP client: spawns a server subprocess, speaks
                              the same protocol, exposes get_fs_client() / get_index_client()
                              process-wide singletons.
mcp_server_config.json        Server config: allowed_roots, watch_state_file, max_batch_size.
mcp_workflow.mmd               Sequence diagram of the agent <-> MCP server interaction.
tests/test_mcp_server.py        MCP test scenarios: tool/resource discovery, a write/read/
                                  search round trip, path-safety rejection, unknown-tool error
                                  handling, batch_process (partial failure isolation),
                                  watch_directory (new-file detection), config get/set, and the
                                  bonus second server.
```

### New MCP-specific tools

- **`watch_directory(directory, extensions=None, reset=False)`** -- polling
  change-detection: each call snapshots (name, mtime, size) for a directory
  and diffs it against the snapshot from the previous call (persisted to
  `.watch_state.json`), returning new/modified/removed files. Lets the agent
  (or a scheduled job) notice new resumes dropped into `data/resumes`
  without the server needing a long-lived background thread.
- **`batch_process(operations)`** -- runs a list of `{op, params}`
  sub-operations (any tool above) in one round trip; a failure in one
  operation doesn't abort the batch -- each result carries its own
  `success`/`error`, so 50 resumes can be read in one call and you still
  know exactly which ones failed and why.

### Protocol details

- **Transport**: one JSON object per line on stdin/stdout; the server never
  writes anything else to stdout (logs would corrupt the protocol stream).
- **Error handling**: standard JSON-RPC 2.0 codes (`-32700` parse error,
  `-32600` invalid request, `-32601` method not found, `-32602` invalid
  params, `-32603` internal error) plus implementation-defined codes in the
  reserved `-32000..-32099` range for file-not-found, unsupported file type,
  path-not-allowed, unknown tool, and tool-execution errors.
- **Resource discovery**: `resources/list` returns every file under the
  configured resource directories (`data/resumes`, `data/job_descriptions`,
  `reports`) as `file://` URIs with a guessed MIME type; `resources/read`
  fetches one by URI.
- **Config management**: `config/get` returns the live server config
  (loaded from `mcp_server_config.json`); `config/set` updates a whitelisted
  key and persists it back to disk.
- **Path safety**: every tool argument that names a filesystem path is
  resolved and checked against `allowed_roots` from the config before any
  I/O happens -- an MCP client can't read or write outside `data/`,
  `reports/`, `milestone1/`, `milestone2/`.

### Run the MCP tests

```bash
venv\Scripts\python tests\test_mcp_server.py
```

## Demo video checklist (record separately)

1. Show `state_machine.png` and narrate the graph.
2. Run `matching_agent.py`, pick a sample JD, show Round 1/2/3 reasoning in
   the terminal as the pipeline runs.
3. Ask: "Compare the top 3 matches side by side."
4. Ask: "Why did `<candidate>` rank higher than `<candidate>`?"
5. Ask: "Require 5+ years of `<skill>`" and show the re-rank + explanation.
6. Ask: "Generate interview questions for `<candidate>`."
7. `quit`, and open `reports/` to show the saved report + `tools_log`-style
   traceability via the file tools.
