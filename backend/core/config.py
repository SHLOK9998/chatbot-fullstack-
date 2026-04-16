# core/config.py
import os
from dotenv import load_dotenv
from pathlib import Path

# Resolve project root (2 levels up from this file)
project_root = Path(__file__).resolve().parents[1]
load_dotenv(project_root / ".env")

class Settings:
    # Project root
    PROJECT_ROOT = project_root

    # Knowledge base and vector DB paths
    DATA_DIR         = Path(os.getenv("DATA_DIR", "data"))
    USER_KNOWLEDGE   = Path(os.getenv("USER_KNOWLEDGE", "data/user_knowledge"))
    # CHROMA_DIR       = Path(os.getenv("CHROMA_DIR", "data/chroma_db"))

    # LLM credentials (Groq)
    GROQ_API_KEY     = os.getenv("GROQ_API_KEY")
    MODEL_NAME       = os.getenv("MODEL_NAME", "llama-3.1-8b-instant")

    # Embedding model (Gemini)
    GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY")
    EMBED_MODEL      = os.getenv("EMBED_MODEL", "gemini/embedding-001")

    # OAuth client secrets file (downloaded from Google Cloud Console)
    GOOGLE_CREDENTIALS_FILE = project_root / os.getenv("GOOGLE_CREDENTIALS_FILE", "creden/credentials.json")
    # Token file shared by Gmail + Calendar (both scopes baked in at auth time)
    GOOGLE_TOKEN_FILE = project_root / os.getenv("GOOGLE_TOKEN_FILE", "creden/token.json")

    # Google OAuth scopes (Gmail + Calendar + Tasks)
    GOOGLE_SCOPES = [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/tasks",
    ]

    #Sender Gmail address (used as "From" header)
    GMAIL_SENDER = os.getenv("GMAIL_SENDER")
    
    # max tokens for context momoey for buffer and summary diff
    MEMORY_MAX_TOKEN_LIMIT = int(os.getenv("MEMORY_MAX_TOKEN_LIMIT",10000))
    
    # database information :
    MONGO_DB_NAME = os.getenv("DB_NAME")
    MONGO_URL = os.getenv("MONGO_URL")
    
    # REDIS URL FOR CACHE DATA STORAGE FOR BETTER AND FASTER REPLY
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

    # Google OAuth redirect URI
    GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://127.0.0.1:8000/auth/google/callback")

    # JWT auth — sliding session
    JWT_SECRET      = os.getenv("JWT_SECRET", "change-this-secret-in-production")
    JWT_EXPIRE_DAYS = int(os.getenv("JWT_EXPIRE_DAYS", "30"))


settings = Settings()