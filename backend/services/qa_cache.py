from __future__ import annotations

import hashlib
import logging
from typing import Optional

import chromadb
from chromadb.config import Settings as ChromaSettings

from backend.config import get_settings

logger = logging.getLogger("qa_cache")

COLLECTION_NAME = "qa_cache"


class QACache:
    """
    Semantic cache for job application Q&A answers.

    Questions are embedded with nomic-embed-text (via Ollama) and stored in ChromaDB.
    On lookup, returns the stored answer if cosine distance < threshold.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._client: Optional[chromadb.PersistentClient] = None
        self._collection: Optional[chromadb.Collection] = None

    def _ensure_ready(self) -> None:
        if self._collection is not None:
            return
        chroma_path = str(self._settings.chroma_persist_dir)
        self._client = chromadb.PersistentClient(
            path=chroma_path,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("ChromaDB ready at %s (collection: %s)", chroma_path, COLLECTION_NAME)

    async def get_answer(self, question: str) -> Optional[str]:
        self._ensure_ready()
        if self._collection.count() == 0:
            return None
        embedding = await self._embed(question)
        results = self._collection.query(
            query_embeddings=[embedding],
            n_results=1,
            include=["documents", "metadatas", "distances"],
        )
        distances = results.get("distances") or [[]]
        if not distances[0]:
            return None
        distance = distances[0][0]
        threshold = self._settings.qa_similarity_threshold
        if distance <= threshold:
            answer = (results["documents"] or [[]])[0][0]
            portal = ((results["metadatas"] or [[]])[0][0] or {}).get("portal", "?")
            logger.debug(
                "Cache HIT: distance=%.4f threshold=%.4f portal=%s", distance, threshold, portal
            )
            return answer
        logger.debug("Cache MISS: distance=%.4f > threshold=%.4f", distance, threshold)
        return None

    async def store_answer(self, question: str, answer: str, portal: str = "") -> None:
        self._ensure_ready()
        embedding = await self._embed(question)
        doc_id = hashlib.sha256(question.encode()).hexdigest()[:32]
        self._collection.upsert(
            ids=[doc_id],
            embeddings=[embedding],
            documents=[answer],
            metadatas=[{"portal": portal, "question_preview": question[:120]}],
        )
        logger.info("Stored Q&A: portal=%s id=%s", portal, doc_id)

    async def count(self) -> int:
        self._ensure_ready()
        return self._collection.count()

    async def _embed(self, text: str) -> list[float]:
        # Import here to avoid circular dependency with llm_router
        from backend.services.llm_router import get_llm_router
        return await get_llm_router().embed(text)


# Module-level singleton
_cache: Optional[QACache] = None


def get_qa_cache() -> QACache:
    global _cache
    if _cache is None:
        _cache = QACache()
    return _cache
