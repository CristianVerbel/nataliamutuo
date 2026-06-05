# agent/reports.py — Reportes automáticos de gestión
# Mutuo Fintech S.A.S. — Origen IA

import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta

import httpx

logger = logging.getLogger("mutuo-bot")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
COL_TZ = timezone(timedelta(hours=-5))

SB_HEADERS = {
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "apikey": SUPABASE_KEY,
    "Content-Type": "application/json",
}


async def fetch_report_stats(since: str = None, until: str = None, label: str = "Hoy") -> dict:
    """Genera estadísticas completas: hora actual + acumulado del día + pendientes anteriores."""
    if not SUPABASE_URL:
        return {}

    now_col = datetime.now(COL_TZ)
    today_start = now_col.replace(hour=0, minute=0, second=0).isoformat()
    hour_ago = (now_col - timedelta(hours=1)).isoformat()
    yesterday_end = now_col.replace(hour=0, minute=0, second=0).isoformat()

    if not since:
        since = today_start
    if not until:
        until = now_col.isoformat()

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            # Leads de la ÚLTIMA HORA
            r_hour = await c.get(
                f"{SUPABASE_URL}/rest/v1/whatsapp_conversations"
                f"?created_at=gte.{hour_ago}&select=id,status,messages_count",
                headers=SB_HEADERS,
            )
            hour_convs = r_hour.json() if r_hour.status_code == 200 else []

            # Leads del DÍA completo
            r_today = await c.get(
                f"{SUPABASE_URL}/rest/v1/whatsapp_conversations"
                f"?created_at=gte.{today_start}&select=id,status,messages_count",
                headers=SB_HEADERS,
            )
            today_convs = r_today.json() if r_today.status_code == 200 else []

            # Conversaciones con actividad HOY
            r_active_today = await c.get(
                f"{SUPABASE_URL}/rest/v1/whatsapp_conversations"
                f"?last_message_at=gte.{today_start}&select=id,status,messages_count",
                headers=SB_HEADERS,
            )
            active_today = r_active_today.json() if r_active_today.status_code == 200 else []

            # Estado global (TODOS)
            r_all = await c.get(
                f"{SUPABASE_URL}/rest/v1/whatsapp_conversations?select=id,status,messages_count",
                headers=SB_HEADERS,
            )
            all_convs = r_all.json() if r_all.status_code == 200 else []

            # Tareas pendientes HOY
            r_tasks_today = await c.get(
                f"{SUPABASE_URL}/rest/v1/customer_tasks?status=eq.pendiente&created_at=gte.{today_start}&select=id",
                headers=SB_HEADERS,
            )
            tasks_today = r_tasks_today.json() if r_tasks_today.status_code == 200 else []

            # Tareas pendientes de DÍAS ANTERIORES (creadas antes de hoy, aún pendientes)
            r_tasks_old = await c.get(
                f"{SUPABASE_URL}/rest/v1/customer_tasks?status=eq.pendiente&created_at=lt.{yesterday_end}&select=id",
                headers=SB_HEADERS,
            )
            tasks_old = r_tasks_old.json() if r_tasks_old.status_code == 200 else []

    except Exception as e:
        logger.error(f"[REPORT] Error fetching stats: {e}")
        return {}

    def count_by_status(convs):
        s = {}
        for c in convs:
            st = c.get("status", "nuevo")
            s[st] = s.get(st, 0) + 1
        return s

    hour_status = count_by_status(hour_convs)
    today_status = count_by_status(today_convs)
    global_status = count_by_status(all_convs)

    # Sin respuesta global
    never_replied = sum(1 for c in all_convs if (c.get("messages_count") or 0) <= 1 and c.get("status") != "descartado")

    # Respondieron hoy vs no
    respondieron_hoy = sum(1 for c in active_today if (c.get("messages_count") or 0) > 1)
    no_respondieron_hoy = sum(1 for c in active_today if (c.get("messages_count") or 0) <= 1)

    return {
        "label": label,
        "fecha": now_col.strftime("%d/%m/%Y %H:%M"),
        # ÚLTIMA HORA
        "hora_entrantes": len(hour_convs),
        "hora_calientes": hour_status.get("caliente", 0),
        "hora_agendados": hour_status.get("agendado", 0),
        "hora_convertidos": hour_status.get("convertido", 0),
        # ACUMULADO DEL DÍA
        "dia_entrantes": len(today_convs),
        "dia_en_progreso": today_status.get("en_progreso", 0),
        "dia_calientes": today_status.get("caliente", 0),
        "dia_agendados": today_status.get("agendado", 0),
        "dia_convertidos": today_status.get("convertido", 0),
        "dia_descartados": today_status.get("descartado", 0),
        # ACTIVIDAD HOY
        "activas_hoy": len(active_today),
        "respondieron_hoy": respondieron_hoy,
        "no_respondieron_hoy": no_respondieron_hoy,
        # ESTADO GLOBAL
        "total_leads": len(all_convs),
        "global_en_progreso": global_status.get("en_progreso", 0),
        "global_calientes": global_status.get("caliente", 0),
        "global_agendados": global_status.get("agendado", 0),
        "global_convertidos": global_status.get("convertido", 0),
        "global_descartados": global_status.get("descartado", 0),
        "global_sin_respuesta": never_replied,
        # TAREAS
        "tareas_hoy": len(tasks_today),
        "tareas_pendientes_anteriores": len(tasks_old),
        "tareas_total_pendientes": len(tasks_today) + len(tasks_old),
    }


