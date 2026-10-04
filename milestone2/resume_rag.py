"""
Part A: RAG System Setup

Loads resumes (reusing the file-reading approach from Milestone 1's fs_tools.py),
chunks them by resume section (Education, Experience, Skills, etc.), embeds each
chunk with a local HuggingFace sentence-transformers model, and stores the chunks
+ embeddings + metadata in a persistent ChromaDB collection.
"""
import os
import re
import glob
import json
import time
from pathlib import Path

from sentence_transformers import SentenceTransformer

from vector_store import VectorStore

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESUME_DIR = os.path.join(PROJECT_ROOT, "data", "resumes")
VECTOR_STORE_DIR = os.path.join(PROJECT_ROOT, "vector_db")
COLLECTION_NAME = "resumes"
EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"

# Section headers commonly found in resumes. Matching is case-insensitive and
# anchored to the start of a line so we don't split on the word appearing
# inside a sentence (e.g. "5 years of experience").
SECTION_HEADERS = [
    "OBJECTIVE", "SUMMARY", "PROFILE",
    "EDUCATION",
    "EXPERIENCE", "WORK EXPERIENCE", "RELEVANT EXPERIENCE", "PROFESSIONAL EXPERIENCE",
    "SKILLS", "TECHNICAL SKILLS", "CORE SKILLS",
    "PROJECTS",
    "CERTIFICATIONS", "CERTIFICATES",
    "ACTIVITIES", "LEADERSHIP", "AWARDS", "PUBLICATIONS",
]

SKILL_VOCABULARY = [
    "Python", "Java", "JavaScript", "TypeScript", "C++", "C#", "Go", "Ruby", "Swift", "SQL", "R",
    "Django", "FastAPI", "Flask", "React", "Redux", "Next.js", "Node.js", "Express", "Angular", "Vue",
    "PostgreSQL", "MySQL", "MongoDB", "Kafka", "RabbitMQ", "Spark", "Airflow", "Kubeflow",
    "Docker", "Kubernetes", "Terraform", "Jenkins", "GitLab CI", "CI/CD", "AWS", "GCP", "Azure",
    "Machine Learning", "Deep Learning", "NLP", "TensorFlow", "PyTorch", "scikit-learn", "XGBoost",
    "Pandas", "NumPy", "Hugging Face", "SageMaker", "MLOps",
    "Selenium", "pytest", "Postman", "API Testing", "Jest",
    "Splunk", "Burp Suite", "Metasploit", "SIEM", "Penetration Testing", "Network Security",
    "Agile", "Jira", "Product Strategy", "A/B Testing", "SEO", "SEM", "Google Analytics", "HubSpot",
    "Git", "Linux", "Bash", "REST APIs", "Prometheus", "Grafana",
]

EDUCATION_KEYWORDS = {
    "phd": "PhD",
    "ph.d": "PhD",
    "doctorate": "PhD",
    "master": "Master's",
    "mba": "MBA",
    "m.s.": "Master's",
    "bachelor": "Bachelor's",
    "b.s.": "Bachelor's",
    "b.a.": "Bachelor's",
    "bsn": "Bachelor's",
}


def load_resumes(resume_dir=RESUME_DIR):
    """Load resume text files. Mirrors fs_tools.read_file's simple text-loading
    approach from Milestone 1 (this project only needs .txt resumes, already
    extracted from the original PDFs)."""
    resumes = []
    for path in sorted(glob.glob(os.path.join(resume_dir, "*.txt"))):
        text = Path(path).read_text(encoding="utf-8")
        resumes.append({"path": path, "filename": os.path.basename(path), "text": text})
    return resumes


def chunk_resume(text):
    """Split resume text into (section_name, chunk_text) pairs using section
    headers as boundaries. Falls back to a single 'FULL_DOCUMENT' chunk if no
    recognizable headers are found."""
    pattern = re.compile(
        r"(?im)^\s*(" + "|".join(re.escape(h) for h in SECTION_HEADERS) + r")\s*:?\s*$"
    )
    # Some PDFs extract headers inline (not on their own line), so also try a
    # looser inline match as a fallback pass.
    matches = list(pattern.finditer(text))

    if not matches:
        inline_pattern = re.compile(
            r"(?i)\b(" + "|".join(re.escape(h) for h in SECTION_HEADERS) + r")\b"
        )
        matches = list(inline_pattern.finditer(text))

    if not matches:
        return [("FULL_DOCUMENT", text.strip())]

    chunks = []
    header_text = text[: matches[0].start()].strip()
    if header_text:
        chunks.append(("HEADER", header_text))

    for i, m in enumerate(matches):
        section_name = m.group(1).upper()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            chunks.append((section_name, body))

    return chunks if chunks else [("FULL_DOCUMENT", text.strip())]


