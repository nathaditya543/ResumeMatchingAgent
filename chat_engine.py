"""
The conversational engine used inside the "Human Feedback Loop" node.

Tries an LLM + tool-calling (ReAct-style) turn first. If the LLM endpoint is
unavailable (e.g. Ollama isn't running) it falls back to a small deterministic
keyword router so the agent's interactive features still work end-to-end for
testing/demoing without a live model.
"""
import re
from typing import List, Tuple

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from config import get_llm
from session import SESSION
from tools import ALL_TOOLS, compare_candidates, extract_requirements, generate_interview_questions

MAX_TOOL_ROUNDS = 6
REFINEMENT_TOOL_NAMES = {"extract_requirements"}

TOOL_MAP = {t.name: t for t in ALL_TOOLS}

CHAT_SYSTEM_PROMPT = """
You are a technical recruiting copilot helping a hiring manager screen resume
candidates for a single open role. You already ran an initial screening
pipeline; the results are summarized in the session context given with each
user message.

Use tools when they help:
- compare_candidates: head-to-head comparison, or to explain why one
  candidate ranked above another. Pass candidate_id values shown in the
  session context (or candidate names).
- generate_interview_questions: when asked for screening questions for a
  specific candidate.
- extract_requirements: call this WHENEVER the user asks to change hiring
  criteria (add/remove a required skill, change must-have vs nice-to-have,
  change a years-of-experience bar, or gives a brand new job description).
  Build the full updated job description text yourself (start from the
  current job description in the session context and edit it to reflect the
  requested change) and pass that as the `jd` argument. After calling it,
  tell the user you're re-ranking candidates against the new criteria.
- search_in_directory / search_in_file / read_file / list_files: to look at
  raw resume files on disk if useful.

Keep answers concise and specific to the candidates in the session context.
""".strip()


def _session_context_block() -> str:
    lines = ["[Current session context]"]
    if SESSION.job_description:
        lines.append(f"Job description:\n{SESSION.job_description.strip()}\n")
    if SESSION.requirements:
        lines.append(f"Must-have skills: {', '.join(SESSION.requirements.get('must_have_skills', [])) or '(none)'}")
        lines.append(f"Nice-to-have skills: {', '.join(SESSION.requirements.get('nice_to_have_skills', [])) or '(none)'}")
        exp = SESSION.requirements.get("experience_requirements", [])
        lines.append(f"Experience requirements: {', '.join(exp) or '(none)'}")
    pool = SESSION.final_recommendations or SESSION.shortlist or SESSION.initial_pool
    if pool:
        lines.append("\nCurrent ranked candidates:")
        for c in pool:
            verdict = f", verdict={c['verdict']}" if "verdict" in c else ""
            lines.append(
                f"  [{c['candidate_id']}] {c['candidate_name']} - score={c['match_score']}, "
                f"meets_must_haves={c['meets_must_haves']}{verdict}"
            )
    return "\n".join(lines)


def _run_llm_chat_turn(user_input: str, history: List) -> Tuple[str, bool, List]:
    llm = get_llm().bind_tools(ALL_TOOLS)

    messages = list(history)
    if not messages or not isinstance(messages[0], SystemMessage):
        messages = [SystemMessage(content=CHAT_SYSTEM_PROMPT)] + messages

    contextualized_input = f"{_session_context_block()}\n\n[User message]\n{user_input}"
    messages.append(HumanMessage(content=contextualized_input))

    new_messages = [HumanMessage(content=user_input)]  # store the clean version in history
    refine = False

    for _ in range(MAX_TOOL_ROUNDS):
        ai_message: AIMessage = llm.invoke(messages)
        messages.append(ai_message)
        new_messages.append(ai_message)

        if not ai_message.tool_calls:
            return ai_message.content, refine, new_messages

        for tool_call in ai_message.tool_calls:
            name = tool_call["name"]
            args = tool_call.get("args", {})
            if name in REFINEMENT_TOOL_NAMES:
                refine = True
            tool_fn = TOOL_MAP.get(name)
            result = tool_fn.invoke(args) if tool_fn else f"Unknown tool: {name}"
            tool_message = ToolMessage(content=str(result), tool_call_id=tool_call["id"])
            messages.append(tool_message)
            new_messages.append(tool_message)

    fallback = "I ran out of reasoning steps before finishing. Could you rephrase or narrow the request?"
    new_messages.append(AIMessage(content=fallback))
    return fallback, refine, new_messages