def format_whatsapp_report(stats: dict) -> str:
    tareas_old = stats.get('tareas_pendientes_anteriores', 0)
    tareas_old_line = f"\n⚠️ *{tareas_old} tareas de días anteriores sin atender*" if tareas_old > 0 else ""

    return (
        f"📊 *REPORTE MUTUO*\n"
        f"📅 {stats.get('fecha', '')}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⏰ *ÚLTIMA HORA*\n"
        f"  📥 Entrantes: {stats.get('hora_entrantes', 0)}\n"
        f"  🔥 Calientes: {stats.get('hora_calientes', 0)}\n"
        f"  📅 Agendados: {stats.get('hora_agendados', 0)}\n"
        f"  ✅ Convertidos: {stats.get('hora_convertidos', 0)}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📆 *ACUMULADO DEL DÍA*\n"
        f"  📥 Entrantes: {stats.get('dia_entrantes', 0)}\n"
        f"  🔄 En gestión: {stats.get('dia_en_progreso', 0)}\n"
        f"  🔥 Calientes: {stats.get('dia_calientes', 0)}\n"
        f"  📅 Agendados: {stats.get('dia_agendados', 0)}\n"
        f"  ✅ Convertidos: {stats.get('dia_convertidos', 0)}\n"
        f"  ❌ Perdidos: {stats.get('dia_descartados', 0)}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💬 *ACTIVIDAD HOY*\n"
        f"  Conversaciones activas: {stats.get('activas_hoy', 0)}\n"
        f"  ✍️ Respondieron: {stats.get('respondieron_hoy', 0)}\n"
        f"  😶 No respondieron: {stats.get('no_respondieron_hoy', 0)}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📈 *GLOBAL ({stats.get('total_leads', 0)} leads)*\n"
        f"  🔄 En gestión: {stats.get('global_en_progreso', 0)}\n"
        f"  🔥 Calientes: {stats.get('global_calientes', 0)}\n"
        f"  📅 Agendados: {stats.get('global_agendados', 0)}\n"
        f"  ✅ Ganados: {stats.get('global_convertidos', 0)}\n"
        f"  ❌ Perdidos: {stats.get('global_descartados', 0)}\n"
        f"  😶 Sin respuesta: {stats.get('global_sin_respuesta', 0)}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📞 *TAREAS*\n"
        f"  Nuevas hoy: {stats.get('tareas_hoy', 0)}\n"
        f"  Total pendientes: {stats.get('tareas_total_pendientes', 0)}"
        f"{tareas_old_line}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🤖 Natalia · Origen AI"
    )


