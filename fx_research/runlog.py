"""Writes runs, decisions, trades and metric snapshots to the fx_research decision-log tables
(sql/fx_decisions_schema.sql). Same interface for backtest / walkforward / paper / live."""
import json
import subprocess
import uuid
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat()


def _clean(o):
    return json.loads(json.dumps(o, default=str))


def git_sha():
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


class RunLogger:
    @property
    def db(self):
        # re-select the schema each time: see store.SupabaseStore.db
        return self.client.schema(self.schema)

    def __init__(self, client, schema, mode, instrument, params, direction=None, timeframe="H1",
                 data_source=None, window_days=None, **run_fields):
        self.client, self.schema = client, schema
        self.run_id = str(uuid.uuid4())
        self.seq = 0
        self.db.table("strategy_runs").insert(_clean({
            "id": self.run_id, "mode": mode, "instrument": instrument, "direction": direction,
            "timeframe": timeframe, "data_source": data_source, "window_days": window_days,
            "params": params, "code_version": git_sha(), **run_fields})).execute()

    def decision(self, decided_at, instrument, decision, regime=None, inputs=None, order_spec=None):
        self.seq += 1
        row = {"run_id": self.run_id, "seq": self.seq, "decided_at": str(decided_at), "instrument": instrument,
               "regime": regime, "inputs": inputs, "signal": decision.signal, "gates": decision.gates,
               "action": decision.action, "reason": decision.reason, "order_spec": order_spec}
        return self.db.table("decision_log").insert(_clean(row)).execute().data[0]["id"]

    def trade(self, **fields):
        row = {"run_id": self.run_id, **fields}
        return self.db.table("trades").insert(_clean(row)).execute().data[0]["id"]

    def metrics(self, as_of, final=False, **fields):
        self.db.table("run_metrics").upsert(
            _clean({"run_id": self.run_id, "as_of": str(as_of), "final": final, **fields}),
            on_conflict="run_id,as_of").execute()

    def finish(self, summary, status="finished", edges=None):
        """Closes the run. ALWAYS computes the full BuildAlpha-style metric set (metrics.compute_edge) from
        this run's closed trades, stores it as the final run_metrics row and in summary["edge"], so every
        backtest / walk-forward / paper / live run is comparable. `edges` adds named extra sets
        (e.g. in_sample / out_of_sample)."""
        edge = refresh_metrics(self.client, self.schema, self.run_id, summary, edges)
        self.db.table("strategy_runs").update(
            _clean({"status": status, "finished_at": _now()})).eq("id", self.run_id).execute()
        return edge


def refresh_metrics(client, schema, run_id, summary=None, edges=None):
    """Recompute the metric set from a run's closed trades and store it (final run_metrics row +
    strategy_runs.summary.edge). Safe to call repeatedly: the live tracker uses it as trades close."""
    from .metrics import compute_edge
    db = client.schema(schema)
    rows = (db.table("trades").select("r_multiple,placed_at").eq("run_id", run_id)
            .eq("status", "closed").order("placed_at").execute().data)
    edge = compute_edge([r["r_multiple"] for r in rows], [r["placed_at"] for r in rows])
    as_of = next((r["placed_at"] for r in reversed(rows) if r["placed_at"]), _now())
    db = client.schema(schema)
    db.table("run_metrics").upsert(
        _clean({"run_id": run_id, "as_of": str(as_of), "final": True, "fills": edge["n_trades"],
                "closed": edge["n_trades"], "mean_r": edge["expectancy_R"], "total_r": edge["net_profit_R"],
                "win_rate": edge["win_rate"], "max_dd_r": edge["max_drawdown_R"],
                "extra": {"edge": edge, **(edges or {})}}), on_conflict="run_id,as_of").execute()
    if summary is None:
        summary = client.schema(schema).table("strategy_runs").select("summary").eq("id", run_id).execute().data[0]["summary"] or {}
    client.schema(schema).table("strategy_runs").update(
        _clean({"summary": {**summary, "edge": edge, **(edges or {})}})).eq("id", run_id).execute()
    return edge