def extract_name(text, filename):
    """Heuristic: the candidate's name is usually the first line, or the first
    few capitalized words before an email/phone appears."""
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    m = re.match(r"^\s*([A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+){1,3})", first_line)
    if m:
        candidate = m.group(1).strip()
        if 2 <= len(candidate.split()) <= 4:
            return candidate
    # Fall back to filename
    stem = os.path.splitext(filename)[0]
    return stem.replace("_", " ")


def skill_matches(text, skill):
    """Word-boundary match so short skill names (e.g. 'Go', 'R') don't match
    as substrings of unrelated words (e.g. 'Go' inside 'Django')."""
    pattern = r"(?<![A-Za-z0-9])" + re.escape(skill) + r"(?![A-Za-z0-9])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def extract_skills(text):
    return sorted({skill for skill in SKILL_VOCABULARY if skill_matches(text, skill)})


def extract_experience_years(text):
    """Look for explicit '<N> years' mentions, and fall back to estimating
    from the earliest year seen in the experience/date ranges."""
    explicit = re.findall(r"(\d+)\+?\s*years?", text, flags=re.IGNORECASE)
    if explicit:
        return max(int(n) for n in explicit)

    years = [int(y) for y in re.findall(r"\b((?:19|20)\d{2})\b", text)]
    if len(years) >= 2:
        return max(0, max(years) - min(years))
    return 0


def extract_education(text):
    lower = text.lower()
    for keyword, label in EDUCATION_KEYWORDS.items():
        if keyword in lower:
            return label
    return "Not specified"


def extract_metadata(text, filename):
    return {
        "name": extract_name(text, filename),
        "skills": ", ".join(extract_skills(text)),
        "experience_years": extract_experience_years(text),
        "education": extract_education(text),
        "source_file": filename,
    }


class ResumeRAG:
    def __init__(self, vector_store_dir=VECTOR_STORE_DIR, collection_name=COLLECTION_NAME,
                 model_name=EMBEDDING_MODEL_NAME):
        self.collection = VectorStore(vector_store_dir, collection_name)
        self.model = SentenceTransformer(model_name)

    def build_index(self, resume_dir=RESUME_DIR, reset=True):
        """Full pipeline: load -> chunk -> embed -> store."""
        if reset:
            self.collection.reset()

        resumes = load_resumes(resume_dir)
        ids, documents, metadatas = [], [], []

        for resume in resumes:
            metadata = extract_metadata(resume["text"], resume["filename"])
            chunks = chunk_resume(resume["text"])

            for idx, (section, chunk_text) in enumerate(chunks):
                chunk_id = f"{resume['filename']}::chunk{idx}::{section}"
                ids.append(chunk_id)
                documents.append(chunk_text)
                metadatas.append({
                    **metadata,
                    "section": section,
                    "chunk_index": idx,
                    "resume_path": resume["path"],
                })

        if not documents:
            return {"resumes_indexed": 0, "chunks_indexed": 0}

        embeddings = self.model.encode(documents, show_progress_bar=False).tolist()

        # Chroma has an add batch limit on some backends; batch defensively.
        batch_size = 200
        for i in range(0, len(ids), batch_size):
            self.collection.add(
                ids=ids[i:i + batch_size],
                documents=documents[i:i + batch_size],
                embeddings=embeddings[i:i + batch_size],
                metadatas=metadatas[i:i + batch_size],
            )

        return {"resumes_indexed": len(resumes), "chunks_indexed": len(documents)}

    def count(self):
        return self.collection.count()


if __name__ == "__main__":
    start = time.time()
    rag = ResumeRAG()
    stats = rag.build_index()
    elapsed = time.time() - start
    print(json.dumps({**stats, "seconds": round(elapsed, 2)}, indent=2))
