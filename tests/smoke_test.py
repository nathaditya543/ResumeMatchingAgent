"""Non-interactive smoke test: run the pipeline to the first pause, then feed
a scripted list of human turns through Command(resume=...), printing what the
agent says at each step. Not a unit test -- a quick way to sanity-check the
whole graph without typing into a terminal."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import AIMessage
from langgraph.types import Command

from matching_agent import build_graph, initial_state

JD_PATH = str(Path(__file__).resolve().parent.parent / "data" / "job_descriptions" / "jd_01_backend_python_engineer.txt")

SCRIPTED_TURNS = [
    "Compare the top 3 matches side by side",
    None,  # filled in after we see the report, to reference a real candidate id
    "Require 5+ years of React",
    "quit",
]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    graph = build_graph()
    config = {"configurable": {"thread_id": "smoke-test"}, "recursion_limit": 100}

    result = graph.invoke(initial_state(JD_PATH), config)
    print("=" * 80)
    print("REPORT AFTER INITIAL PIPELINE")
    print("=" * 80)
    print(result["report_markdown"])

    top_candidates = result["final_recommendations"][:2]
    assert top_candidates, "expected at least one ranked candidate"
    names = " and ".join(c["candidate_id"] for c in top_candidates)
    SCRIPTED_TURNS[1] = f"Why did {names.split(' and ')[0]} rank higher than {names.split(' and ')[-1]}?"

    for turn in SCRIPTED_TURNS:
        print("\n" + "=" * 80)
        print(f"USER: {turn}")
        print("=" * 80)
        result = graph.invoke(Command(resume=turn), config)
        ai_messages = [m for m in result["messages"] if isinstance(m, AIMessage)]
        if ai_messages:
            print(f"AGENT: {ai_messages[-1].content}")

        snapshot = graph.get_state(config)
        if not snapshot.next:
            print("\n[graph reached END]")
            break

    print("\nSmoke test completed without exceptions.")


if __name__ == "__main__":
    main()
