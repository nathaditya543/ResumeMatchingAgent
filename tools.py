"""
Tools available to the LangGraph agent:

- Milestone 1 file-system tools, now served by filesystem_mcp_server.py
  (Milestone 4) and called through mcp_client.py -- list_files, read_file,
  search_in_file, search_in_directory, write_file no longer import
  fs_tools.py directly, they go over a real MCP (JSON-RPC/stdio) call.
- Milestone 2 RAG search (resume_rag.py / job_matcher.py): search_resumes.
- New Milestone 3 tools: extract_requirements, compare_candidates,
  generate_interview_questions.

All tools are plain, LLM-tool-callable functions (decorated with @tool) so
they can be bound directly to a langchain chat model, and are also called
directly by the graph nodes in matching_agent.py.
"""
import re
import sys
from pathlib import Path
from typing import List, Optional

from langchain_core.tools import tool

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "milestone2"))

from mcp_client import MCPError, get_fs_client  # Milestone 4: MCP client, not fs_tools
from resume_rag import ResumeRAG, SKILL_VOCABULARY, skill_matches  # Milestone 2
from job_matcher import JobMatcher, extract_required_skills, extract_must_haves  # Milestone 2

from session import SESSION

# ---------------------------------------------------------------------------
# Lazily-built singletons: loading the embedding model + resume index is the
# expensive part of startup, so build once and reuse across the whole chat.
# ---------------------------------------------------------------------------
_job_matcher: Optional[JobMatcher] = None


def get_job_matcher() -> JobMatcher:
    global _job_matcher
    if _job_matcher is None:
        rag = ResumeRAG()
        if rag.count() == 0:
            rag.build_index()
        _job_matcher = JobMatcher(rag=rag)
    return _job_matcher


# ---------------------------------------------------------------------------
# Milestone 1 (via Milestone 4's MCP server): file-system tools, now thin
# @tool wrappers around mcp_client.get_fs_client() instead of fs_tools.py.
# ---------------------------------------------------------------------------
@tool
def list_files(directory: str, extension: Optional[str] = None) -> str:
    """List files in a directory, optionally filtered by extension (e.g. 'pdf', 'txt')."""
    try:
        result = get_fs_client().list_files(directory, extension=extension)
        return "\n".join(result["files"]) if result["files"] else "(no files found)"
    except MCPError as error:
        return f"Error: {error.message}"


@tool
def read_file(path: str) -> str:
    """Read text content from a .txt, .pdf, or .docx file and return it with basic metadata."""
    try:
        result = get_fs_client().read_file(path)
        return f"[{result['metadata']['name']}, {result['word_count']} words]\n{result['text']}"
    except MCPError as error:
        return f"Error: {error.message}"


@tool
def search_in_file(filepath: str, keyword: str) -> str:
    """Search for a keyword inside a single txt/pdf/docx file and return matches with context."""
    try:
        result = get_fs_client().search_in_file(filepath, keyword)
        if result["match_count"] == 0:
            return f"No matches for '{keyword}' in {filepath}"
        lines = [f"{result['match_count']} match(es) for '{keyword}' in {filepath}:"]
        for match in result["matches"][:10]:
            lines.append(f"  ...{match['full_match']}...")
        return "\n".join(lines)
    except MCPError as error:
        return f"Error: {error.message}"


@tool
def search_in_directory(directory: str, keyword: str) -> str:
    """Search all readable files in a directory for a keyword; returns matches grouped by file."""
    try:
        result = get_fs_client().search_in_directory(directory, keyword)
        lines = [
            f"Searched {result['searched_files']} files, "
            f"{result['matched_files']} contained '{keyword}' ({result['total_matches']} matches total)."
        ]
        for file_result in result["results"]:
            lines.append(f"  - {file_result['metadata']['name']}: {file_result['match_count']} match(es)")
        return "\n".join(lines)
    except MCPError as error:
        return f"Error: {error.message}"


