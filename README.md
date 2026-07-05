# Mutuo WhatsApp Bot (Valeria)

Servicio **independiente** del bot de WhatsApp de Mutuo: conversación con IA (ventas,
soporte, acciones de afiliados), Messenger y voz. Corre como servicio propio (Railway)
y se integra con el sistema principal (mutuoventas) **solo por API/webhook** — si el
sistema principal se cae, el bot sigue conversando; si el bot se cae, el sistema sigue
vendiendo y cobrando.

## Arquitectura

```
WhatsApp (Whapi / Meta Cloud API)
        │  webhook entrante
        ▼
   este servicio (FastAPI, Railway)
        │  estado propio: conversaciones, sesiones, dedup
        ▼
   sistema principal (Supabase mutuoventas)
        - hoy: PostgREST + edge functions (whatsapp-sync, send-payment-receipt, …)
        - meta: un único gateway autenticado (ver docs/INTEGRACION.md)
```

## Correr local

```bash
pip install -r requirements.txt
cp .env.example .env   # completar credenciales
uvicorn agent.main:app --reload --port 8000
```

Docker: `docker build -t mutuo-bot . && docker run --env-file .env -p 8000:8000 mutuo-bot`

## Deploy (Railway)

El `Dockerfile` en la raíz es el que usa Railway. Variables en `.env.example`.
Webhook de WhatsApp del proveedor → `https://<dominio>/webhook`.

## Piezas

| Ruta | Qué es |
|---|---|
| `agent/main.py` | FastAPI: webhook, sesiones, watchdogs |
| `agent/brain.py` | Cerebro conversacional (Claude) |
| `agent/providers/` | Proveedores WhatsApp: `whapi`, `meta` (oficial), `twilio`, `messenger` |
| `agent/mutuo_actions.py` | Acciones de negocio (consultas de afiliado, pagos) |
| `agent/outbound.py` | Campañas salientes con pacing anti-ban (30–60s) |
| `origen_ia/` | Prompts y configuración de campañas |
| `voice_main.py` / `voice_handler.py` | Canal de voz |
| `messenger_main.py` | Canal Facebook Messenger |

## Integración con el sistema principal

Ver **`docs/INTEGRACION.md`**: inventario completo de las tablas y edge functions
que el bot consume hoy, y el plan por fases para llevar el estado del bot a su
propia base de datos y el acceso a datos de negocio a un gateway autenticado.
