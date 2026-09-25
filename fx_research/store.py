"""Where scan results go: the fx_research schema in Supabase, or JSON files for dry runs."""
import json
import os
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat()


class DryRunStore:
    def __init__(self, out_dir):
        self.out_dir = out_dir
        os.makedirs(out_dir, exist_ok=True)

    def start_run(self, run):
        self._write(f"run_{run['id']}.json", run)

    def finish_run(self, run_id, status, error=None):
        path = os.path.join(self.out_dir, f"run_{run_id}.json")
        run = json.load(open(path)) if os.path.exists(path) else {"id": run_id}
        run.update(status=status, error=error, finished_at=_now())
        self._write(f"run_{run_id}.json", run)

    def save_instrument(self, snapshot, analogs, strategy):
        name = f"{snapshot['instrument']}_{snapshot['run_id']}.json"
        self._write(name, {"regime_snapshot": snapshot, "analog_matches": analogs, "strategy_result": strategy})

    def _write(self, name, obj):
        with open(os.path.join(self.out_dir, name), "w") as f:
            json.dump(obj, f, indent=2, default=str)


class SupabaseStore:
    """Writes to the results schema (default fx_research); see sql/fx_research_schema.sql."""

    def __init__(self, client, schema):
        self.db = client.schema(schema)

    def start_run(self, run):
        self.db.table("scan_runs").insert(run).execute()

    def finish_run(self, run_id, status, error=None):
        self.db.table("scan_runs").update(
            {"status": status, "error": error, "finished_at": _now()}).eq("id", run_id).execute()

    def save_instrument(self, snapshot, analogs, strategy):
        self.db.table("regime_snapshots").insert(snapshot).execute()
        if analogs:
            self.db.table("analog_matches").insert(analogs).execute()
        self.db.table("strategy_results").insert(strategy).execute()


def make_supabase_client(settings):
    from supabase import create_client
    return create_client(settings.supabase_url, settings.supabase_key)
