"""
In-memory session state shared between the LangGraph nodes and the tool
functions in tools.py.

The Part A tool signatures required by the assignment are fixed --
compare_candidates(candidate_ids: list) and
generate_interview_questions(candidate_id: str) don't take a "session"
argument -- so the tools need a place to find "the current shortlist"
without it being passed in explicitly. A single process-wide session object
does that; it's appropriate here because the agent is a single-user CLI/
Streamlit chat, not a multi-tenant service.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AgentSession:
    job_description: str = ""
    requirements: Dict[str, Any] = field(default_factory=dict)
    initial_pool: List[Dict[str, Any]] = field(default_factory=list)   # Round 1: top 10 of N
    shortlist: List[Dict[str, Any]] = field(default_factory=list)      # Round 2: deep-analyzed top 10
    final_recommendations: List[Dict[str, Any]] = field(default_factory=list)  # Round 3: hire/no-hire
    report_markdown: str = ""
    round_number: int = 0
    report_version: int = 0  # monotonically increasing, independent of round_number (which resets to 1/2/3 each pipeline pass)
    history: List[Dict[str, str]] = field(default_factory=list)  # ranking history for diffing on refinement

    def candidate_by_id(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        candidate_id_norm = candidate_id.strip().lower()
        pool = self.final_recommendations or self.shortlist or self.initial_pool
        for candidate in pool:
            if candidate["candidate_id"].lower() == candidate_id_norm:
                return candidate
            if candidate["candidate_name"].lower() == candidate_id_norm:
                return candidate
        return None

    def reset_rankings(self):
        self.initial_pool = []
        self.shortlist = []
        self.final_recommendations = []
        self.report_markdown = ""
        self.round_number = 0


# Single process-wide session instance used by both the graph nodes and tools.
SESSION = AgentSession()
