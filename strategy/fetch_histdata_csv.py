#!/usr/bin/env python3
"""Mirrors free HistData.com 1-minute bar data to the local price cache,
resampled to H1, so it plugs into --source local exactly like
fetch_supabase_csv.py's output does.

HistData.com (https://www.histdata.com) publishes free 1-minute ASCII/CSV
forex bars back to ~2000-2003 for most majors and several minors -- USD/SGD
and SGD/JPY included -- plus a handful of commodities/indices (XAU/USD,
XAG/USD gold/silver, WTI/USD and BCO/USD crude oil, and a few equity
indices) on the same free, no-signup, no-API-key terms. No account needed.

This reproduces HistData's documented download flow (load the month's page
to grab a one-time token, then POST that token to get.php to receive the
ZIP) -- the same flow used by long-standing open-source downloaders such as
philipperemy/FX-1-Minute-Data.

*** CAVEAT ***: this was written without being able to reach histdata.com
from this sandboxed environment (its network egress policy blocks the host,
same as it blocks Supabase) -- so the token-scraping regex below is based on
HistData's long-documented page structure, not verified against a live
response just now. If a fetch fails with "could not find download token",
open one month's page in a browser, view source, and update _TOKEN_RE to
match the current hidden <input name="tk"> field.

Timezone: HistData timestamps are documented as fixed Eastern Standard Time
(UTC-5) with NO daylight-saving adjustment, year round. This module converts
them to naive UTC before caching. That means an hour boundary here will NOT
line up with FTMO MT4 broker-time bars (typically UTC+2/+3, DST-shifting) --
keep them as distinct Source tags ("histdata" vs "FTMO_MT4_demo") exactly as
the schema already supports; never concatenate the two into one series.

Usage:
    python3 -m strategy.fetch_histdata_csv --symbol USDSGD --years 20
    python3 -m strategy.run_discovery --source local --timeframe H1 \
        --source-name histdata --symbols USDSGD
"""
import argparse
import io
import re
import sys
import time
import zipfile
from datetime import date

import pandas as pd

from . import data as data_mod

BASE_URL = 'https://www.histdata.com'
PAGE_URL = BASE_URL + '/download-free-forex-data/?/ascii/1-minute-bar-quotes/{pair}/{year}/{month}'
POST_URL = BASE_URL + '/get.php'
_TOKEN_RE = re.compile(r'id=["\']tk["\']\s+value=["\']([^"\']+)["\']')
HISTDATA_SOURCE_NAME = 'histdata'
HISTDATA_TZ_OFFSET_HOURS = 5  # fixed EST (UTC-5), no DST, per HistData's documentation


def _month_range(years: int, end: date = None):
    """Ascending list of (year, month) for the last `years` complete
    calendar months up to (not including) the current partial month."""
    end = end or date.today()
    months = []
    y, m = end.year, end.month - 1
    if m == 0:
        y, m = y - 1, 12
    for _ in range(years * 12):
        months.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(months))


def _fetch_month_zip(session, pair_lower: str, pair_upper: str, year: int, month: int) -> bytes:
    import requests  # imported lazily: optional dependency, only needed for this module

    page_url = PAGE_URL.format(pair=pair_lower, year=year, month=month)
    page = session.get(page_url, timeout=30)
    page.raise_for_status()
    m = _TOKEN_RE.search(page.text)
    if not m:
        raise RuntimeError(
            f'could not find download token on {page_url} -- HistData may have changed its '
            f'page structure, or this pair/month has no data. See the CAVEAT in this module.'
        )
    token = m.group(1)

    resp = session.post(POST_URL, headers={'Referer': page_url}, data={
        'tk': token,
        'date': f'{year}{month:02d}',
        'datemonth': f'{year}{month:02d}',
        'platform': 'ASCII',
        'timeframe': 'M1',
        'fxpair': pair_upper,
        'head': '',
    }, timeout=60)
    resp.raise_for_status()
    if b'PK' != resp.content[:2]:
        raise RuntimeError(
            f'{page_url} did not return a ZIP (likely no data for this pair/month, or the '
            f'token flow needs updating) -- got {len(resp.content)} bytes, '
            f"content-type={resp.headers.get('content-type')}"
        )
    return resp.content


