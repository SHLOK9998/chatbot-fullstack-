# services/mongo_rag_service.py
"""
MongoDB Atlas Vector Search service.

This is the replacement for ChromaDB's retriever (db.as_retriever()).
Old: db.as_retriever(search_kwargs={"k": 20}) → LangChain retriever object
New: search_employees(query, top_k=5)          → list of matching dicts

HOW ATLAS VECTOR SEARCH WORKS:
  1. You send a query text ("who is the full stack intern?")
  2. We convert it to an embedding vector using Gemini
  3. We send that vector to MongoDB Atlas via the $vectorSearch pipeline
  4. Atlas compares it against all stored embeddings in employee_kb
  5. Atlas returns the top-k documents whose embeddings are most similar
  6. We return those documents to chat_service as plain dicts

BEFORE YOU USE THIS — ATLAS INDEX REQUIRED:
  You must create a vector search index in MongoDB Atlas UI first.
  Go to: Atlas → your cluster → Search Indexes → Create Index → JSON Editor

  For employee_kb collection, paste this JSON:
  {
    "fields": [
      {
        "type": "vector",
        "path": "embedding",
        "numDimensions": 768,
        "similarity": "cosine"
      },
      { "type": "filter", "path": "metadata.role" },
      { "type": "filter", "path": "metadata.position" },
      { "type": "filter", "path": "metadata.address" }
    ]
  }
  Name it: "employee_vector_index"

  numDimensions=768 because Gemini "models/embedding-001" produces 768-dim vectors.
"""

import logging
from typing import Optional

from core.database import get_db
from services.embedding_service import EmbeddingService

logger = logging.getLogger(__name__)
embedding_service = EmbeddingService()

async def search_employees(
    query: str,
    top_k: int = 5,
    filters: Optional[dict] = None,
) -> list[dict]:
    """
    Search the employee_kb collection using Atlas Vector Search.

    Args:
        query   : the user's natural language question
        top_k   : how many results to return (default 5)
        filters : optional metadata pre-filter dict
                  e.g. {"metadata.role": "Full Stack"}
                  e.g. {"metadata.position": "Intern"}
                  Filters narrow the search BEFORE vector similarity runs.

    Returns:
        List of dicts, each with:
          - content  : the employee text content
          - metadata : name, role, position, address, email, contact
          - score    : similarity score (0.0 to 1.0, higher = more similar)

    Example return:
        [
          {
            "content": "Anand Vaghela is a Full Stack Intern located in Botad...",
            "metadata": {"name": "Anand", "role": "Full Stack", ...},
            "score": 0.92
          },
          ...
        ]
    """
    if not query or not query.strip():
        logger.warning("[RAG] Empty query — returning no results.")
        return []

    # Step 1: Convert query text → embedding vector
    # This is the same Gemini model used during ingestion,
    # so the vectors are in the same space and can be compared.
    logger.info("[RAG] Embedding query: '%s'", query[:60])
    query_vector = await embedding_service.get_embedding(query)

    if not query_vector:
        logger.error("[RAG] Failed to embed query — returning no results.")
        return []

    # Step 2: Build the Atlas $vectorSearch pipeline
    # numCandidates: how many docs Atlas pre-selects before ranking by similarity.
    #   Should be at least top_k * 10. More candidates = slower but more accurate.
    # limit: final number of results returned after ranking.
    vector_search_stage = {
        "$vectorSearch": {
            "index":         "employee_vector_index",  # must match the index name in Atlas
            "path":          "embedding",              # role in the document that holds the vector
            "queryVector":   query_vector,             # your query as a vector
            "numCandidates": top_k * 10,               # pre-selection pool size
            "limit":         top_k,                    # final result count
        }
    }

    # Add metadata filter if provided.
    # Filters are applied BEFORE vector search — this is more efficient than
    # post-filtering because Atlas only searches a smaller subset of documents.
    # Example: filter to only search Full Stack employees
    if filters:
        vector_search_stage["$vectorSearch"]["filter"] = filters
        logger.info("[RAG] Applying filter: %s", filters)

    # Step 3: $project — select what fields to return.
    # We exclude "embedding" because it's a large array of 768 floats —
    # we don't need it in the response, it would just waste memory.
    # vectorSearchScore gives us the similarity score for debugging.
    project_stage = {
        "$project": {
            "content":    1,          # include
            "metadata":   1,          # include
            "source":     1,          # include
            "score": {"$meta": "vectorSearchScore"},  # include similarity score
        }
    }

    pipeline = [vector_search_stage, project_stage]

    # Step 4: Run the aggregation pipeline against MongoDB
    try:
        db         = get_db()
        collection = db["employee_kb"]

        # aggregate() returns an async cursor — we iterate it with to_list()
        cursor  = collection.aggregate(pipeline)
        results = await cursor.to_list(length=top_k)

        logger.info("[RAG] Vector search returned %d results for query: '%s'", len(results), query[:60])

        # Log scores for debugging — helps tune numCandidates and top_k
        for i, r in enumerate(results):
            logger.debug("[RAG] Result %d | score=%.4f | name=%s", i+1, r.get("score", 0), r.get("metadata", {}).get("name", "?"))

        return results

    except Exception as e:
        logger.exception("[RAG] Atlas vector search failed: %s", e)
        return []

async def search_employees_with_filter(
    query: str,
    role: Optional[str] = None,
    position: Optional[str] = None,
    address: Optional[str] = None,
    top_k: int = 5,
) -> list[dict]:
    """
    Convenience wrapper for filtered employee search.

    Instead of building the filter dict manually, pass named parameters.
    Used when you know specific metadata to filter by — e.g. from CRUD intent.

    Example:
        results = await search_employees_with_filter(
            query="who is the intern",
            position="Intern",
            top_k=3
        )
    """
    filters = {}
    if role:
        filters["metadata.role"] = role
    if position:
        filters["metadata.position"] = position
    if address:
        filters["metadata.address"] = address

    return await search_employees(query, top_k=top_k, filters=filters if filters else None)