# agent/audio_transcription.py — Transcripción de audios de WhatsApp
# Mutuo Fintech — Origen IA
#
# Descarga audios de WhatsApp (Whapi/Meta) y los transcribe usando
# OpenAI Whisper API o una alternativa local.

import os
import logging
import tempfile
import httpx

logger = logging.getLogger("origen-ai")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
WHAPI_TOKEN = os.getenv("WHAPI_TOKEN", "") or os.getenv("WHAPI_API_KEY", "")


async def download_audio(media_url: str) -> bytes | None:
    """Descarga el audio desde la URL proporcionada por Whapi/Meta."""
    if not media_url:
        return None
    try:
        headers = {}
        # Whapi requiere token para algunos endpoints protegidos
        if "whapi.cloud" in media_url and WHAPI_TOKEN:
            headers["Authorization"] = f"Bearer {WHAPI_TOKEN}"

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            r = await client.get(media_url, headers=headers)
            if r.status_code == 200:
                logger.info(f"[AUDIO] Descargado {len(r.content)} bytes")
                return r.content
            logger.error(f"[AUDIO] Error descargando: {r.status_code} {r.text[:200]}")
    except Exception as e:
        logger.error(f"[AUDIO] Excepción descargando: {e}")
    return None


async def transcribe_with_openai(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str | None:
    """Transcribe un audio usando OpenAI Whisper API.
    Costo: ~$0.006 USD por minuto (muy barato)."""
    if not OPENAI_API_KEY:
        logger.warning("[AUDIO] OPENAI_API_KEY no configurado")
        return None

    # Determinar extensión según mime
    ext_map = {
        "audio/ogg": "ogg",
        "audio/mpeg": "mp3",
        "audio/mp4": "m4a",
        "audio/wav": "wav",
        "audio/webm": "webm",
        "audio/x-opus+ogg": "ogg",
    }
    ext = ext_map.get(mime_type, "ogg")

    # Guardar en archivo temporal (Whisper API necesita archivo)
    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            with open(tmp_path, "rb") as f:
                files = {"file": (f"audio.{ext}", f, mime_type)}
                data = {
                    "model": "whisper-1",
                    "language": "es",
                    "response_format": "text",
                }
                headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
                r = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    files=files,
                    data=data,
                    headers=headers,
                )
                if r.status_code == 200:
                    text = r.text.strip()
                    logger.info(f"[AUDIO] Transcrito: {text[:100]}")
                    return text
                logger.error(f"[AUDIO] Whisper error {r.status_code}: {r.text[:300]}")
    except Exception as e:
        logger.error(f"[AUDIO] Excepción transcribiendo: {e}")
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
    return None


async def transcribe_audio(media_url: str, mime_type: str = "audio/ogg") -> str | None:
    """Función principal: descarga y transcribe un audio.
    Retorna el texto transcrito o None si falla."""
    if not media_url:
        return None

    audio_bytes = await download_audio(media_url)
    if not audio_bytes:
        return None

    text = await transcribe_with_openai(audio_bytes, mime_type)
    return text
