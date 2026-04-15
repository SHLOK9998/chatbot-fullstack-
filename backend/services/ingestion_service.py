# services/ingestion_service.py
"""
Reads Excel employee data → embeds each row → upserts into MongoDB employee_kb.

FLOW:
  App startup → initialize_knowledge_base()
    1. Find Excel file in USER_KNOWLEDGE folder
    2. Compute MD5 hash of the file
    3. Compare with stored hash in MongoDB ingestion_manifest collection
    4. If unchanged → skip (no wasted Gemini API calls)
    5. If changed   → parse Excel → batch embed → upsert employee_kb → save hash

UPSERT KEY:
  Each employee is identified by employee_no.
  If employee_no is blank, we generate a stable UUID from the row index
  so every row always has a unique, consistent key.

MANIFEST:
  Stored in MongoDB ingestion_manifest collection (not a file on disk).
  { filename: "employees.xlsx", md5: "abc123...", updated_at: ISODate(...) }

SYNC vs ASYNC:
  initialize_knowledge_base() is called inside asyncio.to_thread() at startup,
  so all functions here are sync. We use a separate sync pymongo client
  (not the async Motor client) for all DB writes in this file.
"""

import logging
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple, Optional

import pandas as pd

from core.config import settings
from core.database import get_db
from services.embedding_service import EmbeddingService

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
USER_KNOWLEDGE      = settings.USER_KNOWLEDGE
SUPPORTED_EXCEL_EXT = {".xls", ".xlsx"}

# Ensure the knowledge folder exists at startup
USER_KNOWLEDGE.mkdir(parents=True, exist_ok=True)

embedding_service = EmbeddingService()


# ── File hash ──────────────────────────────────────────────────────────────────

def _md5(file_path: Path) -> str:
    """
    Compute the MD5 hash of a file in chunks.
    Used to detect if the Excel changed since the last ingestion run.
    Same hash → skip.  Different hash → re-ingest.
    """
    h = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# ── MongoDB manifest (sync pymongo) ───────────────────────────────────────────

def _get_sync_db():
    """
    Return a sync pymongo database handle.
    Used only inside ingestion (which runs in a thread, not the async event loop).
    We create a fresh client each time to avoid connection-state issues in threads.
    """
    import pymongo
    client = pymongo.MongoClient(settings.MONGO_URL, serverSelectionTimeoutMS=5000)
    return client, client[settings.MONGO_DB_NAME]


def _get_manifest(filename: str) -> Optional[str]:
    """
    Look up the stored MD5 for a filename in ingestion_manifest.
    Returns the stored hash string, or None if not found.
    """
    try:
        client, db = _get_sync_db()
        doc = db["ingestion_manifest"].find_one({"filename": filename})
        client.close()
        return doc["md5"] if doc else None
    except Exception as e:
        logger.warning("[Ingestion] Could not read manifest: %s", e)
        return None


