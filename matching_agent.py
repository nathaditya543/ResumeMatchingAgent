"""
Milestone 3: LangGraph resume-matching agent.

As of Milestone 4, all file access (parse_jd, rank_candidates,
generate_report) goes through the filesystem MCP server
(filesystem_mcp_server.py) via mcp_client.get_fs_client() -- this module no
longer imports fs_tools.py or touches the filesystem directly.

Graph:

    START -> parse_jd -> extract_requirements -> search_resumes ->
    rank_candidates -> generate_report -> human_feedback_loop -> END
                              ^                        |
                              |____ (refinement) _______|

- parse_jd:             load the job description (raw text or a file path).
- extract_requirements: must-have vs nice-to-have skills + experience bars.
- search_resumes:       Round 1 -- semantic+keyword RAG search, top-K of the pool.
- rank_candidates:      Round 2 -- deep per-candidate analysis (strengths/gaps).
- generate_report:      Round 3 -- hire/no-hire verdicts + saved Markdown report.
- human_feedback_loop:  interactive chat (compare, explain, interview questions,
                         iterative requirement refinement) that loops until the
                         user is done, re-entering the pipeline at
                         extract_requirements whenever criteria change.

Run directly for a CLI chat: `python matching_agent.py`
"""
import re
import sys
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from chat_engine import run_chat_turn
from config import REPORTS_DIR
from mcp_client import get_fs_client  # Milestone 4: file access goes through the MCP server
from session import SESSION
from state import AgentState
from tools import extract_requirements, search_resumes  # also puts milestone2 on sys.path

# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------

def parse_jd_node(state: AgentState) -> dict:
    """Accept either raw job-description text or a path to a .txt/.pdf/.docx file."""
    jd_input = state["job_description"]
    path = Path(jd_input)

    if path.exists() and path.is_file():
        jd_text = get_fs_client().read_file(str(path))["text"]
    else:
        jd_text = jd_input

    SESSION.job_description = jd_text
    return {
        "job_description": jd_text,
        "messages": [HumanMessage(content=f"[System] Job description loaded ({len(jd_text)} characters).")],
    }


def extract_requirements_node(state: AgentState) -> dict:
    summary = extract_requirements.invoke({"jd": state["job_description"]})
    return {
        "requirements": dict(SESSION.requirements),
        "messages": [AIMessage(content=summary)],
    }


def search_resumes_node(state: AgentState) -> dict:
    """Round 1: initial screen -- top-K of the full resume pool."""
    summary = search_resumes.invoke({"job_description": state["job_description"], "top_k": 10})
    return {
        "initial_pool": list(SESSION.initial_pool),
        "round_number": 1,
        "messages": [AIMessage(content="Round 1 - Initial screen:\n" + summary)],
    }


def rank_candidates_node(state: AgentState) -> dict:
    """Round 2: deep analysis of the Round 1 shortlist -- read full resumes,
    compute strengths/gaps against the extracted requirements, and flag
    improvement suggestions for borderline candidates."""
    required_skills = set(SESSION.requirements.get("must_have_skills", [])) | set(
        SESSION.requirements.get("nice_to_have_skills", [])
    )

    deep_shortlist = []
    for candidate in state["initial_pool"]:
        try:
            full_text = get_fs_client().read_file(candidate["resume_path"])["text"]
        except Exception:
            full_text = ""

        matched = set(candidate["matched_skills"])
        gaps = sorted(required_skills - matched)

        improvement_suggestions = []
        if gaps and 40 <= candidate["match_score"] < 70:
            improvement_suggestions.append(
                f"Borderline match -- would rank higher with evidence of: {', '.join(gaps)}"
            )
        if not candidate["meets_must_haves"]:
            improvement_suggestions.append("Does not clearly meet one or more must-have requirements from the JD.")
        if not gaps and candidate["match_score"] < 70:
            improvement_suggestions.append("Has the required skills but resume lacks depth/detail -- verify in interview.")

        enriched = dict(candidate)
        enriched["gaps"] = gaps
        enriched["improvement_suggestions"] = improvement_suggestions
        enriched["resume_excerpt"] = full_text[:500].strip()
        deep_shortlist.append(enriched)

    deep_shortlist.sort(key=lambda c: (not c["meets_must_haves"], -c["match_score"]))
    SESSION.shortlist = deep_shortlist

    lines = [f"Round 2 - Deep analysis of top {len(deep_shortlist)}:"]
    for c in deep_shortlist:
        lines.append(f"  {c['candidate_name']} [{c['candidate_id']}]: gaps={c['gaps'] or 'none'}")

    return {
        "shortlist": deep_shortlist,
        "round_number": 2,
        "messages": [AIMessage(content="\n".join(lines))],
    }


