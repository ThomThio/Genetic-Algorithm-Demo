"""Settings for the FX research scanner.

Everything comes from environment variables so no credentials live in the repo.
"""
import os
from dataclasses import dataclass, field


def _load_dotenv(path=".env"):
    """Minimal .env reader (KEY=VALUE lines); real environment variables win."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _env_list(name, default):
    raw = os.environ.get(name)
    if not raw:
        return list(default)
    return [s.strip() for s in raw.split(",") if s.strip()]


# Same list MT4-TradeSignals trades (global_vars.fixed_instruments), FX only.
DEFAULT_INSTRUMENTS = ["GBPUSD", "AUDUSD", "USDJPY", "AUDJPY", "EURUSD"]

# Regimes as named in TradeCoreV2 global_vars.regimes.
REGIMES = ["Uptrend", "Sharp_Uptrend", "Downtrend", "Sharp_Downtrend", "Sideways", "Random"]


@dataclass
class Settings:
    instruments: list = field(default_factory=lambda: _env_list("FXR_INSTRUMENTS", DEFAULT_INSTRUMENTS))
    timeframe: str = os.environ.get("FXR_TIMEFRAME", "H1")

    # Current window: last N trading days (weekends are never counted).
    window_days: int = int(os.environ.get("FXR_WINDOW_DAYS", "7"))
    # How far back to search for similar windows.
    history_years: float = float(os.environ.get("FXR_HISTORY_YEARS", "3"))
    # Trading days after each matched window used to score a strategy.
    forward_days: int = int(os.environ.get("FXR_FORWARD_DAYS", "3"))
    # Number of closest historical windows kept per scan.
    top_k: int = int(os.environ.get("FXR_TOP_K", "25"))

    # GA
    ga_population: int = int(os.environ.get("FXR_GA_POP", "40"))
    ga_generations: int = int(os.environ.get("FXR_GA_GENS", "30"))
    ga_seed: int = int(os.environ["FXR_GA_SEED"]) if os.environ.get("FXR_GA_SEED") else None

    # Where bars come from: "supabase", "mt4" or "csv".
    bar_source: str = os.environ.get("FXR_BAR_SOURCE", "supabase")
    csv_dir: str = os.environ.get("FXR_CSV_DIR", "currency_data")
    # Path to a checkout of MT4-TradeSignals (for EACommunicator_API) when bar_source == "mt4".
    mt4_tradesignals_path: str = os.environ.get("MT4_TRADESIGNALS_PATH", "")
    mt4_port: int = int(os.environ.get("MT4_ZMQ_PORT", "5555"))

    # Supabase. Results go to their own schema; prices are read from public.fx_prices.
    supabase_url: str = os.environ.get("SUPABASE_URL", "")
    supabase_key: str = os.environ.get("SUPABASE_SERVICE_KEY", "") or os.environ.get("SUPABASE_KEY", "")
    results_schema: str = os.environ.get("FXR_SCHEMA", "fx_research")
    prices_schema: str = os.environ.get("FXR_PRICES_SCHEMA", "public")
    prices_table: str = os.environ.get("FXR_PRICES_TABLE", "fx_prices")

    # Scheduler (same timezone as MT4-TradeSignals).
    timezone: str = os.environ.get("FXR_TIMEZONE", "Asia/Singapore")
    # Cron minute(s) for the hourly scan; runs a few minutes after the H1 bar closes.
    cron_minute: str = os.environ.get("FXR_CRON_MINUTE", "5")
    cron_hour: str = os.environ.get("FXR_CRON_HOUR", "*")

    # Where results are written when Supabase isn't configured.
    dry_run_dir: str = os.environ.get("FXR_DRY_RUN_DIR", "fx_research_output")

    @property
    def has_supabase(self):
        return bool(self.supabase_url and self.supabase_key)
