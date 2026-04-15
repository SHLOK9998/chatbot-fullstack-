# services/embedding_service.py
"""
WHAT CHANGED AND WHY:
─────────────────────
Old file: EmbeddingService was a big class that did TWO things:
  1. Create embeddings (text → list of floats via Gemini)
  2. Store/load/append those embeddings in ChromaDB

Now those two responsibilities are separated:
  1. EmbeddingService  → ONLY creates embeddings (this file, much simpler)
  2. MongoDB           → stores embeddings inside documents (ingestion_service writes them)

Why separate them?
  - ChromaDB handled both embedding and storage as one unit.
  - MongoDB stores embeddings as just another role in a document.
    So "storage" is now handled by ingestion_service + mongo_rag_service.
  - This file becomes small and focused: just "give me a vector for this text".

WHAT STAYED:
  - GoogleGenerativeAIEmbeddings is still the embedding model (Gemini).
  - The lazy-init pattern (_embeddings initialised only on first use) stays.
  - get_embedding() returns List[float] — same data type as before.
  - Async wrapper stays so FastAPI/asyncio callers don't block.

WHAT WAS REMOVED:
  - create_vector_db_sync / append_to_vector_db_sync / load_vector_db_sync
  - All Chroma imports (langchain_chroma, Chroma)
  - persist_dir / _persist_path — no local folder needed anymore
  - The mkdir() call at import time (no chroma folder to create)
"""

import logging
import asyncio
from typing import List, Optional

from langchain_google_genai import GoogleGenerativeAIEmbeddings

from core.config import settings

logger = logging.getLogger(__name__)


class EmbeddingService:
    """
    Single responsibility: convert text → embedding vector using Gemini.

    The vector (list of floats) is then stored by whoever calls this —
    ingestion_service stores it in the "embedding" role of employee_kb,
    summary_service stores it in the "embedding" role of summaries.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ):
        # api_key: Gemini API key from settings (or override for testing)
        self.api_key = api_key or settings.GEMINI_API_KEY

        # model: which Gemini embedding model to use.
        # "models/embedding-001" is the standard Gemini text embedding model.
        self.model = model or settings.EMBED_MODEL

        # _embeddings: the actual LangChain Gemini embedding object.
        # Set to None now, created only on first use (lazy init).
        # Reason: we don't want to make an API call just by importing this file.
        self._embeddings: Optional[GoogleGenerativeAIEmbeddings] = None

        logger.info("EmbeddingService initialised | model=%s", self.model)

    def _init_embeddings(self) -> GoogleGenerativeAIEmbeddings:
        """
        Create the Gemini embedding client if it doesn't exist yet.
        Called internally before every embedding operation.
        This is called 'lazy initialisation' — create only when first needed.
        """
        if self._embeddings is None:
            logger.info("Initialising GoogleGenerativeAIEmbeddings | model=%s", self.model)
            self._embeddings = GoogleGenerativeAIEmbeddings(
                model=self.model,
                google_api_key=self.api_key,
            )
        return self._embeddings

    def get_embedding_sync(self, text: str) -> List[float]:
        """
        Convert a single text string into an embedding vector.

        Returns: List[float]  e.g. [0.23, -0.11, 0.45, ...]
        The length of the list depends on the Gemini model (typically 768 dims).

        This is the SYNC version — used by ingestion_service which already
        runs inside asyncio.to_thread() so sync is fine there.

        How it works:
          1. Gemini receives the text
          2. Gemini returns a vector (list of floats) that numerically
             represents the meaning of the text
          3. Similar texts produce similar vectors — that's what makes
             semantic search possible
        """
        if not text or not text.strip():
            logger.warning("get_embedding called with empty text — returning empty list.")
            return []

        embeddings = self._init_embeddings()
        try:
            # embed_query() takes a single string and returns List[float]
            vector = embeddings.embed_query(text)
            logger.debug("Embedding created | text_len=%d | vector_len=%d", len(text), len(vector))
            return vector
        except Exception as e:
            logger.exception("Failed to create embedding: %s", e)
            raise

    async def get_embedding(self, text: str) -> List[float]:
        """
        Async wrapper around get_embedding_sync.

        Why asyncio.to_thread?
        GoogleGenerativeAIEmbeddings.embed_query() is a blocking (sync) HTTP call.
        If we call it directly inside an async function, it blocks the entire
        FastAPI event loop — no other requests can be handled during that time.
        asyncio.to_thread() runs it in a separate thread so the event loop
        stays free to handle other work.
        """
        return await asyncio.to_thread(self.get_embedding_sync, text)

    def get_embeddings_batch_sync(self, texts: List[str]) -> List[List[float]]:
        """
        Convert multiple texts into embedding vectors in one API call.

        Used by ingestion_service when loading many employee rows at once.
        Batching is more efficient than calling get_embedding_sync() in a loop
        because it makes fewer API calls to Gemini.

        Returns: List of vectors, one per input text, same order.
        """
        if not texts:
            return []

        embeddings = self._init_embeddings()
        try:
            # embed_documents() takes List[str] and returns List[List[float]]
            vectors = embeddings.embed_documents(texts)
            logger.info("Batch embedding created | count=%d | vector_len=%d", len(vectors), len(vectors[0]) if vectors else 0)
            return vectors
        except Exception as e:
            logger.exception("Failed to create batch embeddings: %s", e)
            raise

    async def get_embeddings_batch(self, texts: List[str]) -> List[List[float]]:
        """Async wrapper for batch embedding."""
        return await asyncio.to_thread(self.get_embeddings_batch_sync, texts)





# {
#   "fields": [
#     {
#       "type": "vector",
#       "path": "embedding",
#       "numDimensions": 3072,
#       "similarity": "cosine"
#     },
#     {
#       "type": "filter",
#       "path": "metadata.role"
#     },
#     {
#       "type": "filter",
#       "path": "metadata.position"
#     },
#     {
#       "type": "filter",
#       "path": "metadata.address"
#     }
#   ]
# }