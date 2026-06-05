# agent/profiler.py — Perfil dinámico del cliente (Mutuo Plan Exequial)

import json


class ClientProfile:
    """Perfil dinámico que se actualiza durante la conversación."""

    def __init__(self):
        self.nombre = ""
        self.ciudad = ""
        self.barrio = ""
        self.estrato = None
        # Datos familiares (clave para plan exequial)
        self.composicion_hogar = ""
        self.num_beneficiarios = 0
        self.tiene_padres_mayores = None
        self.tiene_mascotas = None
        self.tiene_plan_funerario = None
        self.plan_funerario_actual = ""
        # Perfil de compra
        self.disposicion_pago = ""
        self.perfil_disc = ""
        self.temperatura = 0
        self.paquete_recomendado = ""
        self.objeciones_detectadas = []
        self.motivacion = ""  # qué lo motivó a preguntar
        # Datos de cierre
        self.nombre_completo = ""
        self.cedula = ""
        self.telefono = ""
        self.email = ""
        self.fecha_nacimiento = ""

    def update_from_dict(self, data: dict):
        """Actualiza el perfil con datos extraídos por el agente."""
        for key, value in data.items():
            if hasattr(self, key) and value is not None:
                if key == "objeciones_detectadas" and isinstance(value, list):
                    for obj in value:
                        if obj not in self.objeciones_detectadas:
                            self.objeciones_detectadas.append(obj)
                else:
                    setattr(self, key, value)

    def datos_cierre_completos(self) -> bool:
        """Verifica si se tienen los datos mínimos para cierre."""
        return all([
            self.nombre_completo,
            self.cedula,
            self.telefono,
        ])

    def datos_faltantes(self) -> list:
        """Retorna los datos que faltan para completar el cierre."""
        campos = {
            "nombre_completo": self.nombre_completo,
            "cedula": self.cedula,
            "telefono": self.telefono,
        }
        return [k for k, v in campos.items() if not v]

    def to_dict(self) -> dict:
        return {
            "nombre": self.nombre,
            "ciudad": self.ciudad,
            "barrio": self.barrio,
            "estrato": self.estrato,
            "composicion_hogar": self.composicion_hogar,
            "num_beneficiarios": self.num_beneficiarios,
            "tiene_padres_mayores": self.tiene_padres_mayores,
            "tiene_mascotas": self.tiene_mascotas,
            "tiene_plan_funerario": self.tiene_plan_funerario,
            "plan_funerario_actual": self.plan_funerario_actual,
            "disposicion_pago": self.disposicion_pago,
            "perfil_disc": self.perfil_disc,
            "temperatura": self.temperatura,
            "paquete_recomendado": self.paquete_recomendado,
            "objeciones_detectadas": self.objeciones_detectadas,
            "motivacion": self.motivacion,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
