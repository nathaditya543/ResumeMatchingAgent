"""LangGraph agent state definition."""
from typing import Annotated, Any, Dict, List, Optional, TypedDict

from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    # 1. Conversation history (LangGraph's built-in reducer appends new messages).
    messages: Annotated[List[Any], add_messages]

    # 2. Job requirements understanding.
    job_description: str
    requirements: Dict[str, Any]

    # 3. Candidate shortlist + reasoning, across the three screening rounds.
    initial_pool: List[Dict[str, Any]]          # Round 1: top-K of the full resume pool
    shortlist: List[Dict[str, Any]]              # Round 2: deep-analyzed shortlist
    final_recommendations: List[Dict[str, Any]]  # Round 3: hire / no-hire verdicts
    report_markdown: str
    round_number: int

    # Control flow for the human-feedback loop.
    should_exit: bool
    needs_rerank: bool
    last_ranking_snapshot: Optional[List[Dict[str, Any]]]