def format_email_report(stats: dict) -> str:
    tareas_old = stats.get('tareas_pendientes_anteriores', 0)
    alert = f'<tr style="background:#fef2f2;"><td style="padding:8px;" colspan="2"><strong>⚠️ {tareas_old} tareas de días anteriores sin atender</strong></td></tr>' if tareas_old > 0 else ""
    t = "border-collapse:collapse;width:100%;max-width:500px;font-family:Arial,sans-serif;"
    return f"""
    <h2>📊 Reporte MUTUO — {stats.get('fecha', '')}</h2>
    <h3>⏰ Última Hora</h3>
    <table style="{t}">
      <tr><td style="padding:8px;">📥 Entrantes</td><td style="padding:8px;font-weight:bold;">{stats.get('hora_entrantes', 0)}</td></tr>
      <tr style="background:#f0fdf4;"><td style="padding:8px;">🔥 Calientes</td><td style="padding:8px;color:#e74c3c;font-weight:bold;">{stats.get('hora_calientes', 0)}</td></tr>
      <tr><td style="padding:8px;">📅 Agendados</td><td style="padding:8px;">{stats.get('hora_agendados', 0)}</td></tr>
      <tr style="background:#f0fdf4;"><td style="padding:8px;">✅ Convertidos</td><td style="padding:8px;color:#27ae60;font-weight:bold;">{stats.get('hora_convertidos', 0)}</td></tr>
    </table>
    <h3>📆 Acumulado del Día</h3>
    <table style="{t}">
      <tr><td style="padding:8px;">📥 Entrantes</td><td style="padding:8px;font-weight:bold;">{stats.get('dia_entrantes', 0)}</td></tr>
      <tr style="background:#f0fdf4;"><td style="padding:8px;">🔄 En gestión</td><td style="padding:8px;">{stats.get('dia_en_progreso', 0)}</td></tr>
      <tr><td style="padding:8px;">🔥 Calientes</td><td style="padding:8px;color:#e74c3c;font-weight:bold;">{stats.get('dia_calientes', 0)}</td></tr>
      <tr style="background:#f0fdf4;"><td style="padding:8px;">📅 Agendados</td><td style="padding:8px;color:#8e44ad;font-weight:bold;">{stats.get('dia_agendados', 0)}</td></tr>
      <tr><td style="padding:8px;">✅ Convertidos</td><td style="padding:8px;color:#27ae60;font-weight:bold;">{stats.get('dia_convertidos', 0)}</td></tr>
      <tr style="background:#f0fdf4;"><td style="padding:8px;">❌ Perdidos</td><td style="padding:8px;">{stats.get('dia_descartados', 0)}</td></tr>
    </table>
    <h3>💬 Actividad Hoy</h3>
    <table style="{t}">
      <tr><td style="padding:8px;">Conversaciones activas</td><td style="padding:8px;font-weight:bold;">{stats.get('activas_hoy', 0)}</td></tr>
      <tr style="background:#f0fdf4;"><td style="padding:8px;">✍️ Respondieron</td><td style="padding:8px;">{stats.get('respondieron_hoy', 0)}</td></tr>
      <tr><td style="padding:8px;">😶 No respondieron</td><td style="padding:8px;">{stats.get('no_respondieron_hoy', 0)}</td></tr>
    </table>
    <h3>📈 Global ({stats.get('total_leads', 0)} leads)</h3>
    <table style="{t}">
      <tr><td style="padding:8px;">🔄 En gestión</td><td style="padding:8px;">{stats.get('global_en_progreso', 0)}</td></tr>
      <tr style="background:#f0fdf4;"><td style="padding:8px;">🔥 Calientes</td><td style="padding:8px;color:#e74c3c;font-weight:bold;">{stats.get('global_calientes', 0)}</td></tr>
      <tr><td style="padding:8px;">📅 Agendados</td><td style="padding:8px;">{stats.get('global_agendados', 0)}</td></tr>
      <tr style="background:#f0fdf4;"><td style="padding:8px;">✅ Ganados</td><td style="padding:8px;color:#27ae60;font-weight:bold;">{stats.get('global_convertidos', 0)}</td></tr>
      <tr><td style="padding:8px;">❌ Perdidos</td><td style="padding:8px;">{stats.get('global_descartados', 0)}</td></tr>
      <tr style="background:#f0fdf4;"><td style="padding:8px;">😶 Sin respuesta</td><td style="padding:8px;">{stats.get('global_sin_respuesta', 0)}</td></tr>
    </table>
    <h3>📞 Tareas</h3>
    <table style="{t}">
      <tr><td style="padding:8px;">Nuevas hoy</td><td style="padding:8px;font-weight:bold;">{stats.get('tareas_hoy', 0)}</td></tr>
      <tr style="background:#f0fdf4;"><td style="padding:8px;">Total pendientes</td><td style="padding:8px;font-weight:bold;">{stats.get('tareas_total_pendientes', 0)}</td></tr>
      {alert}
    </table>
    <p style="color:#888;font-size:12px;margin-top:16px;">🤖 Natalia · Origen AI · Mutuo Fintech S.A.S.</p>
    """


