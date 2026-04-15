# server.py
"""
Mounts the MCP Streamable HTTP server onto the FastAPI app.

After calling mount_mcp(app):
  POST /mcp        — all MCP JSON-RPC traffic
  GET  /mcp/health — health check

MCP Inspector setup:
  Transport type: Streamable HTTP
  URL: http://127.0.0.1:8000/mcp
"""

import logging
from fastapi import FastAPI
from transport.http_handler import router as mcp_router

logger = logging.getLogger(__name__)


def mount_mcp(app: FastAPI, prefix: str = "/mcp") -> None:
    app.include_router(mcp_router, prefix=prefix, tags=["MCP"])
    logger.info("[MCP] Streamable HTTP server mounted at prefix='%s'", prefix)
    logger.info("[MCP] Endpoint: POST %s", prefix)
    logger.info("[MCP] Health:   GET  %s/health", prefix)
