"""Runs the scan on a schedule, like MT4-TradeSignals/main.py: an APScheduler
BlockingScheduler with cron triggers in Asia/Singapore time.

Instead of listing weekday hours by hand, it fires every hour (FXR_CRON_HOUR /
FXR_CRON_MINUTE) and skips runs while the FX market is closed
(Friday 17:00 to Sunday 17:00 New York time).

    python -m fx_research.scheduler            # run forever
    python -m fx_research.scheduler --once     # one scan now, then exit
"""
import argparse
import json
import logging
import os
import urllib.parse
import urllib.request

import pandas as pd
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Settings
from .scan import run_scan, summarize

log = logging.getLogger("fx_research.scheduler")


def market_open(now=None):
    ny = (now or pd.Timestamp.now(tz="UTC")).tz_convert("America/New_York")
    wd, hour = ny.dayofweek, ny.hour
    if wd == 5:                       # Saturday
        return False
    if wd == 4 and hour >= 17:        # Friday after the close
        return False
    if wd == 6 and hour < 17:         # Sunday before the open
        return False
    return True


def notify(text):
    """Optional Telegram message (same bot/chat setup as the other repos, via env vars)."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    body = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000]}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=10)
    except Exception as e:  # never let a notification failure kill the scheduler
        log.warning("telegram notify failed: %s", e)


def scan_job(settings=None, force=False):
    if not force and not market_open():
        log.info("market closed, skipping scan")
        return
    run_id, results, errors = run_scan(settings)
    msg = f"FX research scan {run_id[:8]}\n{summarize(results)}"
    if errors:
        msg += "\nErrors: " + json.dumps(errors)
    log.info(msg)
    notify(msg)


def build_scheduler(settings):
    scheduler = BlockingScheduler(job_defaults={"misfire_grace_time": 15 * 60, "coalesce": True,
                                                "max_instances": 1})
    trigger = CronTrigger(hour=settings.cron_hour, minute=settings.cron_minute, timezone=settings.timezone)
    scheduler.add_job(scan_job, trigger, args=[settings], id="fx_research_scan")

    def listener(event):
        if event.exception:
            log.error("scan job crashed: %s", event.exception)
            notify(f"FX research scan crashed: {event.exception}")
        else:
            job = scheduler.get_job("fx_research_scan")
            if job and job.next_run_time:
                log.info("next scan at %s", job.next_run_time)

    scheduler.add_listener(listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    return scheduler


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="run one scan now (even if the market is closed)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    if args.once:
        scan_job(settings, force=True)
        return
    scheduler = build_scheduler(settings)
    log.info("scheduler started (%s, hour=%s minute=%s)", settings.timezone, settings.cron_hour,
             settings.cron_minute)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler stopped")


if __name__ == "__main__":
    main()
