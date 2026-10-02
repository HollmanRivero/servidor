"""Daglig jobb: agenten gjør vedlikehold og skriver rapport til minnet.

Startes av main.py ved oppstart (startup-eventet). Tidspunktet styres av
DAILY_JOB_TIME i .env (standard 08:00, lokal tid på serveren).
"""
from datetime import datetime

from loguru import logger

import config
import memory

_scheduler = None


def run_daily_job() -> dict:
    """Utfører den daglige agentjobben og lagrer rapporten i minnet."""
    import agent

    provider = "deepseek" if config.DEEPSEEK_API_KEY else "gemini"
    if not (config.DEEPSEEK_API_KEY or config.GEMINI_API_KEY):
        return {"ok": False, "error": "Ingen AI-nøkkel konfigurert."}

    prompt = (
        "Dette er den daglige vedlikeholdsjobben. Gjør følgende: "
        "1) Sjekk serverens status (get_stats). "
        "2) Se hvilke filer som finnes i prosjektmappen (list_projects). "
        "3) Skriv et kort daglig notat i minnet med remember-verktøyet "
        "og key 'daily_report'. Rapporter kort på norsk hva du gjorde."
    )
    try:
        result = agent.run(provider, prompt, conversation_id="daily")
    except Exception as e:
        logger.error("Daglig jobb feilet: {}", e)
        return {"ok": False, "error": str(e)}

    day = datetime.now().strftime("%Y-%m-%d")
    memory.remember(f"daily_report_{day}", result.get("reply", ""), "note")
    return {"ok": True, "reply": result.get("reply")}


def start_scheduler():
    """Starter bakgrunnsplanleggeren (idempotent)."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        hour_s, minute_s = (config.DAILY_JOB_TIME or "08:00").split(":")
        hour, minute = int(hour_s), int(minute_s)

        _scheduler = BackgroundScheduler()
        _scheduler.add_job(run_daily_job, CronTrigger(hour=hour, minute=minute))
        _scheduler.start()
        logger.info("Daglig agentjobb planlagt kl. {:02d}:{:02d} (lokal tid).", hour, minute)
    except Exception as e:
        logger.warning("Kunne ikke starte scheduler: {}", e)

    return _scheduler
