"""
Streamlit chat UI for the resume-matching agent -- same LangGraph graph as
matching_agent.py's CLI, just driven from a web chat instead of input().

Run with:  streamlit run streamlit_app.py
"""
import streamlit as st
from langchain_core.messages import AIMessage
from langgraph.types import Command

from config import JD_DIR
from matching_agent import build_graph, initial_state

st.set_page_config(page_title="Resume Matching Agent", layout="wide")
st.title("Resume Matching Agent")
st.caption("LangGraph agent: parse JD -> extract requirements -> search -> rank -> report -> chat refinement")

if "graph" not in st.session_state:
    st.session_state.graph = build_graph()
    st.session_state.config = {"configurable": {"thread_id": "streamlit-session"}, "recursion_limit": 100}
    st.session_state.started = False
    st.session_state.chat_log = []  # list of (role, text) for display
    st.session_state.finished = False

graph = st.session_state.graph
config = st.session_state.config

with st.sidebar:
    st.header("1. Job description")
    samples = sorted(JD_DIR.glob("*.txt")) if JD_DIR.exists() else []
    sample_names = ["(paste text below instead)"] + [s.name for s in samples]
    choice = st.selectbox("Sample job description", sample_names)
    pasted = st.text_area("...or paste/edit a job description", height=200)
    start_disabled = st.session_state.started

    if st.button("Start screening", disabled=start_disabled):
        if choice != sample_names[0]:
            jd_input = str(JD_DIR / choice)
        else:
            jd_input = pasted.strip()

        if not jd_input:
            st.warning("Provide a job description first.")
        else:
            with st.spinner("Running parse -> extract requirements -> search -> rank -> report..."):
                result = graph.invoke(initial_state(jd_input), config)
            st.session_state.started = True
            st.session_state.chat_log.append(("report", result["report_markdown"]))
            st.rerun()

    if st.session_state.started and st.button("Reset session"):
        for key in ("graph", "config", "started", "chat_log", "finished"):
            st.session_state.pop(key, None)
        st.rerun()

if not st.session_state.started:
    st.info("Pick or paste a job description in the sidebar, then click **Start screening**.")
else:
    for role, text in st.session_state.chat_log:
        if role == "report":
            with st.expander("Candidate Match Report", expanded=True):
                st.markdown(text)
        elif role == "user":
            with st.chat_message("user"):
                st.markdown(text)
        else:
            with st.chat_message("assistant"):
                st.markdown(text)

    if st.session_state.finished:
        st.success("Session ended. Reset to start a new search.")
    else:
        user_text = st.chat_input('Ask to compare candidates, request interview questions, or refine requirements ("require 3+ years of React")...')
        if user_text:
            st.session_state.chat_log.append(("user", user_text))
            with st.spinner("Thinking..."):
                result = graph.invoke(Command(resume=user_text), config)

            ai_messages = [m for m in result["messages"] if isinstance(m, AIMessage)]
            if ai_messages:
                st.session_state.chat_log.append(("assistant", ai_messages[-1].content))

            if result.get("needs_rerank") or (result.get("round_number", 0) >= 3 and result.get("report_markdown")):
                st.session_state.chat_log.append(("report", result["report_markdown"]))

            snapshot = graph.get_state(config)
            if not snapshot.next:
                st.session_state.finished = True

            st.rerun()
