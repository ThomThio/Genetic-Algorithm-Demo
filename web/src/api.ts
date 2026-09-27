import type { Bar, Control, DecisionRow, LiveState, TradeCommand, TradeRow } from "./types";

const URL = import.meta.env.VITE_SUPABASE_URL as string;
const KEY = import.meta.env.VITE_SUPABASE_KEY as string;
const RESULTS = "fx_research";
const SOURCE = "FTMO_MT4_demo";

interface Init {
  method?: string;
  body?: string;
  write?: boolean;
}

async function rest<T>(schema: string, table: string, query = "", init: Init = {}): Promise<T> {
  const res = await fetch(`${URL}/rest/v1/${table}${query ? "?" + query : ""}`, {
    method: init.method,
    body: init.body,
    headers: {
      apikey: KEY,
      Authorization: `Bearer ${KEY}`,
      "Content-Type": "application/json",
      [init.write ? "Content-Profile" : "Accept-Profile"]: schema,
      ...(init.write ? { Prefer: "resolution=merge-duplicates,return=minimal" } : {}),
    },
  });
  if (!res.ok) throw new Error(`${table}: ${res.status} ${await res.text()}`);
  const text = await res.text();
  return (text ? JSON.parse(text) : null) as T;
}

const enc = encodeURIComponent;

export function getLiveStates(): Promise<LiveState[]> {
  return rest<LiveState[]>(RESULTS, "live_state", "select=*&order=instrument");
}

export async function getBars(instrument: string, n = 400): Promise<Bar[]> {
  const q =
    `select=Datetime,Open,High,Low,Close&Ccy=eq.${enc(instrument)}&Timeframe=eq.H1&Source=eq.${SOURCE}` +
    `&order=Datetime.desc&limit=${n}`;
  const rows = await rest<{ Datetime: string; Open: number; High: number; Low: number; Close: number }[]>(
    "public",
    "fx_prices",
    q,
  );
  return rows
    .map((r) => ({
      time: Math.floor(Date.parse(r.Datetime) / 1000),
      open: r.Open,
      high: r.High,
      low: r.Low,
      close: r.Close,
    }))
    .reverse();
}

export async function getControl(instrument: string): Promise<Control | null> {
  const rows = await rest<Control[]>(RESULTS, "trading_control", `select=*&instrument=eq.${enc(instrument)}`);
  return rows[0] ?? null;
}

export async function setControl(instrument: string, enabled: boolean, minutes: number): Promise<void> {
  const armed_until = enabled ? new Date(Date.now() + minutes * 60_000).toISOString() : null;
  await rest(RESULTS, "trading_control", "on_conflict=instrument", {
    method: "POST",
    write: true,
    body: JSON.stringify({ instrument, enabled, armed_until, updated_at: new Date().toISOString(), updated_by: "web" }),
  });
}

export function getTrades(instrument: string): Promise<TradeRow[]> {
  return rest<TradeRow[]>(
    RESULTS,
    "trades",
    "select=id,direction,status,simulated,ticket,placed_at,limit_price,entry,exit,sl,tp,lots,r_multiple,exit_reason,pnl_usd,open_pnl_usd,open_r,mark_price,last_price,open_costs_usd" +
      `&instrument=eq.${enc(instrument)}&simulated=eq.false&order=placed_at.desc&limit=200`,
  );
}

export function getDecisions(instrument: string): Promise<DecisionRow[]> {
  return rest<DecisionRow[]>(
    RESULTS,
    "decision_log",
    `select=id,decided_at,action,reason,regime&instrument=eq.${enc(instrument)}&order=decided_at.desc&limit=8`,
  );
}

export async function createCommand(instrument: string, action: TradeCommand["action"]): Promise<void> {
  await rest(RESULTS, "trade_commands", "", {
    method: "POST",
    write: true,
    body: JSON.stringify({ instrument, action, requested_by: "web" }),
  });
}

export function getCommands(instrument: string): Promise<TradeCommand[]> {
  return rest<TradeCommand[]>(
    RESULTS,
    "trade_commands",
    `select=id,instrument,action,status,created_at,result&instrument=eq.${enc(instrument)}&order=created_at.desc&limit=5`,
  );
}
