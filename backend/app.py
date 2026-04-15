"""
app.py — Terminal client for the chatbot.

HOW IT WORKS WITH THE FASTAPI SERVER:

  You run TWO processes:

  Terminal 1 (server):  uvicorn main:app --reload
    When uvicorn starts  → main.py lifespan creates a NEW thread automatically
    When uvicorn stops   → main.py lifespan flushes remaining messages to summary

  Terminal 2 (client):  python app.py
    On start   → checks server health, shows current thread + message count
    Chatting   → POST /chat/ → FastAPI processes it, saves to DB
    On 'exit'  → POST /chat/session/end → flush remaining messages to summary
    On Ctrl+C  → same flush + exit

  NEW THREAD LIFECYCLE:
    Every time you restart uvicorn, a brand new thread is created (in main.py).
    Old threads remain in MongoDB — they are never deleted.
    The summary from old threads is preserved as well.

  SUMMARY FLUSH LIFECYCLE:
    - Every 30 messages (15 rounds) → automatic rolling summary update
    - On 'exit' or Ctrl+C → POST /chat/session/end → flush remaining messages
    - On uvicorn Ctrl+C → same flush runs in the FastAPI shutdown lifecycle

  ALL ROUTES (mounted under /chat prefix in main.py):
    POST   /chat/            ← send a message
    POST   /chat/session/end ← flush summary
    GET    /chat/health      ← server status + thread info
    GET    /chat/threads     ← list all past threads

Usage:
  python app.py                            (API mode, default URL)
  python app.py --url http://127.0.0.1:8000
  python app.py --direct                   (run engine in-process, no server)
"""

import sys
import os
import argparse
import asyncio
import signal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Must match the user_id the server uses (DEFAULT_USER in chat_service.py).
# Override via environment variable if needed: CHAT_USER_ID=yourname python app.py
USER_ID = os.getenv("CHAT_USER_ID", "default_user")


# ─────────────────────────────────────────────────────────────────────────────
# API MODE — communicates with the FastAPI server over HTTP
# ─────────────────────────────────────────────────────────────────────────────

def run_api_mode(base_url: str):
    import requests

    # All routes are under the /chat prefix set in main.py include_router
    chat_url        = f"{base_url}/chat/"          # POST — send message
    health_url      = f"{base_url}/chat/health"    # GET  — status
    session_end_url = f"{base_url}/chat/session/end"  # POST — flush summary

    # ── Confirm server is running ─────────────────────────────────────────────
    try:
        r    = requests.get(health_url, timeout=5)
        r.raise_for_status()
        info = r.json()
        print(f"\n[Server OK]")
        print(f"  Thread   : {info.get('thread_id', '?')}")
        print(f"  Messages : {info.get('message_count', 0)}")
        print(f"  Summarised up to message: {info.get('summarized_up_to', 0)}")
    except Exception as e:
        print(f"\n[ERROR] Could not reach server at {base_url}")
        print(f"  Detail: {e}")
        print(f"  Start server first: uvicorn main:app --reload")
        sys.exit(1)

    print(f"\nChat started. Type 'exit' or 'quit' to quit.")
    print(f"On exit, remaining messages are flushed into the summary automatically.")
    print("─" * 60)

    def _flush_and_exit():
        """
        POST to /chat/session/end to flush remaining messages into rolling summary.
        This runs BEFORE the process exits — so all messages are always summarised.
        """
        print("\n[Flushing session summary...]", end="", flush=True)
        try:
            r    = requests.post(session_end_url, json={"user_id": USER_ID}, timeout=60)
            data = r.json()
            if data.get("flushed"):
                print(f"\n[Summary updated: {data.get('message', '')}]")
            else:
                print(f"\n[{data.get('message', 'Summary already up-to-date')}]")
        except Exception as e:
            print(f"\n[Warning: flush request failed: {e}]")

    # ── Chat loop ─────────────────────────────────────────────────────────────
    while True:
        try:
            user_input = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            _flush_and_exit()
            print("Goodbye.")
            break

        if not user_input:
            continue

        if user_input.lower() in ("exit", "quit", "bye"):
            _flush_and_exit()
            print("Goodbye.")
            break

        try:
            resp = requests.post(
                chat_url,
                json={"message": user_input, "user_id": USER_ID},
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()
            print(f"\nAssistant: {data.get('response', data)}")
        except requests.Timeout:
            print("[Timeout — LLM is processing slowly, please try again]")
        except Exception as e:
            print(f"[Error: {e}]")


# ─────────────────────────────────────────────────────────────────────────────
# DIRECT MODE — runs the full engine in-process, no FastAPI needed
# ─────────────────────────────────────────────────────────────────────────────

def run_direct_mode():
    print("Direct mode — engine running locally (no FastAPI server needed).")
    print("Type 'exit' or 'quit' to quit (summary flushed automatically on exit).")
    print("─" * 60)

    async def main():
        from core.database import connect_db, close_db
        from core.logger import setup_logger
        from services.chat_service import initialize_session, process_query, end_session

        setup_logger()
        await connect_db()

        # Create a fresh thread for this session
        thread_id = await initialize_session(USER_ID)
        print(f"[New thread created: {thread_id}]")

        # SIGINT (Ctrl+C) handler — sets an event to break the loop cleanly
        _stop = asyncio.Event()
        loop  = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, _stop.set)

        # ── Chat loop ─────────────────────────────────────────────────────────
        try:
            while not _stop.is_set():
                try:
                    # Use asyncio.to_thread so input() doesn't block the event loop
                    # This allows SIGINT to be detected even while waiting for input
                    user_input = await asyncio.to_thread(input, "\nYou: ")
                    user_input = user_input.strip()
                except EOFError:
                    break

                if not user_input:
                    continue

                if user_input.lower() in ("exit", "quit", "bye"):
                    break

                reply = await process_query(user_input, USER_ID)
                print(f"\nAssistant: {reply}")

        finally:
            # Always flush on exit — whether clean exit, Ctrl+C, or error
            print("\n[Flushing session summary...]", end="", flush=True)
            try:
                flushed = await end_session(USER_ID)
                if flushed:
                    print("\n[Summary updated with remaining messages]")
                else:
                    print("\n[Summary already up-to-date]")
            except Exception as e:
                print(f"\n[Warning: flush failed: {e}]")

            await close_db()
            print("Goodbye.")

    asyncio.run(main())


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Terminal chatbot client")
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Run engine in-process (no FastAPI server needed)",
    )
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000",
        help="FastAPI base URL (default: http://127.0.0.1:8000)",
    )
    args = parser.parse_args()

    if args.direct:
        run_direct_mode()
    else:
        run_api_mode(args.url)
