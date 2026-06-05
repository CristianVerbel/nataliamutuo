# agent/disc_detector.py — Deteccion de perfil DISC

DISC_SIGNALS = {
    "D": {
        "senales": [
            "interrumpe", "pregunta precio directo", "cuanto es",
            "al grano", "no tengo tiempo", "rapido", "cuanto cuesta",
            "precio", "digame el precio", "no me de rodeos",
        ],
        "estilo_respuesta": {
            "tono": "conciso, directo, sin rodeos",
            "enfasis": "velocidad, eficiencia, no perder tiempo",
            "evitar": "rodeos, detalles tecnicos innecesarios",
            "cierre": "directo, sin preambulo",
            "ejemplo": "Te ahorro tiempo: este plan te da datos LIBRE. El precio es X. Te interesa?",
        },
    },
    "I": {
        "senales": [
            "cuenta anecdotas", "expresivo", "emojis", "jaja",
            "que chevere", "imaginate", "me encanta", "super",
            "genial", "wow", "que bien", "experiencia",
        ],
        "estilo_respuesta": {
            "tono": "entusiasta, usa historias breves",
            "enfasis": "entretenimiento, familia, lo que van a disfrutar",
            "evitar": "datos frios sin emocion",
            "cierre": "Imaginate el fin de semana de tu familia con todo eso.",
            "ejemplo": "Imaginate: peliculas, series, deportes en vivo, todo para la familia!",
        },
    },
    "S": {
        "senales": [
            "y si no me gusta", "que pasa si", "no estoy seguro",
            "debo pensarlo", "llevo anos con", "no me gustan los cambios",
            "es seguro", "garantia", "me preocupa",
        ],
        "estilo_respuesta": {
            "tono": "ir despacio, generar confianza primero",
            "enfasis": "estabilidad del servicio, soporte, garantia",
            "evitar": "presionar, urgencia agresiva",
            "cierre": "Muchos clientes como tu llevan anos con nosotros, conformes.",
            "ejemplo": "Tranquilo, el proceso es sencillo y te acompanamos en todo.",
        },
    },
    "C": {
        "senales": [
            "cuantos megas", "latencia", "garantia contractual",
            "contrato", "letra pequena", "comparar", "especificaciones",
            "tecnicamente", "fibra optica", "simetrico", "SLA",
        ],
        "estilo_respuesta": {
            "tono": "dar detalles tecnicos, ser preciso",
            "enfasis": "velocidad exacta, terminos del contrato, garantias",
            "evitar": "responder con emocion en vez de datos",
            "cierre": "Los numeros tienen sentido para ti?",
            "ejemplo": "Son X megas simetricos, fibra optica directa, SLA del 99.5%.",
        },
    },
}


def get_disc_style(perfil: str) -> dict:
    """Retorna el estilo de respuesta para un perfil DISC."""
    return DISC_SIGNALS.get(perfil, {}).get("estilo_respuesta", {})
