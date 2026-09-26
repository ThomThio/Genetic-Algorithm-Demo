export interface Bar {
  time: number; // unix seconds (bar open)
  open: number;
  high: number;
  low: number;
  close: number;
}

export interface Plan {
  direction: "LONG" | "SHORT";
  limit: number;
  sl: number;
  tp: number;
  lots: number;
  risk_usd: number;
  balance: number;
  ttl_bars: number;
  reprice_every: number;
  rr: number | null;
}

export interface Tick {
  bid: number;
  ask: number;
  ts: string;
  plan?: Plan;
}

export interface Analog {
  rank: number;
  distance: number;
  shape_corr: number;
  regime: string;
  same_regime: boolean;
  split: string;
  window_start: string;
  window_end: string;
  fwd_move_atr: number | null;
  end_idx: number; // index in `path` of the window end (aligned to the current last bar)
  path: number[];
}

export interface Gate {
  pass: boolean;
  [k: string]: unknown;
}

export interface Metrics {
  fills: number;
  decisions: number;
  mean_r: number;
  win_rate: number;
}

export interface Analysis {
  asof: string;
  bar_age_h: number;
  regime: string;
  direction: string;
  features: Record<string, number>;
  window: { start: string; end: string; days: number; n_bars: number };
  history_windows: number;
  forward_days: number;
  analogs: Analog[];
  signal: { recommended: boolean; train: Metrics; validate: Metrics; atr: number; last_close: number };
  decision: { action: "ENTER" | "SKIP"; reason: string; gates: Record<string, Gate> };
  spread: number;
}

export interface RunnerInfo {
  mode?: string;
  allow_live?: boolean;
  armed?: boolean;
  error?: string;
  started_at?: string;
  order?: { ticket: number; state: string; limit: number; sl: number; tp: number; lots: number };
}

export interface LiveState {
  instrument: string;
  updated_at: string;
  tick: Tick | null;
  analysis: Analysis | null;
  runner: RunnerInfo | null;
}

export interface Control {
  instrument: string;
  enabled: boolean;
  armed_until: string | null;
  risk_pct: number;
}

export interface TradeRow {
  id: number;
  direction: string;
  status: string;
  simulated: boolean;
  ticket: number | null;
  placed_at: string | null;
  limit_price: number | null;
  entry: number | null;
  exit: number | null;
  sl: number | null;
  tp: number | null;
  lots: number | null;
  r_multiple: number | null;
  exit_reason: string | null;
}

export interface DecisionRow {
  id: number;
  decided_at: string;
  action: string;
  reason: string;
  regime: string | null;
}