def _verdict_for(candidate: dict) -> str:
    if candidate["meets_must_haves"] and candidate["match_score"] >= 70:
        return "Hire"
    if candidate["match_score"] >= 50:
        return "Consider (borderline)"
    return "No Hire"


def generate_report_node(state: AgentState) -> dict:
    """Round 3: final hire/no-hire recommendations + a saved, explainable Markdown report."""
    recommendations = []
    for candidate in state["shortlist"]:
        enriched = dict(candidate)
        enriched["verdict"] = _verdict_for(candidate)
        recommendations.append(enriched)
    SESSION.final_recommendations = recommendations

    lines = [
        "# Candidate Match Report",
        "",
        "## Job Description",
        "",
        state["job_description"].strip(),
        "",
        "## Requirements",
        "",
        f"- Must-have skills: {', '.join(state['requirements'].get('must_have_skills', [])) or 'none detected'}",
        f"- Nice-to-have skills: {', '.join(state['requirements'].get('nice_to_have_skills', [])) or 'none detected'}",
        f"- Experience requirements: {', '.join(state['requirements'].get('experience_requirements', [])) or 'none detected'}",
        "",
        "## Ranked Candidates",
        "",
    ]
    for candidate in recommendations:
        lines.append(f"### {candidate['candidate_name']} ({candidate['candidate_id']}) — **{candidate['verdict']}**")
        lines.append("")
        lines.append(f"- Match score: {candidate['match_score']}/100")
        lines.append(f"- Meets must-have requirements: {candidate['meets_must_haves']}")
        lines.append(f"- Experience: {candidate['experience_years']} years, Education: {candidate['education']}")
        lines.append(f"- Matched skills (strengths): {', '.join(candidate['matched_skills']) or 'none'}")
        lines.append(f"- Gaps: {', '.join(candidate['gaps']) or 'none'}")
        if candidate["improvement_suggestions"]:
            lines.append(f"- Improvement suggestions: {' '.join(candidate['improvement_suggestions'])}")
        lines.append(f"- Reasoning: {candidate['reasoning']}")
        lines.append("")

    report_markdown = "\n".join(lines)
    SESSION.report_markdown = report_markdown

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "_", state["job_description"][:40].lower()).strip("_") or "report"
    SESSION.report_version += 1
    # The MCP filesystem server's write_file only supports .txt/.docx (Milestone 1's writable types); the
    # content is still Markdown, just saved with a .txt extension.
    report_path = REPORTS_DIR / f"{slug}_v{SESSION.report_version}.txt"
    get_fs_client().write_file(str(report_path), report_markdown)

    return {
        "final_recommendations": recommendations,
        "report_markdown": report_markdown,
        "round_number": 3,
        "last_ranking_snapshot": [
            {"candidate_id": c["candidate_id"], "match_score": c["match_score"], "verdict": c["verdict"]}
            for c in recommendations
        ],
        "messages": [AIMessage(content=f"Round 3 - Final recommendations generated. Full report saved to {report_path}.")],
    }


EXIT_PATTERN = re.compile(r"^\s*(quit|exit|done|stop|bye)\s*[.!]?\s*$", re.IGNORECASE)


