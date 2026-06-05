"""
voice_main.py — Servidor FastAPI dedicado SOLO al agente de voz.
Rutas: /voice/twiml, /voice/stream (WebSocket), /voice/debug, /health
"""
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.routing import WebSocketRoute as _WSRoute

from voice_handler import twiml_handler, voice_stream_handler

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Mutuo Voice Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_api_route("/voice/twiml", twiml_handler, methods=["POST", "GET"])
app.router.routes.append(_WSRoute("/voice/stream", endpoint=voice_stream_handler))


@app.get("/health")
async def health():
    return {"status": "ok", "service": "voice-agent"}


@app.get("/voice/debug")
async def voice_debug():
    routes = [{"path": getattr(r, "path", "?"), "type": type(r).__name__} for r in app.router.routes]
    return {
        "routes": routes,
        "voice_stream_registered": any(getattr(r, "path", "") == "/voice/stream" for r in app.router.routes),
    }