async def get_recipients() -> list[dict]:
    if not SUPABASE_URL:
        return []
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{SUPABASE_URL}/rest/v1/report_recipients?active=eq.true&select=*",
                headers=SB_HEADERS,
            )
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        logger.warning(f"[REPORT] Error fetching recipients: {e}")
    return []


async def send_whatsapp_report(proveedor, stats: dict):
    recipients = await get_recipients()
    wa = [r for r in recipients if r.get("receive_whatsapp") and r.get("phone")]
    if not wa:
        return
    msg = format_whatsapp_report(stats)
    for r in wa:
        phone = r["phone"].replace("+", "").replace(" ", "")
        if len(phone) == 10 and phone.startswith("3"):
            phone = "57" + phone
        try:
            await proveedor.enviar_mensaje(phone, msg)
            logger.info(f"[REPORT] WA → {r['name']} ({phone})")
        except Exception as e:
            logger.warning(f"[REPORT] WA error {phone}: {e}")


async def send_email_report(stats: dict):
    recipients = await get_recipients()
    emails = [r["email"] for r in recipients if r.get("receive_email") and r.get("email")]
    if not emails:
        return
    html = format_email_report(stats)
    subject = f"📊 Reporte MUTUO — {stats.get('label', '')} {stats.get('fecha', '')}"
    # Enviar vía Resend (el antiguo edge function 'send-otp' no existe → fallaba en silencio)
    from agent.alerting import send_email
    await send_email(emails, subject, html)


async def report_scheduler(proveedor):
    """Loop: WhatsApp cada hora (7am-7pm COL), email diario a las 7pm."""
    logger.info("[REPORT] Scheduler iniciado")
    while True:
        try:
            now = datetime.now(COL_TZ)
            if 7 <= now.hour < 19:
                # Reporte de la última hora
                since = (now - timedelta(hours=1)).isoformat()
                until = now.isoformat()
                stats = await fetch_report_stats(since, until, "Última hora")
                if stats:
                    await send_whatsapp_report(proveedor, stats)

                # Email diario a las 7pm
                if now.hour == 18 and now.minute < 5:
                    since_day = now.replace(hour=0, minute=0, second=0).isoformat()
                    daily = await fetch_report_stats(since_day, now.isoformat(), "Resumen del día")
                    if daily:
                        await send_email_report(daily)
        except Exception as e:
            logger.error(f"[REPORT] Scheduler error: {e}")
        await asyncio.sleep(3600)