def human_feedback_node(state: AgentState) -> dict:
    """Pauses the graph for a real human turn, then routes the response
    through the conversational tool-using chat engine (compare candidates,
    explain rankings, generate interview questions, or refine requirements)."""
    user_input = interrupt(
        {
            "prompt": "Awaiting hiring-manager feedback",
            "report_markdown": state.get("report_markdown", ""),
        }
    )

    if EXIT_PATTERN.match(user_input or ""):
        return {
            "messages": [HumanMessage(content=user_input), AIMessage(content="Ending the session. Good luck with the hire!")],
            "should_exit": True,
            "needs_rerank": False,
        }

    answer, needs_rerank, new_messages = run_chat_turn(user_input, state["messages"])

    result = {
        "messages": new_messages,
        "should_exit": False,
        "needs_rerank": needs_rerank,
    }
    if needs_rerank:
        result["job_description"] = SESSION.job_description
    return result


def route_after_feedback(state: AgentState) -> str:
    if state["should_exit"]:
        return "end"
    if state["needs_rerank"]:
        return "rerank"
    return "loop"


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_graph():
    workflow = StateGraph(AgentState)

    workflow.add_node("parse_jd", parse_jd_node)
    workflow.add_node("extract_requirements", extract_requirements_node)
    workflow.add_node("search_resumes", search_resumes_node)
    workflow.add_node("rank_candidates", rank_candidates_node)
    workflow.add_node("generate_report", generate_report_node)
    workflow.add_node("human_feedback", human_feedback_node)

    workflow.add_edge(START, "parse_jd")
    workflow.add_edge("parse_jd", "extract_requirements")
    workflow.add_edge("extract_requirements", "search_resumes")
    workflow.add_edge("search_resumes", "rank_candidates")
    workflow.add_edge("rank_candidates", "generate_report")
    workflow.add_edge("generate_report", "human_feedback")

    workflow.add_conditional_edges(
        "human_feedback",
        route_after_feedback,
        {"end": END, "rerank": "extract_requirements", "loop": "human_feedback"},
    )

    return workflow.compile(checkpointer=MemorySaver())


def initial_state(job_description: str) -> AgentState:
    return {
        "messages": [],
        "job_description": job_description,
        "requirements": {},
        "initial_pool": [],
        "shortlist": [],
        "final_recommendations": [],
        "report_markdown": "",
        "round_number": 0,
        "should_exit": False,
        "needs_rerank": False,
        "last_ranking_snapshot": None,
    }


# ---------------------------------------------------------------------------
# CLI driver
# ---------------------------------------------------------------------------

def _choose_job_description() -> str:
    from config import JD_DIR

    samples = sorted(JD_DIR.glob("*.txt")) if JD_DIR.exists() else []
    print("=== Resume Matching Agent ===")
    if samples:
        print("Sample job descriptions:")
        for i, sample in enumerate(samples, 1):
            print(f"  {i}. {sample.name}")
        print("Enter a number to use a sample, a file path, or paste JD text directly.")

    raw = input("\nJob description: ").strip()
    if raw.isdigit() and samples and 1 <= int(raw) <= len(samples):
        return str(samples[int(raw) - 1])
    return raw


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to a non-UTF-8 codepage.

    graph = build_graph()
    config = {"configurable": {"thread_id": "cli-session"}, "recursion_limit": 100}

    jd_input = _choose_job_description()
    state = initial_state(jd_input)

    print("\nRunning screening pipeline (parse JD -> extract requirements -> search -> rank -> report)...\n")
    result = graph.invoke(state, config)
    print(result.get("report_markdown", ""))

    print("\n--- You can now chat with the agent. Try things like: ---")
    print('  "Compare the top 3 matches side by side"')
    print('  "Why did <candidate> rank higher than <candidate>?"')
    print('  "Require 3+ years of React"')
    print('  "Generate interview questions for <candidate>"')
    print("  Type 'quit' to finish.\n")

    while True:
        snapshot = graph.get_state(config)
        if not snapshot.next:
            break

        user_text = input("You: ").strip()
        if not user_text:
            continue

        result = graph.invoke(Command(resume=user_text), config)

        ai_messages = [m for m in result["messages"] if isinstance(m, AIMessage)]
        if ai_messages:
            print(f"\nAgent: {ai_messages[-1].content}\n")


if __name__ == "__main__":
    main()
