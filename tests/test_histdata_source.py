import io
import zipfile
from datetime import date

import pandas as pd
import pytest

from data_sources.base import FetchRequest
from data_sources.histdata import HistDataSource


def _make_zip(csv_body: str, csv_name="DAT_ASCII_EURUSD_H1_2023.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(csv_name, csv_body)
    return buf.getvalue()


@pytest.mark.parametrize(
    "tag",
    [
        '<input id="tk" name="tk" type="hidden" value="abc123">',
        '<input type="hidden" value="abc123" name="tk">',
        "<input name='tk' value='abc123' type='hidden'>",
    ],
)
def test_extract_token_handles_attribute_order(tag):
    html = f"<form>{tag}<input name=\"date\" value=\"2023\"></form>"
    assert HistDataSource._extract_token(html) == "abc123"


def test_extract_token_missing_raises():
    with pytest.raises(RuntimeError):
        HistDataSource._extract_token("<form><input name=\"other\" value=\"x\"></form>")


def test_parse_zip_reads_semicolon_bars():
    csv_body = (
        "20230102 220000;1.06610;1.06682;1.06580;1.06655;0\n"
        "20230102 230000;1.06655;1.06700;1.06600;1.06620;0\n"
    )
    df = HistDataSource._parse_zip(_make_zip(csv_body))

    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 2
    assert df.index[0] == pd.Timestamp("2023-01-02 22:00:00")
    assert df.iloc[0]["close"] == 1.06655


def test_parse_zip_no_csv_raises():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "not a csv")
    with pytest.raises(RuntimeError):
        HistDataSource._parse_zip(buf.getvalue())


def test_fetch_filters_to_requested_range_and_normalizes(monkeypatch):
    csv_body = (
        "20221231 230000;1.0;1.1;0.9;1.05;0\n"  # outside range, previous year
        "20230102 220000;1.06610;1.06682;1.06580;1.06655;0\n"
        "20230102 220000;1.06611;1.06683;1.06581;1.06656;0\n"  # duplicate timestamp
    )

    source = HistDataSource()
    monkeypatch.setattr(
        source, "_fetch_year", lambda pair, timeframe, tf_path, year: HistDataSource._parse_zip(_make_zip(csv_body))
    )

    request = FetchRequest(
        instrument="EURUSD", timeframe="H1", start=date(2023, 1, 1), end=date(2023, 1, 31)
    )
    df = source.fetch(request)

    # the 2022 bar is filtered out, and the duplicate timestamp collapses to one row
    assert len(df) == 1
    assert df.index[0] == pd.Timestamp("2023-01-02 22:00:00", tz="UTC")
    assert df.iloc[0]["close"] == 1.06656  # keep="last" on the duplicate


def test_fetch_rejects_unsupported_timeframe():
    source = HistDataSource()
    request = FetchRequest(
        instrument="EURUSD", timeframe="M5", start=date(2023, 1, 1), end=date(2023, 1, 31)
    )
    with pytest.raises(ValueError):
        source.fetch(request)