@tool
def write_file(path: str, content: str, overwrite: bool = True) -> str:
    """Write text content to a .txt or .docx file (PDFs cannot be written)."""
    try:
        result = get_fs_client().write_file(path, content, overwrite=overwrite)
        return result["message"] + f" -> {path}"
    except MCPError as error:
        return f"Error: {error.message}"


# ---------------------------------------------------------------------------
# Milestone 2: RAG search over the resume vector store
# ---------------------------------------------------------------------------
@tool
def search_resumes(job_description: str, top_k: int = 10) -> str:
    """Semantic + keyword hybrid search over all indexed resumes for a job description.
    Returns the top_k candidates ranked by match score, with matched skills and reasoning."""
    matcher = get_job_matcher()
    result = matcher.match(job_description, top_k=top_k)
    SESSION.job_description = job_description
    SESSION.initial_pool = _annotate_candidate_ids(result["top_matches"])
    lines = [f"Top {len(SESSION.initial_pool)} candidates (of {matcher.rag.count()} resume chunks indexed):"]
    for candidate in SESSION.initial_pool:
        lines.append(
            f"  [{candidate['candidate_id']}] {candidate['candidate_name']} - "
            f"score {candidate['match_score']}, meets_must_haves={candidate['meets_must_haves']}"
        )
    return "\n".join(lines)


def _annotate_candidate_ids(matches: List[dict]) -> List[dict]:
    """Give each match a short, stable candidate_id derived from its resume filename."""
    annotated = []
    for match in matches:
        stem = Path(match["resume_path"]).stem
        candidate = dict(match)
        candidate["candidate_id"] = stem
        annotated.append(candidate)
    return annotated


# ---------------------------------------------------------------------------
# Milestone 3: extract_requirements
# ---------------------------------------------------------------------------
NICE_TO_HAVE_HEADERS = re.compile(
    r"(?im)^\s*(nice[\s-]to[\s-]have|preferred|bonus|good[\s-]to[\s-]have|plus(?:es)?)\s*:?\s*$"
)


def _split_jd_sections(jd_text: str):
    """Split a JD into (main_text, nice_to_have_text) using a 'Nice to have' /
    'Preferred' style header as the boundary, if present."""
    match = NICE_TO_HAVE_HEADERS.search(jd_text)
    if not match:
        return jd_text, ""
    return jd_text[: match.start()], jd_text[match.end():]


