"""
Part B: Job Matching Engine

Takes a job description, embeds it, retrieves the top-K semantically similar
resume chunks, re-ranks candidates with a hybrid score (semantic similarity +
keyword overlap on critical skills), filters out candidates missing must-have
requirements, and produces a scored + explained ranking in the required
output JSON format.
"""
import re
import json
import time
import argparse
from collections import defaultdict

from resume_rag import ResumeRAG, SKILL_VOCABULARY, skill_matches

TOP_K = 10


def extract_required_skills(job_description):
    """Skills mentioned anywhere in the JD, matched against the same
    vocabulary used for resume skill extraction (keyword search side of
    hybrid search)."""
    return sorted({s for s in SKILL_VOCABULARY if skill_matches(job_description, s)})


def extract_must_haves(job_description):
    """Parse explicit 'N+ years of <skill/experience>' requirements, e.g.
    '5+ years of Python' or '5+ years of experience'. Returns a list of
    {years, skill} dicts. A generic 'N+ years of experience' (skill=None)
    is checked against the candidate's overall experience_years metadata;
    a skill-specific one (e.g. 'Python') requires that skill to be present
    AND is treated as a soft signal on top of the overall years filter.
    """
    must_haves = []
    pattern = re.compile(r"(\d+)\+?\s*years?\s*(?:of\s*)?(?:experience\s*(?:with|in)?\s*)?([A-Za-z][A-Za-z0-9+.# ]*)?",
                          re.IGNORECASE)
    for match in pattern.finditer(job_description):
        years = int(match.group(1))
        trailing = (match.group(2) or "").strip().rstrip(".,;:")
        skill = None
        # Longest-name-first + word-boundary match, so e.g. "JavaScript" isn't
        # misdetected as "Java" (a string-prefix, but not a word-boundary match)
        # just because "Java" sorts earlier in SKILL_VOCABULARY.
        for vocab_skill in sorted(SKILL_VOCABULARY, key=len, reverse=True):
            if skill_matches(trailing, vocab_skill):
                skill = vocab_skill
                break
        must_haves.append({"years": years, "skill": skill})

    # Deduplicate identical requirements
    seen = set()
    unique = []
    for m in must_haves:
        key = (m["years"], m["skill"])
        if key not in seen:
            seen.add(key)
            unique.append(m)
    return unique


def keyword_overlap_score(candidate_skills, required_skills):
    if not required_skills:
        return 0.0
    candidate_set = {s.lower() for s in candidate_skills}
    required_set = {s.lower() for s in required_skills}
    matched = candidate_set & required_set
    return len(matched) / len(required_set)


class JobMatcher:
    def __init__(self, rag: ResumeRAG = None):
        self.rag = rag or ResumeRAG()

    def match(self, job_description, top_k=TOP_K, semantic_weight=0.65, keyword_weight=0.35):
        start = time.time()
        required_skills = extract_required_skills(job_description)
        must_haves = extract_must_haves(job_description)

        query_embedding = self.rag.model.encode([job_description], show_progress_bar=False).tolist()
        raw = self.rag.collection.query(query_embeddings=query_embedding, n_results=top_k * 5)

        # Aggregate chunk-level hits into per-candidate results (best semantic
        # similarity chunk per candidate + all matched sections, for reasoning).
        candidates = {}
        for doc, meta, dist in zip(raw["documents"][0], raw["metadatas"][0], raw["distances"][0]):
            source = meta["source_file"]
            similarity = 1 - dist  # cosine distance -> similarity
            if source not in candidates:
                candidates[source] = {
                    "metadata": meta,
                    "best_similarity": similarity,
                    "matched_sections": defaultdict(float),
                    "excerpts": {},
                }
            entry = candidates[source]
            entry["best_similarity"] = max(entry["best_similarity"], similarity)
            if similarity > entry["matched_sections"][meta["section"]]:
                entry["matched_sections"][meta["section"]] = similarity
                entry["excerpts"][meta["section"]] = doc[:280]

        results = []
        for source, entry in candidates.items():
            meta = entry["metadata"]
            candidate_skills = [s.strip() for s in meta["skills"].split(",") if s.strip()]

            # Must-have filtering: a generic years requirement must be met by
            # experience_years metadata; a skill-specific requirement must
            # have that skill present in the candidate's skills.
            passes_filters = True
            failed_reasons = []
            for req in must_haves:
                if req["skill"]:
                    if req["skill"] not in candidate_skills:
                        passes_filters = False
                        failed_reasons.append(f"Missing required skill: {req['skill']}")
                else:
                    if meta["experience_years"] < req["years"]:
                        passes_filters = False
                        failed_reasons.append(
                            f"Requires {req['years']}+ years, candidate has {meta['experience_years']}"
                        )

            kw_score = keyword_overlap_score(candidate_skills, required_skills)
            semantic_score = entry["best_similarity"]
            combined = semantic_weight * semantic_score + keyword_weight * kw_score
            match_score = round(max(0.0, min(1.0, combined)) * 100)

            matched_skills = sorted(set(candidate_skills) & set(required_skills))
            matched_sections = sorted(entry["matched_sections"].keys())

            reasoning_parts = []
            if matched_skills:
                reasoning_parts.append(f"Matched skills: {', '.join(matched_skills)}")
            if matched_sections:
                reasoning_parts.append(f"Strong semantic match in section(s): {', '.join(matched_sections)}")
            reasoning_parts.append(f"Semantic similarity {semantic_score:.2f}, keyword overlap {kw_score:.2f}")
            if failed_reasons:
                reasoning_parts.append("Does not meet must-have requirements: " + "; ".join(failed_reasons))

            results.append({
                "candidate_name": meta["name"],
                "resume_path": meta["resume_path"],
                "match_score": match_score,
                "matched_skills": matched_skills,
                "relevant_excerpts": list(entry["excerpts"].values()),
                "reasoning": ". ".join(reasoning_parts) + ".",
                "meets_must_haves": passes_filters,
                "experience_years": meta["experience_years"],
                "education": meta["education"],
            })

        # Must-have-passing candidates first, then ranked by match_score.
        results.sort(key=lambda r: (not r["meets_must_haves"], -r["match_score"]))
        top_matches = results[:top_k]

        latency = time.time() - start
        return {
            "job_description": job_description,
            "required_skills": required_skills,
            "must_have_requirements": must_haves,
            "top_matches": top_matches,
            "latency_seconds": round(latency, 3),
        }


def main():
    parser = argparse.ArgumentParser(description="Match a job description against indexed resumes.")
    parser.add_argument("job_description_file", help="Path to a .txt file containing the job description")
    parser.add_argument("--top-k", type=int, default=TOP_K)
    parser.add_argument("--output", default=None, help="Optional path to write the JSON result")
    args = parser.parse_args()

    with open(args.job_description_file, "r", encoding="utf-8") as f:
        jd_text = f.read()

    matcher = JobMatcher()
    result = matcher.match(jd_text, top_k=args.top_k)

    output_json = json.dumps(result, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_json)
    print(output_json)


if __name__ == "__main__":
    main()
