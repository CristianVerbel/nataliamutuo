#!/usr/bin/env python3
"""
tramitar_cancelacion_retroactiva.py

Registra retroactivamente una solicitud de cancelación (radicado) para un cliente
que pidió la baja por WhatsApp pero a quien el bot nunca le generó el radicado
(p.ej. el caso de Luis Rangel: pidió retiro y la afiliación quedó activa y
auto-renovándose).

Reutiliza la MISMA función del bot (crear_ticket_cancelacion), por lo que deja
todo consistente:
  - Crea el radicado en cancellation_requests (estado 'pendiente')
  - Frena cobros futuros (payment_transactions pendientes/vencidas -> cancelled)
  - Marca la afiliación como pending_cancellation = true (sigue activa hasta trámite)
  - Registra en payment_portfolio_history y affiliation_audit_log
  - Alerta a los administradores

Es idempotente: si ya existe un radicado 'pendiente' para esa afiliación, devuelve
ese mismo y no duplica.

Uso:
  cd whatsapp-bot
  # 1) Verificar primero (no escribe nada):
  SUPABASE_URL=xxx SUPABASE_SERVICE_ROLE_KEY=xxx \
    python scripts/tramitar_cancelacion_retroactiva.py --dry-run

  # 2) Ejecutar de verdad (requiere --yes):
  SUPABASE_URL=xxx SUPABASE_SERVICE_ROLE_KEY=xxx \
    python scripts/tramitar_cancelacion_retroactiva.py --yes

Parámetros (por defecto apuntan a Luis Rangel Pezzotti):
  --phone    Teléfono del titular (default: +573007117697)
  --cedula   Cédula del titular, usada como respaldo si no aparece por teléfono
             (default: 1002035312)
  --reason   Motivo registrado en el radicado
  --dry-run  Solo consulta y muestra la afiliación; NO escribe.
  --yes      Confirma la ejecución real (sin esto, no escribe).
"""

import argparse
import asyncio
import os
import sys

# Permitir importar el paquete `agent` ejecutando desde whatsapp-bot/ o desde la raíz.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass  # dotenv es opcional; las env vars pueden venir del entorno

# El módulo lee SUPABASE_URL/KEY al importarse, así que cargamos env ANTES.
if not os.getenv("SUPABASE_URL"):
    print("ERROR: falta SUPABASE_URL en el entorno.", file=sys.stderr)
    sys.exit(1)
if not (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_ANON_KEY")):
    print("ERROR: falta SUPABASE_SERVICE_ROLE_KEY (o SUPABASE_KEY) en el entorno.", file=sys.stderr)
    sys.exit(1)

from agent.mutuo_actions import (  # noqa: E402
    crear_ticket_cancelacion,
    consultar_estado_cuenta,
    consultar_cuenta_por_cedula,
)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Tramitar cancelación retroactiva (radicado).")
    parser.add_argument("--phone", default="+573007117697", help="Teléfono del titular")
    parser.add_argument("--cedula", default="1002035312", help="Cédula del titular (respaldo)")
    parser.add_argument(
        "--reason",
        default="Solicitud explícita de retiro por WhatsApp; se afilió con otra entidad. "
                "Radicado generado retroactivamente (el bot no lo había creado).",
        help="Motivo del radicado",
    )
    parser.add_argument("--dry-run", action="store_true", help="Solo consultar, no escribir")
    parser.add_argument("--yes", action="store_true", help="Confirmar la ejecución real")
    args = parser.parse_args()

    # 1) Localizar la afiliación (por teléfono, y si no, por cédula).
    estado = await consultar_estado_cuenta(args.phone)
    if not estado.get("found") and args.cedula:
        print(f"No se encontró por teléfono {args.phone}; intentando por cédula {args.cedula}...")
        estado = await consultar_cuenta_por_cedula(args.cedula)

    if not estado.get("found"):
        print("ERROR: no se encontró ninguna afiliación con esos datos.", file=sys.stderr)
        return 2

    print("── Afiliación encontrada ──────────────────────────────")
    print(f"  Titular        : {estado.get('name')}")
    print(f"  Affiliation ID : {estado.get('affiliation_id')}")
    print(f"  Plan           : {estado.get('plan')}")
    print(f"  Estado pago    : {estado.get('payment_status')}")
    print(f"  Activa         : {estado.get('is_active')}")
    print(f"  Cuotas pend.   : {estado.get('cuotas_pendientes')}  (deuda ~${estado.get('total_deuda')})")
    print("───────────────────────────────────────────────────────")

    if args.dry_run:
        print("\n[DRY-RUN] No se escribió nada. Vuelve a correr con --yes para tramitar el radicado.")
        return 0

    if not args.yes:
        print("\nFalta --yes. Por seguridad no se escribió nada.")
        print("Corre de nuevo con --yes para generar el radicado de cancelación.")
        return 0

    # 2) Generar el radicado (idempotente).
    result = await crear_ticket_cancelacion(
        phone=args.phone,
        reason=args.reason,
        retention_attempts=1,
        cedula=args.cedula,
    )

    if result.get("success"):
        ya = " (ya existía, no se duplicó)" if result.get("already_exists") else ""
        print(f"\n✅ Radicado de cancelación: {result.get('radicado')}{ya}")
        print("   Cobros futuros frenados y afiliación marcada como cancelación en trámite.")
        print("   La baja definitiva la confirma el equipo al tramitar el radicado en el panel.")
        return 0

    print(f"\n❌ No se pudo registrar la cancelación: {result.get('error')}", file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
