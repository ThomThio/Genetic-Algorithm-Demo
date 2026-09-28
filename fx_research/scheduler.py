"""Runs the scan on a schedule, like MT4-TradeSignals/main.py: an APScheduler
BlockingScheduler with cron triggers in Asia/Singapore time.

Instead of listing weekday hours by hand, it fires every hour (FXR_CRON_HOUR /
FXR_CRON_MINUTE) and skips runs while the FX market is closed
(Friday 17:00 to Sunday 17:00 New York time).

Also runs a second job every 15 minutes that scans USDSGD specifically for a
fighter-entry signal (see entry_watch.py) -- paper (analyse/decide/notify
only) unless started with --allow-live, in which case a signal only turns
into a real order when fx_research.trading_control is additionally armed
for USDSGD (same switch live_runner.py's web UI uses).

    python -m fx_research.scheduler                 # run forever, paper only
    python -m fx_research.scheduler --once           # one hourly scan now, then exit
    python -m fx_research.scheduler --allow-live     # run forever; USDSGD entry watch can place real orders when armed
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


def entry_watch_job(settings, allow_live, instrument="USDSGD", force=False):
    # Imported lazily: entry_watch imports notify from this module, so a
    # module-level import here would be circular.
    from .entry_watch import entry_watch

    if not force and not market_open():
        log.info("market closed, skipping %s entry watch", instrument)
        return
    try:
        entry_watch(instrument=instrument, settings=settings, allow_live=allow_live)
    except Exception as e:
        log.error("%s entry watch crashed: %s", instrument, e)
        notify(f"{instrument} entry watch crashed: {e}")


def build_scheduler(settings, allow_live=False):
    scheduler = BlockingScheduler(job_defaults={"misfire_grace_time": 15 * 60, "coalesce": True,
                                                "max_instances": 1})
    trigger = CronTrigger(hour=settings.cron_hour, minute=settings.cron_minute, timezone=settings.timezone)
    scheduler.add_job(scan_job, trigger, args=[settings], id="fx_research_scan")

    entry_trigger = CronTrigger(minute="*/15", timezone=settings.timezone)
    scheduler.add_job(entry_watch_job, entry_trigger, args=[settings, allow_live],
                      id="usdsgd_entry_watch_15min")

    def listener(event):
        if event.exception:
            log.error("job %s crashed: %s", event.job_id, event.exception)
            if event.job_id == "fx_research_scan":
                notify(f"FX research scan crashed: {event.exception}")
        elif event.job_id == "fx_research_scan":
            job = scheduler.get_job("fx_research_scan")
            if job and job.next_run_time:
                log.info("next scan at %s", job.next_run_time)

    scheduler.add_listener(listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    return scheduler


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="run one hourly scan now (even if the market is closed)")
    ap.add_argument("--allow-live", action="store_true",
                     help="let the USDSGD entry watch place real orders when fx_research.trading_control "
                          "is armed for it (default: paper -- analyse, decide, and notify, never order)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    if args.once:
        scan_job(settings, force=True)
        return
    scheduler = build_scheduler(settings, allow_live=args.allow_live)
    log.info("scheduler started (%s, hourly scan hour=%s minute=%s; USDSGD entry watch every 15min, "
             "allow_live=%s)", settings.timezone, settings.cron_hour, settings.cron_minute, args.allow_live)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler stopped")


if __name__ == "__main__":
    main()
