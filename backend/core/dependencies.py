# core/dependencies.py
"""
Centralised dependency factories.
All shared singletons (LLM, memory store) live here so every service
imports from one place and avoids re-initialisation on every request.
"""
import logging
from functools import lru_cache
from core.config import settings
from langchain_groq import ChatGroq
# from langchain_classic.memory import ConversationSummaryBufferMemory

logger = logging.getLogger(__name__)

# returns a single chatgorq llm instance , only initialised once per session

@lru_cache()
def get_llm() -> ChatGroq:
   
    logger.info("Initialising ChatGroq LLM (model=%s)", settings.MODEL_NAME)
    return ChatGroq(
        temperature=0.4,
        model_name=settings.MODEL_NAME,
        groq_api_key=settings.GROQ_API_KEY,
    )

