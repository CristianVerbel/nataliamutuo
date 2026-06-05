# agent/image_selector.py — Selector inteligente de imágenes de planes
# Mutuo Fintech — Origen IA
#
# Ejecuta ANTES de llamar al modelo de IA para determinar qué imagen enviar.
# Loguea cada selección para entrenamiento futuro.

import logging
from datetime import datetime, timezone

from origen_ia.config.products import PAQUETES, PLAN_ORDER

logger = logging.getLogger("origen-ai")

# Historial de selecciones para analytics
selection_log: list[dict] = []


class ImageSelector:
    """Selecciona la imagen del plan más relevante según el contexto."""

    def __init__(self):
        self.last_image_sent: str | None = None
        self.images_sent: list[str] = []

    def select(
        self,
        mensaje_cliente: str,
        paquete_recomendado: str,
        temperatura: int,
        perfil: dict,
        fase: str,
    ) -> dict | None:
        """
        Determina si se debe enviar una imagen y cuál.

        Returns:
            dict con {plan_key, nombre, imagen, precio} o None si no enviar imagen.
        """
        msg = mensaje_cliente.lower()

        # ── Regla 1: NO enviar imagen si ya se envió la misma ──
        # ── Regla 2: NO enviar imagen en fases muy tempranas ──
        if fase in ("PROSPECTO",):
            return None

        # ── Regla 3: Cliente pide ver planes/precios ──
        pide_ver = any(kw in msg for kw in [
            "precio", "cuanto", "cuánto", "plan", "paquete", "que incluye",
            "qué incluye", "ver", "muestra", "mostrar", "imagen", "foto",
            "oferta", "promocion", "promo", "opciones", "catalogo",
        ])

        # ── Regla 4: Trigger keywords de un plan específico ──
        plan_by_trigger = self._match_by_triggers(msg)

        # ── Regla 5: Pide ver todos los planes ──
        pide_todos = any(kw in msg for kw in [
            "todos los planes", "todas las opciones", "todos los paquetes",
            "que tienen", "qué tienen", "ver todo",
        ])

        # ── Decidir ──
        if pide_todos:
            return self._select_multiple(paquete_recomendado)

        if plan_by_trigger:
            return self._select_single(plan_by_trigger)

        if pide_ver:
            return self._select_single(paquete_recomendado)

        # ── Regla 6: Si temperatura >= 5 y nunca se envió imagen, enviar ──
        if temperatura >= 5 and not self.images_sent:
            return self._select_single(paquete_recomendado)

        # ── Regla 7: Si el modelo recomienda cambiar paquete, enviar el nuevo ──
        # (se maneja externamente tras la respuesta del modelo)

        return None

    def select_after_response(
        self,
        paquete_actual: str,
        paquete_anterior: str,
    ) -> dict | None:
        """Se ejecuta DESPUÉS de la respuesta del modelo.
        Si el paquete cambió, enviar la imagen del nuevo."""
        if paquete_actual != paquete_anterior and paquete_actual in PAQUETES:
            if paquete_actual not in self.images_sent:
                return self._select_single(paquete_actual)
        return None

    def _match_by_triggers(self, msg: str) -> str | None:
        """Busca un plan que coincida por triggers en el mensaje."""
        best_match = None
        best_count = 0

        for key, plan in PAQUETES.items():
            triggers = plan.get("triggers", [])
            count = sum(1 for t in triggers if t in msg)
            if count > best_count:
                best_count = count
                best_match = key

        return best_match if best_count > 0 else None

    def _select_single(self, plan_key: str) -> dict | None:
        """Selecciona un solo plan para enviar."""
        if plan_key not in PAQUETES:
            # Fallback al primer plan del orden
            plan_key = PLAN_ORDER[0]

        # No repetir la misma imagen consecutivamente
        if plan_key == self.last_image_sent:
            return None

        plan = PAQUETES[plan_key]
        self.last_image_sent = plan_key
        self.images_sent.append(plan_key)

        result = {
            "mode": "single",
            "plans": [{
                "key": plan_key,
                "nombre": plan["nombre"],
                "imagen": plan["imagen"],
                "precio": plan["precio"],
            }],
        }

        self._log_selection(plan_key, "single")
        return result

    def _select_multiple(self, recommended: str) -> dict:
        """Selecciona hasta 3 planes: el recomendado + 2 alternativas."""
        plans = []

        # Primero el recomendado
        if recommended in PAQUETES:
            plans.append(recommended)

        # Luego 2 alternativas: una más barata y una más cara
        rec_idx = PLAN_ORDER.index(recommended) if recommended in PLAN_ORDER else 3
        if rec_idx > 0:
            plans.append(PLAN_ORDER[rec_idx - 1])
        if rec_idx < len(PLAN_ORDER) - 1:
            plans.append(PLAN_ORDER[rec_idx + 1])

        # Máximo 3, sin repetir
        seen = set()
        unique = []
        for p in plans:
            if p not in seen:
                seen.add(p)
                unique.append(p)
        plans = unique[:3]

        result = {
            "mode": "multiple",
            "plans": [{
                "key": k,
                "nombre": PAQUETES[k]["nombre"],
                "imagen": PAQUETES[k]["imagen"],
                "precio": PAQUETES[k]["precio"],
            } for k in plans],
        }

        for k in plans:
            self.last_image_sent = k
            self.images_sent.append(k)
            self._log_selection(k, "multiple")

        return result

    def _log_selection(self, plan_key: str, mode: str):
        """Loguea selección para analytics y entrenamiento."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "plan": plan_key,
            "mode": mode,
            "converted": None,  # Se actualiza cuando se cierra la venta
        }
        selection_log.append(entry)
        logger.info(f"[IMAGE] Seleccionado: {plan_key} (mode={mode})")

    def mark_conversion(self, plan_key: str):
        """Marca la última selección de ese plan como conversión."""
        for entry in reversed(selection_log):
            if entry["plan"] == plan_key and entry["converted"] is None:
                entry["converted"] = True
                break
