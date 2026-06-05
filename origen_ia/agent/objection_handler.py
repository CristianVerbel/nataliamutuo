# agent/objection_handler.py — Motor de objeciones para plan exequial

OBJECIONES = {
    "precio_alto": [
        (
            "Entiendo. Pero mira, son menos de mil pesos al dia para proteger "
            "a toda tu familia. Un evento sin plan puede costar mas de $5 millones."
        ),
        (
            "Con que lo comparas? Piensa que por lo que cuesta un cafe al dia, "
            "tienes a toda tu familia protegida con cobertura nacional."
        ),
        (
            "Si el precio no fuera un tema, lo tomarias? "
            "Entonces miremos el Plan Esencial que arranca en $25.000/mes."
        ),
    ],
    "ya_tiene_plan": [
        (
            "Que bueno que ya tienes proteccion! Y cubre a toda tu familia? "
            "El nuestro incluye hasta 9 personas con cobertura nacional y beneficios exclusivos."
        ),
        (
            "Muchos de nuestros afiliados tenian otro plan antes. Lo que los convenció "
            "es la cobertura nacional con Los Olivos y los beneficios adicionales."
        ),
        (
            "Te propongo algo: comparalo con lo que tienes hoy. Si el nuestro "
            "no te ofrece mas, no pierdes nada. Pero al menos que lo conozcas."
        ),
    ],
    "no_me_interesa": [
        (
            "Entiendo, es una decision personal e importante. "
            "Si en algun momento quieres informarte, aqui estoy."
        ),
        (
            "Sin problema. Solo queria que conocieras esta opcion para tu familia. "
            "Que estes muy bien!"
        ),
        (
            "Respeto tu decision. Si cambias de opinion, escribeme. Que estes bien!"
        ),
    ],
    "es_de_mala_suerte": [
        (
            "Entiendo que es un tema delicado. Pero justamente se trata de prevencion "
            "y tranquilidad, como un seguro del carro. Es proteger a los que mas quieres."
        ),
        (
            "Muchas personas piensan eso al principio. Pero cuando ven que incluye "
            "descuentos y beneficios para toda la familia, lo ven como un "
            "club de bienestar, no solo proteccion."
        ),
        (
            "Es como la proteccion que le das a tu casa o a tu carro. Es estar preparado "
            "para que tu familia no tenga que preocuparse por nada. Tranquilidad."
        ),
    ],
    "tengo_que_consultarlo": [
        (
            "Claro. Que necesitas confirmar? A veces puedo ayudarte a resolver eso "
            "ahora mismo para que no tengas que esperar."
        ),
        (
            "Entiendo. La buena noticia es que la afiliacion es digital y se puede "
            "cancelar. No hay compromiso a largo plazo."
        ),
        (
            "Perfecto. Te parece si te escribo manana para que me cuentes que decidieron? "
            "Asi no se te pasa."
        ),
    ],
    "no_tengo_tiempo": [
        (
            "Son solo 5 minutos, todo es digital. Te cuento lo basico y decides."
        ),
        (
            "Entiendo que estas ocupado. Cuando te queda mejor que te contacte? "
            "Asi respeto tu tiempo."
        ),
        (
            "Solo una pregunta rapida: cuantas personas hay en tu familia? "
            "Con eso te digo cuanto te saldria la proteccion."
        ),
    ],
    "no_confio": [
        (
            "Es normal querer estar seguro. Mutuo trabaja con Los Olivos, la red funeraria "
            "mas grande de Colombia. Llevamos anos protegiendo familias."
        ),
        (
            "Puedes verificarlo: somos Mutuo Fintech S.A.S., empresa registrada. "
            "Todos nuestros planes tienen respaldo de Los Olivos (Centralco Ltda)."
        ),
        (
            "Entiendo la desconfianza. Si quieres te envio mas informacion y lo revisas "
            "con calma. Sin ninguna presion."
        ),
    ],
    "soy_joven_no_necesito": [
        (
            "Justamente por eso es el mejor momento! El plan cubre a toda tu familia "
            "y sale mas barato que cuando eres mayor. Menos de mil pesos al dia."
        ),
        (
            "No es solo para ti, es para proteger a tus papas, hermanos, o tu pareja. "
            "Ademas, el Plan Total incluye descuentos en McDonald's, Juan Valdez y PriceSmart."
        ),
        (
            "Piensa que tus papas o abuelos tambien quedan cubiertos. "
            "Es una forma de cuidarlos a ellos tambien."
        ),
    ],
}


class ObjectionHandler:
    """Maneja objeciones con respuestas rotativas."""

    def __init__(self):
        self.intentos = {}  # {tipo_objecion: numero_intento}

    def get_respuesta(self, tipo_objecion: str) -> str | None:
        """Retorna la siguiente respuesta para una objeción. None si se agotaron los intentos."""
        if tipo_objecion not in OBJECIONES:
            return None

        intento = self.intentos.get(tipo_objecion, 0)
        respuestas = OBJECIONES[tipo_objecion]

        if intento >= len(respuestas):
            return None  # Se agotaron los 3 intentos

        self.intentos[tipo_objecion] = intento + 1
        return respuestas[intento]

    def intentos_restantes(self, tipo_objecion: str) -> int:
        """Cuántos intentos quedan para una objeción."""
        intento = self.intentos.get(tipo_objecion, 0)
        total = len(OBJECIONES.get(tipo_objecion, []))
        return max(0, total - intento)

    def objeciones_agotadas(self, tipo_objecion: str) -> bool:
        """Si ya se agotaron los intentos para una objeción."""
        return self.intentos_restantes(tipo_objecion) == 0

    def to_dict(self) -> dict:
        return {"intentos_por_objecion": dict(self.intentos)}
