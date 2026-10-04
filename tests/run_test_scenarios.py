"""
Runs 5+ scripted conversation flows against the real compiled graph (each on
its own thread_id, i.e. its own independent session) and writes the full
transcripts to TEST_SCENARIOS.md. Nothing here is hand-written output --
every line is what the graph actually produced, so this doubles as an
integration test and as the assignment's required "5+ conversation flows"
deliverable.

Run with: venv/Scripts/python.exe tests/run_test_scenarios.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import AIMessage
from langgraph.types import Command

from matching_agent import build_graph, initial_state

PROJECT_ROOT = Path(__file__).resolve().parent.parent
JD_DIR = PROJECT_ROOT / "data" / "job_descriptions"
OUT_PATH = PROJECT_ROOT / "TEST_SCENARIOS.md"


def run_scenario(graph, thread_id, title, jd_source, turns):
    """turns: list of either a literal string, or a callable(state) -> str
    (used when a turn needs to reference the actual top candidate ids)."""
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
    lines = [f"## {title}", "", f"**Thread id:** `{thread_id}`", ""]

    lines.append("**Job description:**")
    lines.append("```")
    jd_text = jd_source if not Path(jd_source).exists() else Path(jd_source).read_text(encoding="utf-8")
    lines.append(jd_text.strip())
    lines.append("```")
    lines.append("")

    state = graph.invoke(initial_state(jd_source), config)
    lines.append("**Initial pipeline output (Round 1 -> 2 -> 3):**")
    lines.append("")
    lines.append(state["report_markdown"])
    lines.append("")
    last_report = state["report_markdown"]

    for turn in turns:
        user_text = turn(state) if callable(turn) else turn
        lines.append(f"**User:** {user_text}")
        lines.append("")
        state = graph.invoke(Command(resume=user_text), config)
        ai_messages = [m for m in state["messages"] if isinstance(m, AIMessage)]
        answer = ai_messages[-1].content if ai_messages else "(no response)"
        lines.append(f"**Agent:** {answer}")
        lines.append("")
        if state.get("report_markdown") and state["report_markdown"] != last_report:
            last_report = state["report_markdown"]
            lines.append("**(requirements changed -> pipeline re-ran; updated report:)**")
            lines.append("")
            lines.append(last_report)
            lines.append("")

    graph.invoke(Command(resume="quit"), config)
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def top_two_ids(state):
    top = state["final_recommendations"][:2]
    return top[0]["candidate_id"], top[1]["candidate_id"]


def main():
    graph = build_graph()
    sections = ["# Test Scenarios", "", "Five scripted conversation flows run against the real LangGraph agent.", ""]

    # --- Scenario 1: ad-hoc natural-language search (no JD file) -----------
    sections.append(
        run_scenario(
            graph,
            "scenario-1-nl-search",
            "Scenario 1 -- Natural-language candidate search",
            "Looking for a frontend engineer with React and at least 3 years of experience, "
            "who also knows TypeScript and Redux.",
            [
                "Compare the top 3 matches side by side",
            ],
        )
    )

    # --- Scenario 2: explainability (why did X rank above Y) ---------------
    sections.append(
        run_scenario(
            graph,
            "scenario-2-explainability",
            "Scenario 2 -- Explainability (\"why did X rank higher than Y?\")",
            str(JD_DIR / "jd_01_backend_python_engineer.txt"),
            [
                lambda state: f"Why did {top_two_ids(state)[0]} rank higher than {top_two_ids(state)[1]}?",
            ],
        )
    )

    # --- Scenario 3: iterative refinement mid-conversation ------------------
    sections.append(
        run_scenario(
            graph,
            "scenario-3-refinement",
            "Scenario 3 -- Iterative requirement refinement",
            str(JD_DIR / "jd_04_devops_cloud_engineer.txt"),
            [
                "Actually, make Terraform a hard requirement, not just nice-to-have.",
                "Now compare the top 2 candidates given the new requirement.",
            ],
        )
    )

    # --- Scenario 4: interview question generation --------------------------
    sections.append(
        run_scenario(
            graph,
            "scenario-4-interview-questions",
            "Scenario 4 -- Interview question generation",
            str(JD_DIR / "jd_02_machine_learning_engineer.txt"),
            [
                lambda state: f"Generate interview questions for {state['final_recommendations'][0]['candidate_id']}",
            ],
        )
    )

    # --- Scenario 5: multi-turn end-to-end screening -------------------------
    sections.append(
        run_scenario(
            graph,
            "scenario-5-multi-turn",
            "Scenario 5 -- Multi-round screening in one conversation",
            str(JD_DIR / "jd_05_cybersecurity_analyst.txt"),
            [
                "What's the biggest gap for the #2 ranked candidate?",
                "Require 4+ years of Penetration Testing experience.",
                lambda state: f"Generate interview questions for {state['final_recommendations'][0]['candidate_id']}",
            ],
        )
    )

    OUT_PATH.write_text("\n".join(sections), encoding="utf-8")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
