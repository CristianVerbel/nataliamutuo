# agent/core.py — Motor conversacional principal de ORIGEN IA (Mutuo Plan Exequial)

import os
import json
import logging
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

from origen_ia.agent.states import LeadStateMachine
from origen_ia.agent.profiler import ClientProfile
from origen_ia.agent.objection_handler import ObjectionHandler
from origen_ia.agent.recommender import PackageRecommender
from origen_ia.agent.image_selector import ImageSelector
from origen_ia.config.prompts import SYSTEM_PROMPT
from origen_ia.config.products import PAQUETES
from agent.cost_control import select_model, trim_history, try_no_ai_response, CostTracker

load_dotenv()
logger = logging.getLogger("origen-ai")

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
cost_tracker = CostTracker()


class OrigenIA:
    """Motor conversacional principal del agente de ventas."""

    def __init__(self, canal: str = "whatsapp"):
        self.canal = canal
        self.state_machine = LeadStateMachine()
        self.profile = ClientProfile()
        self.objection_handler = ObjectionHandler()
        self.recommender = PackageRecommender()
        self.image_selector = ImageSelector()
        self.historial = []
        self.turnos = 0
        self.intentos_cierre = 0
        self.campaign_context: str | None = None  # Contexto outbound si aplica
        self.is_outbound: bool = False  # True si Natalia abrió la conversación
        self.pending_images: dict | None = None  # Imágenes pendientes de enviar

    def _build_system_prompt(self) -> str:
        """Construye el system prompt con el contexto actual."""
        paquete = self.recommender.get_paquete_actual()
        base_prompt = SYSTEM_PROMPT.format(
            estado_actual=json.dumps(self.state_machine.to_dict(), ensure_ascii=False),
            perfil_cliente=self.profile.to_json(),
            objeciones=json.dumps(self.objection_handler.to_dict(), ensure_ascii=False),
            paquete_recomendado=json.dumps({
                "nombre": paquete["nombre"],
                "incluye": paquete["incluye"],
                "precio": paquete["precio"],
                "argumento": paquete["argumento"],
                "puede_bajar": self.recommender.puede_bajar(),
            }, ensure_ascii=False),
        )

        # Diferenciar INBOUND vs OUTBOUND
        if self.is_outbound and self.campaign_context:
            base_prompt += (
                "\n\n── MODO: OUTBOUND (TÚ ABRISTE LA CONVERSACIÓN) ──\n"
                "IMPORTANTE: Este cliente NO te escribió primero. TÚ le enviaste "
                "un mensaje de campaña y ahora está respondiendo.\n"
                "- Ya te presentaste como Natalia de Mutuo\n"
                "- NO te vuelvas a presentar ni repitas el mensaje de apertura\n"
                "- Continúa la conversación de forma natural desde donde quedó\n"
                "- Sé EXTRA empática porque llegaste sin ser solicitada\n"
                "- Si dice que no le interesa, respeta y despídete amablemente\n"
                "- Si dice 'quién eres' o 'de dónde sacaron mi número', explica que "
                "eres de Mutuo, Club de Bienestar Familiar, y que ofrecemos protección "
                "exequial para familias colombianas\n\n"
                + self.campaign_context
            )
        elif not self.is_outbound:
            base_prompt += (
                "\n\n── MODO: INBOUND (EL CLIENTE TE ESCRIBIÓ PRIMERO) ──\n"
                "El cliente te contactó por iniciativa propia. Está interesado.\n"
                "- Preséntate como Natalia de Mutuo, Club de Bienestar Familiar\n"
                "- Califica rápido: ¿de dónde escribe? ¿cuántas personas en su familia?\n"
                "- Ve directo a la oferta una vez tengas el perfil mínimo\n"
            )

        return base_prompt

    def _build_messages(self, mensaje_nuevo: str) -> list:
        """Construye la lista de mensajes para la API."""
        messages = []
        for msg in self.historial:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": mensaje_nuevo})
        return messages

    def _process_agent_response(self, raw_response: str) -> dict:
        """Parsea la respuesta JSON del agente."""
        # Intentar extraer JSON de la respuesta
        try:
            # Buscar JSON en la respuesta
            start = raw_response.find("{")
            end = raw_response.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(raw_response[start:end])
        except json.JSONDecodeError:
            pass

        # Si no es JSON, tratar como respuesta directa
        return {
            "thinking": "",
            "mensaje_cliente": raw_response,
            "nuevo_estado": self.state_machine.estado,
            "perfil_update": {},
            "temperatura": self.profile.temperatura,
            "objecion_detectada": None,
            "paquete_accion": "mantener",
            "datos_cierre_capturados": None,
        }

    def _update_state(self, response: dict):
        """Actualiza el estado interno basado en la respuesta del agente."""
        # Actualizar estado del lead
        nuevo_estado = response.get("nuevo_estado", self.state_machine.estado)
        if nuevo_estado != self.state_machine.estado:
            self.state_machine.transicionar(nuevo_estado)

        # Actualizar perfil
        perfil_update = response.get("perfil_update", {})
        if perfil_update:
            self.profile.update_from_dict(perfil_update)

        # Actualizar temperatura
        temp = response.get("temperatura")
        if temp is not None:
            self.profile.temperatura = int(temp)

        # Manejar objeciones
        objecion = response.get("objecion_detectada")
        if objecion and objecion != "null":
            if objecion not in self.profile.objeciones_detectadas:
                self.profile.objeciones_detectadas.append(objecion)

        # Manejar paquete
        paquete_accion = response.get("paquete_accion", "mantener")
        if paquete_accion == "bajar":
            self.recommender.bajar_paquete()
        elif paquete_accion == "subir":
            self.recommender.subir_paquete()

        # Si el modelo pidió enviar imagen, forzar el envío
        if response.get("enviar_imagen"):
            self.pending_images = self.image_selector._select_single(self.recommender.paquete_actual)

        # Capturar datos de cierre
        datos_cierre = response.get("datos_cierre_capturados")
        if datos_cierre and isinstance(datos_cierre, dict):
            self.profile.update_from_dict(datos_cierre)

        # Actualizar paquete recomendado en perfil
        self.profile.paquete_recomendado = self.recommender.get_paquete_actual()["nombre"]

    async def responder(self, mensaje: str) -> str:
        """Procesa un mensaje del cliente y genera respuesta. VERSIÓN SIMPLE & ROBUSTA."""
        self.turnos += 1

        # Primera interaccion: mover a CONTACTADO
        if self.state_machine.estado == "PROSPECTO":
            self.state_machine.transicionar("CONTACTADO")

        # ── Imagen: seleccionar ANTES para poder mandarla con respuesta ──
        pre_image = self.image_selector.select(
            mensaje_cliente=mensaje,
            paquete_recomendado=self.recommender.paquete_actual,
            temperatura=self.profile.temperatura,
            perfil=self.profile.to_dict(),
            fase=self.state_machine.estado,
        )

        # ── PROMPT SIMPLE (sin JSON, solo texto plano) ──
        paquete = self.recommender.get_paquete_actual()
        nombre = self.profile.nombre or ""
        ciudad = self.profile.ciudad or ""

        simple_prompt = f"""Eres Natalia, asesora de Mutuo, Club de Bienestar Familiar en Colombia.
Vendes planes exequiales prepagados con cobertura nacional a través de Los Olivos.

TONO: CÁLIDO, EMPÁTICO y PROTECTOR. NUNCA morbosa ni sombría.
NUNCA uses "muerte", "morir", "fallecer", "funeral", "ataúd" directamente.
USA: "protección", "tranquilidad", "prevención", "cuando llegue el momento".

DATOS DEL CLIENTE (úsalos, NO los preguntes de nuevo):
- Nombre: {nombre or 'desconocido'}
- Ciudad: {ciudad or 'desconocida'}

PLAN RECOMENDADO AHORA:
- {paquete.get('nombre')} por {paquete.get('precio')}/mes
- Cobertura: {paquete.get('cobertura', '')}
- Incluye: {', '.join(paquete.get('incluye', [])[:4])}

REGLAS:
- Responde SOLO con texto plano, máximo 2 oraciones (40 palabras max)
- Cálida, empática pero vendedora
- Si ya tienes el nombre, ÚSALO, no lo preguntas
- NO repitas preguntas ya hechas (lee el historial)
- Si cliente muestra interés → cierra con: "¿Te afilio? Son 5 minutos, 100% digital"
- Si dice NO → respeta y despídete: "Entiendo, es una decisión importante. Que estés bien!"
- Si pide precio → dale el precio del plan recomendado y cierra
- NO uses JSON, NO uses markdown, NO uses asteriscos
- Afiliación 100% digital, sin exámenes médicos, carencia 90 días

PLANES DISPONIBLES (si necesitas otro):
- Familia Esencial $25.000/mes (titular + 5 beneficiarios, 1 mascota incluida, Golden Offers, 1 evento/año)
- Familia Plus $29.900/mes (titular + 7 beneficiarios + 1 sin límite de edad, 1 mascota incluida, Golden Offers, 2 eventos/año) ← PRIMERA OFERTA
- Familia Total $38.000/mes (lo máximo: 2 sin límite de edad, exhumación, columbario, Golden Offers, eventos ilimitados; mascota con pago adicional)
- Adicional: $9.900/mes por persona extra

TODOS: Cobertura exequial nacional Los Olivos, Tarjeta Golden Offers, sin exámenes médicos, carencia 90 días desde la activación (eventos por año según el plan)
"""

        # Construir mensajes (historial + actual)
        messages = self._build_messages(mensaje)

        # Recortar historial si es muy largo (últimos 10 intercambios)
        if len(messages) > 20:
            messages = messages[-20:]

        # ── LLAMADA AL MODELO DE IA ──
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=500,
                system=simple_prompt,
                messages=messages,
            )
            raw = response.content[0].text.strip()

            # Limpiar si el modelo devolvió accidentalmente JSON
            if raw.startswith("{"):
                import re as _re
                match = _re.search(r'"mensaje_cliente"\s*:\s*"([^"]+)"', raw)
                if match:
                    raw = match.group(1)
                else:
                    # Eliminar llaves y parsear como texto
                    raw = _re.sub(r'[{}"]', '', raw).strip()

            mensaje_cliente = raw

            # Detectar interés (para métricas)
            msg_lower = mensaje.lower()
            if any(kw in msg_lower for kw in ["me interesa", "quiero", "si", "cuanto", "cuánto", "listo", "dale", "afiliame", "afíliame"]):
                if self.profile.temperatura < 7:
                    self.profile.temperatura = 7
                if self.state_machine.estado in ("PROSPECTO", "CONTACTADO"):
                    self.state_machine.transicionar("NEGOCIANDO")

            # Guardar en historial
            self.historial.append({"role": "user", "content": mensaje})
            self.historial.append({"role": "assistant", "content": mensaje_cliente})

            # Imagen: enviar si hay una pre-seleccionada o si respuesta menciona precio
            self.pending_images = pre_image
            if not self.pending_images and any(p in mensaje_cliente for p in ["$25", "$29", "$38", "$9.9"]):
                try:
                    self.pending_images = self.image_selector._select_single(self.recommender.paquete_actual)
                except Exception:
                    pass

            logger.info(
                f"[T{self.turnos}] Haiku Estado={self.state_machine.estado} "
                f"Temp={self.profile.temperatura} "
                f"({response.usage.input_tokens}in/{response.usage.output_tokens}out)"
            )

            return mensaje_cliente

        except Exception as e:
            import traceback
            logger.error(f"[ORIGEN IA] ERROR API tipo={type(e).__name__} msg={str(e)[:500]}")
            logger.error(f"[ORIGEN IA] Traceback: {traceback.format_exc()[:1000]}")

            # Intentar con otro modelo como fallback
            try:
                logger.info("[ORIGEN IA] Reintentando con modelo alternativo")
                response = await client.messages.create(
                    model="claude-sonnet-4-5",
                    max_tokens=500,
                    system=simple_prompt,
                    messages=messages,
                )
                raw = response.content[0].text.strip()
                self.historial.append({"role": "user", "content": mensaje})
                self.historial.append({"role": "assistant", "content": raw})
                logger.info(f"[T{self.turnos}] Sonnet retry OK")
                return raw
            except Exception as e2:
                logger.error(f"[ORIGEN IA] Sonnet retry FAILED: {type(e2).__name__}: {str(e2)[:300]}")

            # Último fallback context-aware
            if nombre:
                return f"{nombre.split()[0].title()}, dame un momentito que te confirmo los detalles"
            return f"Hola! Soy Natalia de Mutuo. Dame un momento, ya te respondo."

    def generar_registro_crm(self) -> dict:
        """Genera el objeto JSON para registro en CRM."""
        return {
            "agente": "Natalia",
            "canal": self.canal,
            "estado_final": self.state_machine.estado,
            "perfil_cliente": self.profile.to_dict(),
            "paquete_ofrecido": self.recommender.historial_ofertas[0] if self.recommender.historial_ofertas else "",
            "paquete_cerrado": self.recommender.paquete_actual if self.state_machine.estado == "CERRADO_GANADO" else "",
            "objeciones_encontradas": self.profile.objeciones_detectadas,
            "intentos_cierre": self.intentos_cierre,
            "temperatura_final": self.profile.temperatura,
            "duracion_conversacion_turnos": self.turnos,
            "motivo_perdida": "" if self.state_machine.estado != "CERRADO_PERDIDO" else "Ver objeciones",
            "datos_afiliacion": {
                "nombre": self.profile.nombre_completo,
                "cedula": self.profile.cedula,
                "telefono": self.profile.telefono,
                "email": self.profile.email,
                "fecha_nacimiento": self.profile.fecha_nacimiento,
            } if self.state_machine.estado == "CERRADO_GANADO" else {},
            "requiere_seguimiento_humano": (
                self.profile.temperatura >= 5
                and self.state_machine.estado not in ("CERRADO_GANADO", "CERRADO_PERDIDO")
            ),
            "notas_para_closer": self._generar_notas_closer(),
        }

    def _generar_notas_closer(self) -> str:
        """Genera resumen para el closer humano."""
        if self.profile.temperatura < 5:
            return ""
        return (
            f"Cliente: {self.profile.nombre or 'Sin nombre'}\n"
            f"Ciudad: {self.profile.ciudad or 'No identificada'}\n"
            f"Composición familiar: {self.profile.composicion_hogar or 'No identificada'}\n"
            f"Objeción principal: {self.profile.objeciones_detectadas[-1] if self.profile.objeciones_detectadas else 'Ninguna'}\n"
            f"Plan recomendado: {self.recommender.get_paquete_actual()['nombre']}\n"
            f"Temperatura: {self.profile.temperatura}/10\n"
            f"Turnos: {self.turnos}"
        )
