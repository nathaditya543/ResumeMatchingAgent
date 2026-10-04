"""
Shared configuration: paths, .env loading, and the LLM client factory.

The agent talks to an OpenAI-compatible chat endpoint. By default this points
at a local Ollama server (same setup as Milestone 1's llm_file_assistant.py),
so the whole pipeline runs free/offline. Set OPENAI_BASE_URL to
"https://api.openai.com/v1" (and a real OPENAI_API_KEY / OPENAI_MODEL) to use
OpenAI instead -- no code changes needed.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# The resume embedding model is cached locally after the first run; skip the
# "check for updates" network call so the pipeline works fully offline.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
RESUME_DIR = DATA_DIR / "resumes"
JD_DIR = DATA_DIR / "job_descriptions"
REPORTS_DIR = PROJECT_ROOT / "reports"
VECTOR_DB_DIR = PROJECT_ROOT / "vector_db"

load_dotenv(PROJECT_ROOT / ".env")


def get_llm(temperature: float = 0.2):
    """Build a LangChain ChatOpenAI client pointed at whatever OpenAI-compatible
    endpoint is configured in .env (local Ollama by default)."""
    from langchain_openai import ChatOpenAI

    base_url = os.getenv("OPENAI_BASE_URL", "http://localhost:11434/v1")
    api_key = os.getenv("OPENAI_API_KEY", "ollama")
    model = os.getenv("OPENAI_MODEL", "qwen3.5:4b")

    return ChatOpenAI(
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=temperature,
    )
