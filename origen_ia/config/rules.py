# config/rules.py — Reglas de negocio absolutas

REGLAS = {
    "primera_oferta": "SIEMPRE Triple + OTT",
    "no_mencionar_operador": True,
    "preguntas_por_turno": 1,
    "intentos_por_objecion": 3,
    "no_bajar_paquete_sin_objecion_precio": True,
    "datos_minimos_para_cierre": [
        "nombre_completo", "cedula", "direccion",
        "telefono", "franja_instalacion", "fecha"
    ],
    "temperatura_minima_para_cierre": 6,
    "no_revelar_que_eres_IA": True,
    "idioma": "espanol colombiano coloquial",
    "trato": "tuteo (tu, te, ti)",
    "tono": "calido, seguro, sin presion agresiva",
    "max_palabras_chat": 60,
    "max_palabras_voz": 25,
}

# Estados del lead
ESTADOS = [
    "PROSPECTO",
    "CONTACTADO",
    "CALIFICADO",
    "OFERTA_ENVIADA",
    "OBJECION_ACTIVA",
    "NEGOCIANDO",
    "CERRADO_GANADO",
    "CERRADO_PERDIDO",
]

# Transiciones validas
TRANSICIONES = {
    "PROSPECTO": ["CONTACTADO", "CERRADO_PERDIDO"],
    "CONTACTADO": ["CALIFICADO", "CERRADO_PERDIDO"],
    "CALIFICADO": ["OFERTA_ENVIADA", "CERRADO_PERDIDO"],
    "OFERTA_ENVIADA": ["OBJECION_ACTIVA", "NEGOCIANDO", "CERRADO_GANADO", "CERRADO_PERDIDO"],
    "OBJECION_ACTIVA": ["NEGOCIANDO", "OFERTA_ENVIADA", "CERRADO_PERDIDO"],
    "NEGOCIANDO": ["CERRADO_GANADO", "CERRADO_PERDIDO", "OBJECION_ACTIVA"],
    "CERRADO_GANADO": [],
    "CERRADO_PERDIDO": [],
}
