# agent/cost_control.py — Control de costos de API
# Mutuo Fintech — Origen IA
#
# Estrategias:
# 1. Haiku para mensajes simples, Sonnet solo para negociación/cierre
# 2. Respuestas sin IA para patrones obvios
# 3. Limitar historial de conversación
# 4. Tracking de costos por sesión

import re
import logging
from datetime import datetime, timezone

logger = logging.getLogger("origen-ai")

# Precios por 1M tokens (USD) — Anthropic pricing
PRICING = {
    "claude-haiku-4-5-20251001":  {"input": 0.80,  "output": 4.00},
    "claude-sonnet-4-5":           {"input": 3.00,  "output": 15.00},
}

# Modelo barato para mensajes simples
MODEL_CHEAP = "claude-haiku-4-5-20251001"
MODEL_SMART = "claude-sonnet-4-5"

# Máximo de mensajes en historial (pares user/assistant)
MAX_HISTORY_PAIRS = 6  # últimos 6 intercambios = 12 mensajes

# ── Respuestas sin IA (costo $0) ──────────────────
NO_AI_PATTERNS: list[tuple[list[str], str]] = [
    # Despedidas
    (["gracias", "muchas gracias", "thank"], "¡Con gusto! Si necesitas algo más, aquí estoy 😊"),
    (["chao", "bye", "adios", "adiós", "hasta luego"], "¡Chao! Que tengas un excelente día 🙌"),
    # Rechazo claro
    (["no me escribas", "no me llames", "no me contacten", "bloqueado"], "¡Dale, disculpa la molestia! Que estés bien 🙌"),
    # Insultos
    (["spam", "basura", "estafa", "fraude"], "Disculpa si te molesté. No te vuelvo a escribir. ¡Que estés bien!"),
]


def try_no_ai_response(mensaje: str) -> str | None:
    """Si el mensaje coincide con un patrón simple, responde sin IA."""
    msg = mensaje.lower().strip()
    # Mensajes muy cortos de rechazo
    if msg in ("no", "no gracias", "no me interesa", "no quiero"):
        return "¡Dale, gracias por tu tiempo! Que estés bien 🙌"

    for keywords, response in NO_AI_PATTERNS:
        if any(kw in msg for kw in keywords):
            return response

    return None


def select_model(mensaje: str, temperatura: int, estado: str, turnos: int) -> str:
    """Selecciona el modelo más económico según el contexto.

    Haiku (~95% más barato) para:
    - Primeros 2 turnos (calificación simple)
    - Temperatura baja (< 4)
    - Mensajes cortos del cliente (< 20 chars)

    Sonnet para:
    - Negociación activa (temp >= 4)
    - Manejo de objeciones
    - Cierre de venta
    - Mensajes complejos del cliente
    """
    msg_len = len(mensaje.strip())

    # Siempre Sonnet para cierre y negociación
    if estado in ("NEGOCIANDO", "CERRADO_GANADO", "OBJECION_ACTIVA"):
        return MODEL_SMART

    # Sonnet si temperatura alta (el cliente está caliente)
    if temperatura >= 5:
        return MODEL_SMART

    # Sonnet si el mensaje es complejo (pregunta elaborada)
    if msg_len > 80 or "?" in mensaje:
        return MODEL_SMART

    # Haiku para todo lo demás (apertura, calificación, respuestas cortas)
    return MODEL_CHEAP


def trim_history(historial: list, max_pairs: int = MAX_HISTORY_PAIRS) -> list:
    """Recorta el historial a los últimos N pares de mensajes.
    Siempre mantiene el primer par (apertura) + los últimos N-1 pares."""
    max_msgs = max_pairs * 2
    if len(historial) <= max_msgs:
        return historial

    # Mantener primeros 2 (apertura) + últimos max_msgs-2
    return historial[:2] + historial[-(max_msgs - 2):]


# ── Tracking de costos ─────────────────────────────
class CostTracker:
    """Rastrea costos de API por sesión y total."""

    def __init__(self):
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost_usd = 0.0
        self.calls_haiku = 0
        self.calls_sonnet = 0
        self.calls_skipped = 0  # Respuestas sin IA
        self.sessions = 0

    def record(self, model: str, input_tokens: int, output_tokens: int):
        pricing = PRICING.get(model, PRICING[MODEL_SMART])
        cost = (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000

        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_cost_usd += cost

        if model == MODEL_CHEAP:
            self.calls_haiku += 1
        else:
            self.calls_sonnet += 1

        return cost

    def record_skip(self):
        self.calls_skipped += 1

    def get_stats(self) -> dict:
        total_calls = self.calls_haiku + self.calls_sonnet + self.calls_skipped
        return {
            "total_cost_usd": round(self.total_cost_usd, 4),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "calls": {
                "total": total_calls,
                "haiku": self.calls_haiku,
                "sonnet": self.calls_sonnet,
                "skipped_no_ai": self.calls_skipped,
            },
            "avg_cost_per_call": round(self.total_cost_usd / max(total_calls, 1), 6),
            "savings_estimate": f"{round(self.calls_haiku / max(self.calls_haiku + self.calls_sonnet, 1) * 95)}% de llamadas con Haiku (95% más barato)",
        }

    def log_stats(self):
        s = self.get_stats()
        logger.info(
            f"[COSTS] ${s['total_cost_usd']} USD | "
            f"Haiku:{s['calls']['haiku']} Sonnet:{s['calls']['sonnet']} Skip:{s['calls']['skipped_no_ai']} | "
            f"Tokens: {s['total_input_tokens']}in/{s['total_output_tokens']}out"
        )
