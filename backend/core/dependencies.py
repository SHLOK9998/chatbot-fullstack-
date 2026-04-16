# core/dependencies.py
"""
Centralised dependency factories.
All shared singletons (LLM, memory store) live here so every service
imports from one place and avoids re-initialisation on every request.

Two LLM instances:
  get_llm()        → Groq/llama  — fast, cheap, used for intent classification,
                                    filter extraction, db_query formatting, crud,
                                    email flow logic (non-visible to user)
  get_openai_llm() → OpenAI model via Groq OpenAI-compatible endpoint — higher
                     quality, used for RAG answers, general chat, and email
                     content generation (everything the user actually reads)
"""
import logging
from functools import lru_cache
from core.config import settings
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)


@lru_cache()
def get_llm() -> ChatGroq:
    """Fast Groq/llama model — internal tasks only (intent, filters, flow logic)."""
    logger.info("Initialising ChatGroq LLM (model=%s)", settings.MODEL_NAME)
    return ChatGroq(
        temperature=0.4,
        model_name=settings.MODEL_NAME,
        groq_api_key=settings.GROQ_API_KEY,
    )


@lru_cache()
def get_openai_llm() -> ChatOpenAI:
    """OpenAI-compatible model via Groq — used for RAG, chat, and email content."""
    logger.info("Initialising OpenAI LLM (model=%s)", settings.OPENAI_MODEL_NAME)
    return ChatOpenAI(
        model=settings.OPENAI_MODEL_NAME,
        api_key=settings.API_FOR_OPENAI,
        base_url="https://api.groq.com/openai/v1",
        temperature=0.5,
    )
