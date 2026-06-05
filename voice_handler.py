"""
voice_handler.py — Twilio Media Streams → Deepgram STT → Gemini → Google TTS
Natalia: agente de ventas de Mutuo, Club de Bienestar Familiar.

Flujo:
  1. Twilio llama al TwiML endpoint → retorna <Connect><Stream>
  2. Twilio abre WebSocket a /voice/stream
  3. Audio del cliente → Deepgram STT (nova-2, español, mulaw 8kHz)
  4. Transcripción → Gemini (con base de conocimiento dinámica de Supabase)
  5. Respuesta de Gemini → Google TTS → audio de vuelta a Twilio
  6. Al colgar: transcripción + resultado guardados en voice_call_sessions
  7. Si el cliente da sus datos: se crea b2c_affiliation en Supabase
"""
import os
import re
import json
import uuid
import asyncio
import logging
import base64
import httpx
from datetime import datetime, timezone
from fastapi import WebSocket, WebSocketDisconnect, Request
from fastapi.responses import PlainTextResponse

logger = logging.getLogger("mutuo-voice")

# ── Variables de entorno ──────────────────────────────────────────────────────
DEEPGRAM_API_KEY  = os.getenv("DEEPGRAM_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GOOGLE_TTS_KEY    = os.getenv("GOOGLE_TTS_API_KEY", "")
SERVER_URL       = os.getenv("SERVER_URL", "")
SUPABASE_URL     = os.getenv("SUPABASE_URL") or "https://bmrduogpzjhoxnygzkac.supabase.co"
SUPABASE_KEY     = (os.getenv("SUPABASE_SERVICE_ROLE_KEY")
                    or os.getenv("SUPABASE_KEY")
                    or os.getenv("SUPABASE_ANON_KEY")
                    or "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImJtcmR1b2dwempob3hueWd6a2FjIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzU1MTI0NzksImV4cCI6MjA5MTA4ODQ3OX0.U4EgK2BhW3ZFf0m63UXi2zGzmIUMCCCdwaXW8kH3vSc")

SUPABASE_HEADERS = {
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "apikey": SUPABASE_KEY,
    "Content-Type": "application/json",
}

# ── Prompt base (sin datos de producto — vienen de la KB de Supabase) ─────────
BASE_SYSTEM_PROMPT = """Eres Natalia, asesora de ventas de Mutuo, Club de Bienestar Familiar.
Tu misión: afiliar al cliente ANTES de colgar.

REGLAS DE CONVERSACIÓN:
- FRASES CORTAS. Máximo 2 oraciones por turno.
- NUNCA abandones la venta por una objeción. Mínimo 3 intentos antes de aceptar un "no".
- SOLO ESPAÑOL. Jamás uses "Got it", "Okay", "Understood" ni anglicismos.
- Si el usuario dice "(silencio)" es que no has escuchado nada. Repite tu última pregunta de forma diferente o di "¿Hola, me escucha?".
- Si detectas buzón de voz o silencio prolongado después de 3 intentos, di solo "Adiós" y termina.
- Al primer "Aló", "Bueno" o "Sí" — ATACA INMEDIATAMENTE con el pitch.
- USA SOLO información de la BASE DE CONOCIMIENTO. No inventes precios, beneficios ni políticas.

DATOS QUE DEBES RECOLECTAR PARA AFILIAR:
1. Nombre completo
2. Número de cédula
3. Teléfono de contacto
4. Plan elegido
5. Fecha de nacimiento
6. Correo electrónico (opcional pero deseable)

CUANDO TENGAS nombre + cédula + teléfono + plan, incluye al FINAL de tu respuesta:
<AFILIACION>{"nombre":"...","cedula":"...","telefono":"...","plan":"...","email":"...","fecha_nacimiento":"..."}</AFILIACION>

RESULTADO AL COLGAR — incluye al final de tu ÚLTIMO mensaje:
<RESULTADO>INTERESADO</RESULTADO>  — si quedó interesado pero no afilió
<RESULTADO>RECHAZA</RESULTADO>     — si claramente no quiere
<RESULTADO>BUZON_VOZ</RESULTADO>   — si fue buzón de voz
<RESULTADO>NO_CONTESTA</RESULTADO> — si no respondió
<RESULTADO>AFILIADO</RESULTADO>    — si dio todos los datos y se afilió"""


# ── Supabase helpers ──────────────────────────────────────────────────────────

async def _load_knowledge_base() -> str:
    """Carga la base de conocimiento dinámica desde la edge function de Supabase."""
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            res = await client.get(
                f"{SUPABASE_URL}/functions/v1/voice-knowledge-base",
                headers={"Authorization": f"Bearer {SUPABASE_KEY}"},
            )
            if res.status_code == 200:
                kb = res.text
                logger.info(f"📚 KB cargada ({len(kb)} chars)")
                return kb
            logger.warning(f"KB HTTP {res.status_code}: {res.text[:100]}")
    except Exception as e:
        logger.warning(f"KB load error: {e}")
    return ""


async def _update_session(session_id: str, data: dict):
    """Actualiza voice_call_sessions en Supabase."""
    if not session_id:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.patch(
                f"{SUPABASE_URL}/rest/v1/voice_call_sessions?id=eq.{session_id}",
                headers=SUPABASE_HEADERS,
                json=data,
            )
    except Exception as e:
        logger.error(f"Session update error: {e}")


async def _create_affiliation(session_id: str, lead_data: dict, call_sid: str):
    """Crea un registro de afiliación en b2c_affiliations."""
    try:
        nombre = lead_data.get("nombre", "")
        partes = nombre.strip().split(" ", 1)
        first_name = partes[0] if partes else ""
        last_name  = partes[1] if len(partes) > 1 else ""

        affil = {
            "id": str(uuid.uuid4()),
            "session_id": f"voice-{call_sid or session_id}",
            "first_name": first_name,
            "last_name": last_name,
            "document_number": lead_data.get("cedula", ""),
            "document_type": "CC",
            "phone": lead_data.get("telefono", ""),
            "email": lead_data.get("email", ""),
            "birth_date": lead_data.get("fecha_nacimiento") or None,
            "selected_plan": lead_data.get("plan", ""),
            "payment_status": "pending",
            "status": "in_progress",
            "current_step": 1,
        }
        async with httpx.AsyncClient(timeout=8) as client:
            res = await client.post(
                f"{SUPABASE_URL}/rest/v1/b2c_affiliations",
                headers={**SUPABASE_HEADERS, "Prefer": "return=representation"},
                json=affil,
            )
            if res.status_code in (200, 201):
                logger.info(f"✅ Afiliación creada: {res.json()}")
            else:
                logger.error(f"Affiliation create error {res.status_code}: {res.text[:200]}")
    except Exception as e:
        logger.error(f"Create affiliation error: {e}")


# ── TwiML handler ─────────────────────────────────────────────────────────────

async def twiml_handler(request: Request):
    params     = dict(request.query_params)
    session_id = params.get("session_id", "")
    lead_name  = params.get("lead_name", "Cliente")
    ws_url     = SERVER_URL.replace("https://", "wss://").replace("http://", "ws://")

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{ws_url}/voice/stream">
      <Parameter name="session_id" value="{session_id}" />
      <Parameter name="lead_name" value="{lead_name}" />
    </Stream>
  </Connect>
</Response>"""
    return PlainTextResponse(xml, media_type="text/xml")


# ── WebSocket stream handler ──────────────────────────────────────────────────

async def voice_stream_handler(websocket: WebSocket):
    await websocket.accept()
    logger.info("🔌 Nueva conexión de stream de voz")

    stream_sid        = None
    call_sid          = None
    session_id        = None
    lead_name         = "Cliente"
    transcript        = ""
    buffered_speech   = ""
    is_speaking       = False
    has_greeted       = False
    chat_history      = []
    transcript_lines  = []
    final_result      = None
    affiliation_done  = False
    dg_ws             = None
    dg_task           = None
    system_prompt     = BASE_SYSTEM_PROMPT
    last_user_turn    = {"t": asyncio.get_event_loop().time()}
    silence_reprompts = [0]
    playback_done     = asyncio.Event()
    playback_done.set()  # starts "done"

    # ── Deepgram init ─────────────────────────────────────────────────────────
    async def init_deepgram():
        nonlocal dg_ws
        if not DEEPGRAM_API_KEY:
            logger.error("DEEPGRAM_API_KEY no configurada")
            return
        import websockets as _ws
        url = (
            "wss://api.deepgram.com/v1/listen"
            "?model=nova-2&language=es&smart_format=true"
            "&encoding=mulaw&sample_rate=8000&channels=1"
            "&interim_results=true&endpointing=500"
        )
        auth = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}
        try:
            dg_ws = await _ws.connect(url, additional_headers=auth)
        except TypeError:
            dg_ws = await _ws.connect(url, extra_headers=auth)
        logger.info("🎙️ Deepgram STT conectado")

    # ── Deepgram reader ───────────────────────────────────────────────────────
    async def read_deepgram():
        nonlocal transcript, is_speaking, buffered_speech
        if not dg_ws:
            return
        try:
            async for msg in dg_ws:
                data     = json.loads(msg)
                msg_type = data.get("type")

                if msg_type == "Results":
                    alt        = data.get("channel", {}).get("alternatives", [{}])[0]
                    text       = alt.get("transcript", "").strip()
                    is_final   = data.get("is_final", False)
                    speech_fin = data.get("speech_final", False)

                    if text:
                        logger.info(f"📝 STT {'[FINAL]' if is_final else '[interim]'}: '{text}'")

                    if is_final and text:
                        transcript += " " + text
                        transcript  = transcript.strip()

                    # speech_final=True means endpointing detected end of utterance
                    # This is the trigger when using endpointing without utterance_end_ms
                    if speech_fin and transcript.strip():
                        utterance  = transcript.strip()
                        transcript = ""
                        logger.info(f"🗣️ SpeechFinal → respond: '{utterance}' is_speaking={is_speaking}")
                        if is_speaking:
                            # User interrupted — clear Twilio buffer and respond immediately
                            buffered_speech = utterance
                            playback_done.set()  # unblock speak() so respond() can finish
                            if stream_sid:
                                try:
                                    await websocket.send_text(json.dumps({
                                        "event": "clear", "streamSid": stream_sid
                                    }))
                                except Exception:
                                    pass
                        else:
                            asyncio.create_task(respond(utterance))

                elif msg_type == "UtteranceEnd":
                    # Fallback: if utterance_end_ms is configured
                    text       = transcript.strip()
                    transcript = ""
                    logger.info(f"🗣️ UtteranceEnd: '{text}' is_speaking={is_speaking}")
                    if text and len(text) > 1:
                        if is_speaking:
                            buffered_speech = text
                        else:
                            asyncio.create_task(respond(text))

        except Exception as e:
            logger.error(f"Deepgram read error: {e}")

    # ── Claude response ───────────────────────────────────────────────────────
    async def respond(user_text: str):
        nonlocal is_speaking, chat_history, buffered_speech, final_result, affiliation_done
        if is_speaking:
            buffered_speech = user_text
            return
        is_speaking = True
        last_user_turn["t"] = asyncio.get_event_loop().time()
        try:
            logger.info(f"🤔 Procesando: '{user_text}'")
            transcript_lines.append(f"Cliente: {user_text}")
            chat_history.append({"role": "user", "content": user_text})

            async with httpx.AsyncClient(timeout=12) as client:
                res = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 80,
                        "system": system_prompt + "\n\nIMPORTANTE: Responde en máximo 2 oraciones cortas. NO uses emojis ni asteriscos ni símbolos especiales. Haz UNA pregunta directa para continuar la conversación.",
                        "messages": chat_history,
                    },
                )
                data = res.json()

            reply_raw = data.get("content", [{}])[0].get("text", "").strip()
            if not reply_raw:
                logger.warning(f"Claude sin respuesta: {data}")
                return

            # Extraer marcadores antes de hablar
            result_match = re.search(r"<RESULTADO>(\w+)</RESULTADO>", reply_raw)
            if result_match:
                final_result = result_match.group(1)
                logger.info(f"🏷️ Resultado detectado: {final_result}")

            affil_match = re.search(r"<AFILIACION>(.+?)</AFILIACION>", reply_raw, re.DOTALL)
            if affil_match and not affiliation_done:
                try:
                    lead_data = json.loads(affil_match.group(1))
                    affiliation_done = True
                    asyncio.create_task(_create_affiliation(session_id, lead_data, call_sid))
                    logger.info(f"📋 Datos de afiliación detectados: {lead_data}")
                except Exception as e:
                    logger.error(f"Affiliation parse error: {e}")

            # Limpiar marcadores y emojis del texto hablado
            reply = re.sub(r"<(RESULTADO|AFILIACION)>.*?</(RESULTADO|AFILIACION)>", "", reply_raw, flags=re.DOTALL)
            reply = re.sub(r"[^\w\s\.,;:¿?¡!\-\(\)áéíóúüñÁÉÍÓÚÜÑ]", "", reply).strip()
            if not reply:
                return

            logger.info(f"💬 Natalia: {reply}")
            transcript_lines.append(f"Natalia: {reply}")
            chat_history.append({"role": "assistant", "content": reply_raw})
            await speak(reply)

        except Exception as e:
            logger.error(f"Claude error: {e}")
        finally:
            is_speaking = False
            if buffered_speech:
                pending = buffered_speech
                buffered_speech = ""
                logger.info(f"▶️ Procesando speech buffereado: '{pending}'")
                asyncio.create_task(respond(pending))

    # ── Google TTS → Twilio ───────────────────────────────────────────────────
    async def speak(text: str):
        if not stream_sid:
            return
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                res = await client.post(
                    f"https://texttospeech.googleapis.com/v1/text:synthesize?key={GOOGLE_TTS_KEY}",
                    json={
                        "input": {"text": text},
                        "voice": {
                            "languageCode": "es-US",
                            "name": "es-US-Journey-F",
                            "ssmlGender": "FEMALE",
                        },
                        "audioConfig": {
                            "audioEncoding": "MULAW",
                            "sampleRateHertz": 8000,
                        },
                    },
                )
                data = res.json()

            audio_b64 = data.get("audioContent")
            if not audio_b64:
                logger.error(f"TTS sin audio: {data}")
                return

            audio_bytes = base64.b64decode(audio_b64)
            # Send all chunks in one burst — Twilio buffers and paces playback.
            for i in range(0, len(audio_bytes), 160):
                await websocket.send_text(json.dumps({
                    "event":     "media",
                    "streamSid": stream_sid,
                    "media":     {"payload": base64.b64encode(audio_bytes[i:i+160]).decode()},
                }))
            # Send mark — Twilio echoes it back when playback is done.
            playback_done.clear()
            await websocket.send_text(json.dumps({
                "event": "mark", "streamSid": stream_sid, "mark": {"name": "tts_done"}
            }))
            # Wait for Twilio to confirm playback complete (audio duration + 5s buffer)
            audio_duration = len(audio_bytes) / 8000
            try:
                await asyncio.wait_for(playback_done.wait(), timeout=audio_duration + 5)
            except asyncio.TimeoutError:
                logger.warning("Playback mark timeout — continuing")

        except Exception as e:
            logger.error(f"TTS error: {e}")

    # ── Cargar KB y conectar Deepgram ─────────────────────────────────────────
    try:
        kb = await _load_knowledge_base()
        if kb:
            kb_trimmed = kb[:4000]  # voice doesn't need full KB — keep it tight
            system_prompt = BASE_SYSTEM_PROMPT + "\n\n=== BASE DE CONOCIMIENTO ===\n" + kb_trimmed
    except Exception as e:
        logger.warning(f"KB load failed: {e}")

    try:
        await init_deepgram()
    except Exception as e:
        logger.error(f"⚠️ Deepgram init falló (continuando sin STT): {e}")

    if dg_ws:
        dg_task = asyncio.create_task(read_deepgram())

    # ── Silence reprompt: habla de nuevo si no hay respuesta en 10s ──────────
    async def silence_watcher():
        await asyncio.sleep(12)  # esperar saludo inicial
        while True:
            await asyncio.sleep(5)
            idle = asyncio.get_event_loop().time() - last_user_turn["t"]
            if idle > 10 and not is_speaking and silence_reprompts[0] < 2:
                silence_reprompts[0] += 1
                logger.info(f"🔇 Silencio detectado ({idle:.0f}s), reprompt #{silence_reprompts[0]}")
                asyncio.create_task(respond("(silencio)"))
                last_user_turn["t"] = asyncio.get_event_loop().time()
            elif idle > 30 and silence_reprompts[0] >= 2:
                break  # dejar que la llamada se caiga sola

    asyncio.create_task(silence_watcher())

    # ── Loop principal de eventos Twilio ─────────────────────────────────────
    try:
        async for raw in websocket.iter_text():
            msg   = json.loads(raw)
            event = msg.get("event")

            if event == "start":
                stream_sid = msg["start"]["streamSid"]
                call_sid   = msg["start"].get("callSid", "")
                params     = msg["start"].get("customParameters", {})
                session_id = params.get("session_id", "")
                lead_name  = params.get("lead_name", "Cliente")
                logger.info(f"▶️ Stream sid={stream_sid} call={call_sid} session={session_id} lead={lead_name}")

                if not has_greeted:
                    has_greeted = True
                    await asyncio.sleep(0.8)
                    greeting = (
                        "Hola, buenos días. Le llama Natalia de Mutuo, Club de Bienestar Familiar. "
                        "Tiene un momentico para contarle cómo proteger a su familia?"
                    )
                    transcript_lines.append(f"Natalia: {greeting}")
                    chat_history.append({"role": "assistant", "content": greeting})
                    asyncio.create_task(speak(greeting))

            elif event == "media":
                payload = msg.get("media", {}).get("payload")
                if payload and dg_ws:
                    try:
                        await dg_ws.send(base64.b64decode(payload))
                    except Exception:
                        pass

            elif event == "mark":
                mark_name = msg.get("mark", {}).get("name", "")
                if mark_name == "tts_done":
                    playback_done.set()

            elif event == "stop":
                logger.info("⏹️ Stream detenido")
                break

    except WebSocketDisconnect:
        logger.info("WebSocket desconectado")
    except Exception as e:
        logger.error(f"Stream error: {e}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
        if dg_task:
            dg_task.cancel()
        if dg_ws:
            try:
                await dg_ws.close()
            except Exception:
                pass

        # Guardar transcripción y resultado en Supabase
        if session_id or call_sid:
            transcript_text = "\n".join(transcript_lines)
            update_data = {
                "transcript_text": transcript_text,
                "status": "completed",
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
            if final_result:
                update_data["result_tag"] = final_result
            if call_sid:
                update_data["twilio_call_sid"] = call_sid

            asyncio.create_task(_update_session(session_id, update_data))

        logger.info(f"🔚 Sesión terminada — session={session_id} call={call_sid} turns={len(transcript_lines)} result={final_result}")
