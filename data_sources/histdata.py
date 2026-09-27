"""Ingestion for histdata.com's free ASCII bar data.

histdata.com doesn't expose a plain download link: the download page for a
given (timeframe, pair, year) embeds a one-time form token, and the actual
ZIP is fetched by POSTing that token to get.php. This mirrors that flow:
    1. GET the download page and pull the "tk" token out of the form.
    2. POST the token + request params to get.php to receive the ZIP.
    3. Unzip and parse the semicolon-delimited "DAT_ASCII_*.csv" inside.

Only bar data (H1, M1) is supported - histdata.com's tick data uses a
different URL/file layout.
"""
import io
import re
import zipfile
from datetime import date
from typing import Optional

import pandas as pd
import requests

from .base import DataSource, FetchRequest

DOWNLOAD_PAGE = (
    "https://www.histdata.com/download-free-forex-data/"
    "?/ascii/{tf_path}/{pair}/{year}"
)
GET_URL = "https://www.histdata.com/get.php"

# histdata.com URL path segment for each timeframe we support.
TIMEFRAME_PATHS = {
    "H1": "1-hour-bar-quotes",
    "M1": "1-minute-bar-quotes",
}

_INPUT_TAG_RE = re.compile(r"<input[^>]+>", re.IGNORECASE)
_NAME_TK_RE = re.compile(r'name=["\']tk["\']', re.IGNORECASE)
_VALUE_RE = re.compile(r'value=["\']([^"\']*)["\']', re.IGNORECASE)


class HistDataSource(DataSource):
    name = "histdata"

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0"})

    def fetch(self, request: FetchRequest) -> pd.DataFrame:
        tf_path = TIMEFRAME_PATHS.get(request.timeframe)
        if tf_path is None:
            raise ValueError(
                f"histdata: unsupported timeframe {request.timeframe!r}, "
                f"expected one of {sorted(TIMEFRAME_PATHS)}"
            )

        pair = request.instrument.lower()
        frames = [
            self._fetch_year(pair, request.timeframe, tf_path, year)
            for year in range(request.start.year, request.end.year + 1)
        ]
        df = pd.concat(frames)
        df = df[
            (df.index.date >= request.start) & (df.index.date <= request.end)
        ]
        return self.normalize(df)

    def _fetch_year(self, pair: str, timeframe: str, tf_path: str, year: int) -> pd.DataFrame:
        page_url = DOWNLOAD_PAGE.format(tf_path=tf_path, pair=pair, year=year)
        page = self.session.get(page_url, timeout=30)
        page.raise_for_status()

        token = self._extract_token(page.text)
        form_data = {
            "tk": token,
            "date": str(year),
            "datemonth": "",
            "platform": "ASCII",
            "timeframe": timeframe,
            "fxpair": pair.upper(),
        }
        resp = self.session.post(
            GET_URL, data=form_data, headers={"Referer": page_url}, timeout=60
        )
        resp.raise_for_status()
        return self._parse_zip(resp.content)

    @staticmethod
    def _extract_token(html: str) -> str:
        """Find the "tk" hidden input's value, regardless of attribute order."""
        for tag in _INPUT_TAG_RE.findall(html):
            if _NAME_TK_RE.search(tag):
                match = _VALUE_RE.search(tag)
                if match:
                    return match.group(1)
        raise RuntimeError(
            "histdata: could not find the download token ('tk' input) on the "
            "page - the site layout may have changed"
        )

    @staticmethod
    def _parse_zip(content: bytes) -> pd.DataFrame:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not csv_names:
                raise RuntimeError("histdata: no CSV file found inside the downloaded ZIP")
            with zf.open(csv_names[0]) as fh:
                df = pd.read_csv(
                    fh,
                    sep=";",
                    header=None,
                    names=["timestamp", "open", "high", "low", "close", "volume"],
                )
        df["timestamp"] = pd.to_datetime(df["timestamp"], format="%Y%m%d %H%M%S")
        return df.set_index("timestamp")