@tool
def extract_requirements(jd: str) -> str:
    """Parse a job description into must-have vs nice-to-have skills and experience
    requirements. Updates the agent's current requirements understanding."""
    main_text, nice_text = _split_jd_sections(jd)

    all_skills = extract_required_skills(jd)
    nice_skills = extract_required_skills(nice_text) if nice_text else []
    must_skills = [s for s in all_skills if s not in nice_skills]
    must_haves_years = extract_must_haves(main_text)

    requirements = {
        "must_have_skills": must_skills,
        "nice_to_have_skills": nice_skills,
        "experience_requirements": [
            (f"{m['years']}+ years" + (f" of {m['skill']}" if m["skill"] else " of experience"))
            for m in must_haves_years
        ],
        "all_skills_mentioned": all_skills,
    }
    SESSION.requirements = requirements
    SESSION.job_description = jd

    lines = ["Requirements extracted:"]
    lines.append(f"  Must-have skills: {', '.join(must_skills) or '(none detected)'}")
    lines.append(f"  Nice-to-have skills: {', '.join(nice_skills) or '(none detected)'}")
    lines.append(f"  Experience requirements: {', '.join(requirements['experience_requirements']) or '(none detected)'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Milestone 3: compare_candidates
# ---------------------------------------------------------------------------
@tool
def compare_candidates(candidate_ids: List[str]) -> str:
    """Head-to-head comparison of two or more candidates already in the current
    shortlist. Pass candidate_ids as returned by search_resumes (filename stems)
    or candidate names."""
    candidates = []
    missing = []
    for candidate_id in candidate_ids:
        candidate = SESSION.candidate_by_id(candidate_id)
        if candidate:
            candidates.append(candidate)
        else:
            missing.append(candidate_id)

    if not candidates:
        return f"No matching candidates found for: {', '.join(candidate_ids)}. Run search_resumes first."

    header = f"{'Candidate':<25}{'Score':<8}{'Meets Must-Haves':<18}{'Experience':<12}{'Matched Skills'}"
    lines = [header, "-" * len(header)]
    for candidate in sorted(candidates, key=lambda c: -c["match_score"]):
        lines.append(
            f"{candidate['candidate_name']:<25}{candidate['match_score']:<8}"
            f"{str(candidate['meets_must_haves']):<18}{candidate['experience_years']:<12}"
            f"{', '.join(candidate['matched_skills']) or '(none)'}"
        )

    winner = max(candidates, key=lambda c: c["match_score"])
    lines.append("")
    lines.append(f"{winner['candidate_name']} ranks highest ({winner['match_score']}) because: {winner['reasoning']}")
    for candidate in candidates:
        if candidate is not winner:
            lines.append(f"{candidate['candidate_name']} ({candidate['match_score']}): {candidate['reasoning']}")

    if missing:
        lines.append(f"\n(Could not find: {', '.join(missing)})")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Milestone 3: generate_interview_questions
# ---------------------------------------------------------------------------
TECHNICAL_QUESTION_TEMPLATES = {
    "Python": "Walk me through a recent Python project where you had to optimize performance. What did you profile and change?",
    "Django": "How do you structure a Django project for a service with several related apps, and how do you handle migrations safely in production?",
    "FastAPI": "How would you design async request handling and dependency injection in a FastAPI service under high load?",
    "React": "Describe how you manage state in a non-trivial React app. When do you reach for Context vs Redux vs local state?",
    "Kubernetes": "Describe a production incident involving a Kubernetes deployment. How did you diagnose and resolve it?",
    "Docker": "How do you keep Docker images small and secure for a production service?",
    "AWS": "Walk me through the AWS services you'd use to deploy a scalable, resilient web API, and why.",
    "Machine Learning": "Describe a model you took from prototype to production. What broke, and how did you handle drift/monitoring?",
    "SQL": "Given a slow query on a large table, how would you diagnose and fix it?",
}
DEFAULT_TECHNICAL_QUESTION = "Tell me about a project where you used {skill}. What was the hardest part?"


@tool
def generate_interview_questions(candidate_id: str) -> str:
    """Generate a set of screening interview questions for one candidate,
    based on their matched skills (to verify depth) and requirement gaps
    (to probe risk areas)."""
    candidate = SESSION.candidate_by_id(candidate_id)
    if not candidate:
        return f"No candidate found for id/name '{candidate_id}'. Run search_resumes first."

    matched = candidate.get("matched_skills", [])
    required_skills = set(SESSION.requirements.get("must_have_skills", [])) | set(
        SESSION.requirements.get("nice_to_have_skills", [])
    )
    gaps = sorted(required_skills - set(matched))

    questions = [f"Interview questions for {candidate['candidate_name']} ({candidate_id}):", "", "Technical (verify strengths):"]
    for skill in matched[:4] or ["their strongest listed skill"]:
        template = TECHNICAL_QUESTION_TEMPLATES.get(skill, DEFAULT_TECHNICAL_QUESTION.format(skill=skill))
        questions.append(f"  - {template}")

    if gaps:
        questions.append("")
        questions.append("Gap-probing (requirements not clearly evidenced in the resume):")
        for skill in gaps[:4]:
            questions.append(f"  - The role needs {skill}; have you used it in production, and how recently?")

    questions.append("")
    questions.append("Behavioral:")
    questions.append("  - Tell me about a time you disagreed with a technical decision. What did you do?")
    questions.append(f"  - Why are you interested in this role, given your background in {matched[0] if matched else 'your field'}?")

    return "\n".join(questions)


ALL_TOOLS = [
    list_files,
    read_file,
    search_in_file,
    search_in_directory,
    write_file,
    search_resumes,
    extract_requirements,
    compare_candidates,
    generate_interview_questions,
]