def _parse_month_zip(zip_bytes: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith('.csv')]
        if not csv_names:
            raise RuntimeError('zip contained no .csv file')
        with zf.open(csv_names[0]) as f:
            df = pd.read_csv(f, sep=';', header=None,
                              names=['dt', 'open', 'high', 'low', 'close', 'volume'])
    df['dt'] = pd.to_datetime(df['dt'], format='%Y%m%d %H%M%S')
    df = df.set_index('dt')
    return df


def fetch_symbol_histdata(symbol: str, years: int = 20, sleep_seconds: float = 1.0,
                           progress_cb=None) -> pd.DataFrame:
    """Downloads `years` of 1-minute HistData bars for `symbol`, converts to
    naive UTC, and resamples to H1. Skips (with a warning) any month that
    isn't available -- common at the start of a pair's history."""
    import requests

    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; strategy-discovery/1.0)'})

    pair_lower = symbol.lower()
    pair_upper = symbol.upper()
    frames = []
    months = _month_range(years)
    for i, (year, month) in enumerate(months):
        try:
            zip_bytes = _fetch_month_zip(session, pair_lower, pair_upper, year, month)
            frames.append(_parse_month_zip(zip_bytes))
        except Exception as e:  # noqa: BLE001 -- deliberately broad: keep going across 240 months
            if progress_cb:
                progress_cb(year, month, None, str(e))
        else:
            if progress_cb:
                progress_cb(year, month, len(frames[-1]), None)
        time.sleep(sleep_seconds)  # be polite to a free, no-auth public service

    if not frames:
        raise ValueError(f'no HistData months could be fetched for {symbol} -- see warnings above')

    m1 = pd.concat(frames).sort_index()
    m1 = m1[~m1.index.duplicated(keep='first')]
    m1.index = m1.index + pd.Timedelta(hours=HISTDATA_TZ_OFFSET_HOURS)  # fixed EST (UTC-5) -> naive UTC

    h1 = m1.resample('1h', label='left', closed='left').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum',
    }).dropna(subset=['open', 'high', 'low', 'close'])
    return h1


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--symbol', required=True, help='e.g. USDSGD, EURUSD, XAUUSD')
    p.add_argument('--years', type=int, default=20)
    p.add_argument('--sleep', type=float, default=1.0, help='seconds between month requests')
    p.add_argument('--cache-dir', default=data_mod.DEFAULT_LOCAL_DIR)
    p.add_argument('--source-name', default=HISTDATA_SOURCE_NAME)
    args = p.parse_args()

    def progress(year, month, n_bars, error):
        if error:
            print(f'  {year}-{month:02d}: skipped ({error})', file=sys.stderr)
        else:
            print(f'  {year}-{month:02d}: {n_bars} M1 bars', file=sys.stderr)

    print(f'Fetching {args.years} years of {args.symbol} from HistData.com (free, no signup)...',
          file=sys.stderr)
    df = fetch_symbol_histdata(args.symbol, years=args.years, sleep_seconds=args.sleep,
                                progress_cb=progress)
    path = data_mod.save_to_local_cache(df, args.symbol.upper(), 'H1', args.source_name, args.cache_dir)
    print(f'\nWrote {len(df)} H1 bars [{df.index[0]} .. {df.index[-1]}] -> {path}', file=sys.stderr)
    print('Run discovery against it with:', file=sys.stderr)
    print(f'  python3 -m strategy.run_discovery --source local --timeframe H1 '
          f'--source-name {args.source_name} --symbols {args.symbol.upper()}', file=sys.stderr)


if __name__ == '__main__':
    main()
