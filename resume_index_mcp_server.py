"""
Milestone 4 (Part B, bonus): a second MCP server, so the agent talks to more
than one MCP resource at once.

Exposes read-only stats/queries over the Milestone 2 resume vector index
(milestone2/resume_rag.py + job_matcher.py) -- separate process, separate
concern from the filesystem server, on purpose: this is the "database-ish"
MCP server (index_stats, list_candidates, skill_vocabulary) sitting
alongside the filesystem one, both driven by the same mcp_client.py.

    python resume_index_mcp_server.py
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "milestone1"))
sys.path.insert(0, str(PROJECT_ROOT / "milestone2"))

from mcp_base import make_base_method_dispatch, serve_forever as _serve_forever  # noqa: E402

SERVER_NAME = "resume-index-mcp-server"
SERVER_VERSION = "1.0.0"

_rag = None  # lazily built: loading the embedding model + index is expensive


def _get_rag():
    global _rag
    if _rag is None:
        from resume_rag import ResumeRAG  # Milestone 2

        _rag = ResumeRAG()
    return _rag


def tool_index_stats(config, arguments):
    rag = _get_rag()
    return {"collection": rag.collection.collection_name if hasattr(rag.collection, "collection_name") else "resumes",
            "chunks_indexed": rag.count()}


def tool_list_candidates(config, arguments):
    from resume_rag import load_resumes, extract_metadata  # Milestone 2

    limit = arguments.get("limit", 50)
    resumes = load_resumes()[:limit]
    candidates = []
    for resume in resumes:
        metadata = extract_metadata(resume["text"], resume["filename"])
        candidates.append({
            "candidate_id": Path(resume["path"]).stem,
            "name": metadata["name"],
            "skills": metadata["skills"],
            "experience_years": metadata["experience_years"],
            "education": metadata["education"],
        })
    return {"count": len(candidates), "candidates": candidates}


def tool_skill_vocabulary(config, arguments):
    from resume_rag import SKILL_VOCABULARY  # Milestone 2

    return {"count": len(SKILL_VOCABULARY), "skills": SKILL_VOCABULARY}


TOOL_DISPATCH = {
    "index_stats": tool_index_stats,
    "list_candidates": tool_list_candidates,
    "skill_vocabulary": tool_skill_vocabulary,
}

TOOL_SCHEMAS = [
    {
        "name": "index_stats",
        "description": "Return the number of resume chunks currently indexed in the vector store.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_candidates",
        "description": "List candidates in the resume pool with basic extracted metadata.",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 50}}},
    },
    {
        "name": "skill_vocabulary",
        "description": "The fixed skill vocabulary used for keyword/skill extraction.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

METHOD_DISPATCH = make_base_method_dispatch(SERVER_NAME, SERVER_VERSION, TOOL_SCHEMAS, TOOL_DISPATCH)


def serve_forever(stdin=None, stdout=None):
    _serve_forever({}, METHOD_DISPATCH, stdin=stdin, stdout=stdout)


if __name__ == "__main__":
    serve_forever()