# ---------------------------------------------------------------------------
# Deterministic fallback router (used if the LLM endpoint is unreachable)
# ---------------------------------------------------------------------------
REFINEMENT_KEYWORDS = (
    "must have", "must-have", "require", "requires", "requirement",
    "nice to have", "nice-to-have", "instead of", "at least", "years of",
    "add ", "remove ", "drop ", "change the", "raise the bar", "lower the bar",
    "new job description", "make it a requirement",
)
INTERVIEW_KEYWORDS = ("interview question", "screening question", "questions for")
COMPARE_KEYWORDS = ("compare", "side by side", "why did", "why does", "rank higher", "ranked higher")


def _find_candidate_ids(text: str) -> List[str]:
    pool = SESSION.final_recommendations or SESSION.shortlist or SESSION.initial_pool
    found = []
    text_lower = text.lower()
    for candidate in pool:
        if candidate["candidate_id"].lower() in text_lower or candidate["candidate_name"].lower() in text_lower:
            found.append(candidate["candidate_id"])
    return found


def _run_heuristic_chat_turn(user_input: str) -> Tuple[str, bool, List]:
    text_lower = user_input.lower()
    new_messages: List = [HumanMessage(content=user_input)]

    if any(k in text_lower for k in INTERVIEW_KEYWORDS):
        ids = _find_candidate_ids(user_input)
        if ids:
            answer = generate_interview_questions.invoke({"candidate_id": ids[0]})
        else:
            answer = "Which candidate would you like interview questions for? Use their candidate id or name."
        new_messages.append(AIMessage(content=answer))
        return answer, False, new_messages

    if any(k in text_lower for k in COMPARE_KEYWORDS):
        ids = _find_candidate_ids(user_input)
        if len(ids) >= 2:
            answer = compare_candidates.invoke({"candidate_ids": ids})
        else:
            answer = (
                "Tell me which candidates to compare by name or id, e.g. "
                "\"Compare synth_01_software_engineer_python and synth_09_fullstack_developer\"."
            )
        new_messages.append(AIMessage(content=answer))
        return answer, False, new_messages

    if any(k in text_lower for k in REFINEMENT_KEYWORDS):
        updated_jd = f"{SESSION.job_description}\n\nAdditional requirement from hiring manager: {user_input}"
        summary = extract_requirements.invoke({"jd": updated_jd})
        answer = f"Updated the requirements based on your note. Re-ranking candidates now.\n\n{summary}"
        new_messages.append(AIMessage(content=answer))
        return answer, True, new_messages

    pool = SESSION.final_recommendations or SESSION.shortlist or SESSION.initial_pool
    if pool:
        top = pool[0]
        answer = (
            "I can compare candidates, generate interview questions, or re-rank against new "
            "criteria. Try: \"compare X and Y\", \"interview questions for X\", or "
            f"\"require 3+ years of React\". Current top match: {top['candidate_name']} "
            f"(score {top['match_score']})."
        )
    else:
        answer = "Run a search first, e.g. by providing a job description."
    new_messages.append(AIMessage(content=answer))
    return answer, False, new_messages


def run_chat_turn(user_input: str, history: List) -> Tuple[str, bool, List]:
    """Returns (answer_text, needs_rerank, new_messages_to_append)."""
    if re.match(r"^\s*(quit|exit|done|stop|bye)\s*$", user_input, re.IGNORECASE):
        return "Ending the session. Good luck with the hire!", False, [
            HumanMessage(content=user_input),
            AIMessage(content="Ending the session. Good luck with the hire!"),
        ]

    try:
        return _run_llm_chat_turn(user_input, history)
    except Exception as error:  # LLM endpoint unavailable, model doesn't support tools, etc.
        answer, refine, new_messages = _run_heuristic_chat_turn(user_input)
        note = f"\n\n_(fell back to rule-based routing: {error})_"
        answer_with_note = answer + note
        if new_messages and isinstance(new_messages[-1], AIMessage):
            new_messages[-1] = AIMessage(content=answer_with_note)
        return answer_with_note, refine, new_messages
