"""
A small persistent vector store with a ChromaDB-like interface (add / query /
count / reset). Used in place of ChromaDB because this machine's SQLite
version and lack of a C++ build toolchain prevent ChromaDB's native
dependencies (sqlite>=3.35, compiled hnswlib) from being installed without
modifying system files. Swapping this class for a real ChromaDB/Pinecone/
Weaviate client only requires changing ResumeRAG's storage backend --
the embedding, chunking, and metadata logic in resume_rag.py is unchanged.
"""
import os
import json
import numpy as np


class VectorStore:
    def __init__(self, persist_dir, collection_name="resumes"):
        self.persist_dir = persist_dir
        self.collection_name = collection_name
        os.makedirs(persist_dir, exist_ok=True)
        self._meta_path = os.path.join(persist_dir, f"{collection_name}_meta.json")
        self._emb_path = os.path.join(persist_dir, f"{collection_name}_embeddings.npy")
        self.ids = []
        self.documents = []
        self.metadatas = []
        self.embeddings = None  # shape (n, dim), L2-normalized
        self._load()

    def _load(self):
        if os.path.exists(self._meta_path) and os.path.exists(self._emb_path):
            with open(self._meta_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.ids = data["ids"]
            self.documents = data["documents"]
            self.metadatas = data["metadatas"]
            self.embeddings = np.load(self._emb_path)

    def _save(self):
        with open(self._meta_path, "w", encoding="utf-8") as f:
            json.dump({"ids": self.ids, "documents": self.documents, "metadatas": self.metadatas}, f)
        if self.embeddings is not None:
            np.save(self._emb_path, self.embeddings)

    def reset(self):
        self.ids, self.documents, self.metadatas = [], [], []
        self.embeddings = None
        for path in (self._meta_path, self._emb_path):
            if os.path.exists(path):
                os.remove(path)

    def add(self, ids, documents, embeddings, metadatas):
        embeddings = np.array(embeddings, dtype=np.float32)
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        embeddings = embeddings / norms

        self.ids.extend(ids)
        self.documents.extend(documents)
        self.metadatas.extend(metadatas)
        self.embeddings = embeddings if self.embeddings is None else np.vstack([self.embeddings, embeddings])
        self._save()

    def count(self):
        return len(self.ids)

    @staticmethod
    def _matches_where(metadata, where):
        if not where:
            return True
        for key, condition in where.items():
            value = metadata.get(key)
            if isinstance(condition, dict):
                op, target = next(iter(condition.items()))
                if op == "$gte" and not (value is not None and value >= target):
                    return False
                if op == "$lte" and not (value is not None and value <= target):
                    return False
                if op == "$in" and value not in target:
                    return False
            else:
                if value != condition:
                    return False
        return True

    def query(self, query_embeddings, n_results=10, where=None):
        if self.embeddings is None or len(self.ids) == 0:
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}

        query_embeddings = np.array(query_embeddings, dtype=np.float32)
        norms = np.linalg.norm(query_embeddings, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        query_embeddings = query_embeddings / norms

        candidate_idx = [i for i, m in enumerate(self.metadatas) if self._matches_where(m, where)]
        if not candidate_idx:
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}

        candidate_embeddings = self.embeddings[candidate_idx]
        results = {"ids": [], "documents": [], "metadatas": [], "distances": []}

        for q in query_embeddings:
            sims = candidate_embeddings @ q  # cosine similarity (vectors are normalized)
            order = np.argsort(-sims)[:n_results]
            chosen = [candidate_idx[i] for i in order]
            results["ids"].append([self.ids[i] for i in chosen])
            results["documents"].append([self.documents[i] for i in chosen])
            results["metadatas"].append([self.metadatas[i] for i in chosen])
            results["distances"].append([float(1 - sims[i]) for i in order])  # cosine distance

        return results
