"""Tests for fx_research.execute.scan_and_execute() and the entry_watch/
scheduler wiring on top of it -- the orchestration added for the USDSGD
15-minute live-trading feature. No real MT4/Supabase connection is used:
analyse_instrument is faked (its own correctness is covered by
test_fx_research.py) and the MT4 "api" is a small in-memory fake that
mimics EACommunicator_API's order lifecycle.
"""
import time

import pandas as pd
import pytest

from fx_research import execute
from fx_research.config import Settings
from fx_research.decision import Decision

SYM_INFO = {"digits": 5, "point": 0.00001, "stopLevel": 0, "tickSize": 0.00001, "tickValue": 1.0,
            "lotStep": 0.01, "minLotSize": 0.01, "maxLotSize": 100.0}


class FakeApi:
    """Mimics just enough of EACommunicator_API for FighterOrder/run_fighter."""

    def __init__(self, bid=1.30000, ask=1.30010):
        self.bid, self.ask = bid, ask
        self.orders = []
        self.positions = []
        self.next_ticket = 1000
        self.deleted = []
        self.rejected = False

    def Get_last_tick_info(self, symbol):
        return {"bid": self.bid, "ask": self.ask}

    def Open_order(self, symbol, otype, volume, openprice, slippage, magicnumber, stoploss, takeprofit, comment):
        if self.rejected:
            return None
        ticket = self.next_ticket
        self.next_ticket += 1
        self.orders.append({"ticket": ticket, "instrument": symbol, "comment": comment})
        return ticket

    def Get_all_orders(self):
        cols = ["ticket", "instrument", "comment"]
        return pd.DataFrame(self.orders, columns=cols) if self.orders else pd.DataFrame(columns=cols)

    def Get_all_open_positions(self):
        cols = ["ticket", "instrument", "comment"]
        return pd.DataFrame(self.positions, columns=cols) if self.positions else pd.DataFrame(columns=cols)

    def Delete_order_by_ticket(self, ticket):
        self.deleted.append(ticket)
        self.orders = [o for o in self.orders if o["ticket"] != ticket]

    def Change_settings_for_pending_order(self, ticket, price, stoploss, takeprofit):
        pass

    def fill_pending(self):
        assert len(self.orders) == 1, "expected exactly one pending order to fill"
        self.positions.append(self.orders.pop())


class FakeMT4:
    def __init__(self, api):
        self.api = api

    def close(self):
        pass


def fake_strat(recommended=True, direction="SHORT", mean_r_train=0.5, mean_r_val=0.3, ttl_bars=8):
    return {
        "recommended": recommended,
        "params": {"direction_mode": direction.lower(), "k_atr": 0.33, "sl_atr": 0.6, "tp_atr": 0.92,
                   "ttl_bars": ttl_bars, "reprice_every": 0},
        "train_metrics": {"mean_r": mean_r_train, "fills": 5, "decisions": 10},
        "validate_metrics": {"mean_r": mean_r_val, "fills": 2, "decisions": 4},
        "live_hint": {"direction": direction, "last_close": 1.3, "atr": 0.001,
                      "entry_distance": 0.00033, "sl_distance": 0.0006, "tp_distance": 0.00092,
                      "ttl_bars": ttl_bars, "reprice_every": 0},
        "fitness": 1.0,
    }


def fake_snap():
    return {"regime": "trend_up", "features": {"drift_z": 1.0}}


def bars_df(n=50):
    idx = pd.date_range(pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=n), periods=n, freq="h")
    return pd.DataFrame({"Open": 1.3, "High": 1.301, "Low": 1.299, "Close": 1.3, "Volume": 1.0}, index=idx)


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(execute, "STEP_POLL_SECONDS", 0)


@pytest.fixture(autouse=True)
def no_bar_loading(monkeypatch):
    class FakeLoader:
        def __init__(self, s, client):
            self.mt4 = None

        def load(self, symbol):
            return bars_df()

    monkeypatch.setattr(execute, "BarLoader", FakeLoader)
    monkeypatch.setattr(execute, "make_supabase_client", lambda s: None)
    monkeypatch.setattr(execute, "get_symbol_info", lambda api, symbol: dict(SYM_INFO))


def settings():
    s = Settings()
    s.supabase_url = s.supabase_key = ""  # has_supabase is a read-only property computed from these
    return s


def wait_for(cond, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_skip_sends_no_notification_and_places_no_order(monkeypatch):
    monkeypatch.setattr(execute, "analyse_instrument",
                         lambda *a, **k: (fake_snap(), [], fake_strat(recommended=False)))
    api = FakeApi()
    notified = []
    result = execute.scan_and_execute(FakeMT4(api), "USDSGD", settings(), notify=notified.append)

    assert result["action"] == "SKIP"
    assert notified == []
    assert api.orders == [] and api.positions == []


def test_enter_not_live_sends_dry_run_notification_and_places_no_order(monkeypatch):
    monkeypatch.setattr(execute, "analyse_instrument", lambda *a, **k: (fake_snap(), [], fake_strat()))
    api = FakeApi()
    notified = []
    result = execute.scan_and_execute(FakeMT4(api), "USDSGD", settings(), live=False, notify=notified.append)

    assert result["action"] == "ENTER"
    assert result.get("thread") is None
    assert api.orders == []
    assert len(notified) == 2   # signal found, then "not armed / dry-run"
    assert "signal found" in notified[0]
    assert "dry-run" in notified[1] or "not armed" in notified[1]


def test_enter_live_places_order_and_notifies_on_fill(monkeypatch):
    monkeypatch.setattr(execute, "analyse_instrument", lambda *a, **k: (fake_snap(), [], fake_strat()))
    api = FakeApi()
    notified = []
    result = execute.scan_and_execute(FakeMT4(api), "USDSGD", settings(), live=True, notify=notified.append)

    assert result["action"] == "ENTER"
    thread = result["thread"]
    assert wait_for(lambda: len(api.orders) == 1), "order was never placed"
    api.fill_pending()
    thread.join(timeout=5)
    assert not thread.is_alive(), "background thread never resolved after fill"

    assert len(notified) == 2
    assert "signal found" in notified[0]
    assert "FILLED" in notified[1] and f"ticket={api.positions[0]['ticket']}" in notified[1]


def test_enter_live_rejected_order_notifies_rejection(monkeypatch):
    monkeypatch.setattr(execute, "analyse_instrument", lambda *a, **k: (fake_snap(), [], fake_strat()))
    api = FakeApi()
    api.rejected = True
    notified = []
    result = execute.scan_and_execute(FakeMT4(api), "USDSGD", settings(), live=True, notify=notified.append)

    thread = result["thread"]
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(notified) == 2
    assert "not placed" in notified[1] or "rejected" in notified[1]


def test_run_fighter_reports_cancelled_on_ttl_expiry(monkeypatch):
    """Direct check of the run_fighter return-value fix (ticket, final_state)
    independent of scan_and_execute -- TTL of 0 bars means the very first
    step() call cancels immediately."""
    api = FakeApi()
    params = {"direction_mode": "short", "k_atr": 0.33, "sl_atr": 0.6, "tp_atr": 0.92,
              "ttl_bars": 0, "reprice_every": 0, "atr": 0.001}
    ticket, state = execute.run_fighter(api, "USDSGD", "SHORT", params, dict(SYM_INFO), 100000.0, 0.03)
    assert state == "cancelled"
    assert ticket in api.deleted