def _save_manifest(filename: str, md5: str) -> None:
    """
    Upsert the ingestion record for this filename into ingestion_manifest.
    Creates the document on first run, updates md5 on subsequent runs.
    """
    try:
        client, db = _get_sync_db()
        db["ingestion_manifest"].update_one(
            {"filename": filename},
            {"$set": {"md5": md5, "updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        client.close()
        logger.info("[Ingestion] Manifest saved for '%s'", filename)
    except Exception as e:
        logger.error("[Ingestion] Could not save manifest: %s", e)


# ── Excel → documents ──────────────────────────────────────────────────────────

def _excel_to_row_documents(excel_path: Path) -> List[Tuple[str, dict]]:
    """
    Parse every row of the Excel file into a (content_text, metadata) pair.

    content_text:
        A readable English sentence about the employee.
        e.g. "Anand Vaghela is a Full Stack Intern located in Botad.
               Email: anand@example.com. Contact: 9876543210."
        Readable sentences produce much better Gemini embeddings than raw JSON.

    metadata:
        Structured dict of all employee fields — used for MongoDB filter queries
        and for displaying results to the user.

    FIX vs old version:
        employee_no is now guaranteed non-empty.
        If the Excel row has a blank employee_no/id column, we generate a
        deterministic UUID from the row index so every row has a unique key.
        Without this, multiple blank-employee_no rows would all upsert to the
        same filter key {"metadata.employee_no": ""} and only the last would survive.
    """
    logger.info("[Ingestion] Reading Excel: %s", excel_path)
    df = pd.read_excel(excel_path, sheet_name=0, dtype=object)
    df = df.fillna("")

    # Normalise column names: strip whitespace, lowercase
    df.columns = [str(c).strip().lower() for c in df.columns]

    docs: List[Tuple[str, dict]] = []

    for idx, row in df.iterrows():
        row_dict = {k: str(v).strip() for k, v in row.to_dict().items()}

        name     = row_dict.get("name", "Unknown")
        middle_name = row_dict.get("middle_name","")
        surname = row_dict.get("surname","")
        field     = row_dict.get("field", "")
        position = row_dict.get("position", "")
        address  = row_dict.get("address", "")
        email    = row_dict.get("email", "")
        github   = row_dict.get("github", "")
        slack    = row_dict.get("slack", "")
        linkedin = row_dict.get("linkedin", "")
        contact  = row_dict.get("contact no", row_dict.get("contact", ""))

        # ── FIX: guarantee a non-empty, unique employee_no ────────────────────
        emp_no = (
            row_dict.get("employee_no")
            or row_dict.get("id")
            or f"auto_{uuid.uuid5(uuid.NAMESPACE_OID, f'{excel_path.name}_{idx}')}"
        )

        content = (
            f"{name} {surname} is a {field} {position} located in {address}. "
            f"Email: {email}. Contact: {contact}. github: {github}. slack: {slack}. linkedin: {linkedin} "
            f"and working in the infopulse tech company "
        ).strip()

        metadata = {
            "employee_no": emp_no,
            "name":        name,
            "email":       email,
            "contact":     contact,
            "field":        field,
            "position":    position,
            "address":     address,
        }

        docs.append((content, metadata))

    logger.info("[Ingestion] Converted Excel → %d employee documents", len(docs))
    return docs


# ── Write to MongoDB ───────────────────────────────────────────────────────────

def _upsert_employees_sync(docs: List[Tuple[str, dict]], source_filename: str) -> bool:
    """
    Embed all employee content strings in one Gemini batch call,
    then upsert each document into MongoDB employee_kb.

    Schema of each stored document:
    {
      "content":    "Anand Vaghela is a Full Stack Intern...",
      "embedding":  [0.23, -0.11, ...],   ← 768-dim Gemini vector
      "metadata":   { name, role, position, address, email, contact, employee_no },
      "source":     "employees.xlsx",
      "created_at": ISODate(...)
    }

    Upsert key: metadata.employee_no
      - If the employee already exists → update all fields (including new embedding)
      - If new employee → insert fresh document
    """
    if not docs:
        return False

    try:
        client, db = _get_sync_db()
        collection = db["employee_kb"]

        texts = [content for content, _ in docs]

        logger.info("[Ingestion] Generating embeddings for %d employees...", len(texts))
        vectors = embedding_service.get_embeddings_batch_sync(texts)
        logger.info(
            "[Ingestion] Embeddings done | dims=%d",
            len(vectors[0]) if vectors else 0,
        )

        success_count = 0
        for (content, metadata), vector in zip(docs, vectors):
            document = {
                "content":    content,
                "embedding":  vector,
                "metadata":   metadata,
                "source":     source_filename,
                "created_at": datetime.now(timezone.utc),
            }

            collection.update_one(
                {"metadata.employee_no": metadata["employee_no"]},
                {"$set": document},
                upsert=True,
            )
            success_count += 1

        client.close()
        logger.info(
            "[Ingestion] Upserted %d/%d employees into employee_kb",
            success_count, len(docs),
        )
        return True

    except Exception as e:
        logger.exception("[Ingestion] Failed to upsert employees: %s", e)
        return False


# ── Public: startup entry point ────────────────────────────────────────────────

def initialize_knowledge_base() -> None:
    """
    Called at app startup from main.py lifespan (inside asyncio.to_thread).

    Skips ingestion if the Excel file hash matches the stored manifest hash.
    Re-ingests if the file is new or has changed.
    """
    logger.info("=== Knowledge Base Initialisation ===")

    excel_files = [
        f for f in USER_KNOWLEDGE.iterdir()
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXCEL_EXT
    ]

    if not excel_files:
        logger.info("[Ingestion] No Excel files found in %s — skipping.", USER_KNOWLEDGE)
        return

    # Use the most recently modified Excel file if multiple exist
    excel_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    excel_path = excel_files[0]
    logger.info("[Ingestion] Using file: %s", excel_path.name)

    current_hash = _md5(excel_path)
    stored_hash  = _get_manifest(excel_path.name)

    if stored_hash == current_hash:
        logger.info("[Ingestion] File unchanged (hash match) — skipping ingestion.")
        return

    logger.info("[Ingestion] File is new or changed — starting ingestion...")

    docs = _excel_to_row_documents(excel_path)
    if not docs:
        logger.warning("[Ingestion] No documents produced from Excel — aborting.")
        return

    ok = _upsert_employees_sync(docs, source_filename=excel_path.name)

    if ok:
        _save_manifest(excel_path.name, current_hash)
        logger.info("[Ingestion] Complete — %d employees ingested.", len(docs))
    else:
        logger.error("[Ingestion] MongoDB write failed — manifest NOT updated (will retry next startup).")

    logger.info("=== Knowledge Base Initialisation Complete ===")

# ── Utility: manual refresh ────────────────────────────────────────────────────

def refresh_knowledge_base_from_excel(excel_filename: Optional[str] = None) -> dict:
    """
    Force re-ingestion regardless of hash.
    Can be triggered from an admin endpoint or a management script.
    Useful when you've updated the Excel but want an immediate refresh without restart.
    """
    try:
        if excel_filename:
            excel_path = USER_KNOWLEDGE / excel_filename
            if not excel_path.exists():
                return {"ok": False, "msg": f"File not found: {excel_filename}"}
        else:
            excel_files = [
                f for f in USER_KNOWLEDGE.iterdir()
                if f.is_file() and f.suffix.lower() in SUPPORTED_EXCEL_EXT
            ]
            if not excel_files:
                return {"ok": False, "msg": "No Excel files found."}
            excel_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            excel_path = excel_files[0]

        docs = _excel_to_row_documents(excel_path)
        if not docs:
            return {"ok": False, "msg": "No documents produced from Excel."}

        ok = _upsert_employees_sync(docs, source_filename=excel_path.name)

        if ok:
            _save_manifest(excel_path.name, _md5(excel_path))
            return {"ok": True, "msg": "Knowledge base refreshed.", "count": len(docs)}
        else:
            return {"ok": False, "msg": "MongoDB write failed."}

    except Exception as e:
        logger.exception("[Ingestion] refresh_knowledge_base_from_excel failed: %s", e)
        return {"ok": False, "msg": str(e)}