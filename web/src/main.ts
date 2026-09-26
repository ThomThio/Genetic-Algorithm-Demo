import {
  ColorType,
  CrosshairMode,
  LineStyle,
  createChart,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { getBars, getControl, getDecisions, getLiveStates, getTrades, setControl } from "./api";
import type { Analog, Bar, Control, DecisionRow, LiveState, Plan, TradeRow } from "./types";

const HOUR = 3600;
const ARM_MINUTES = 60;
const TICK_MS = 5_000;
const SLOW_MS = 15_000;
const BARS_MS = 60_000;
const STALE_RUNNER_S = 30;

const $ = <T extends HTMLElement>(id: string): T => document.getElementById(id) as T;
const esc = (s: unknown): string =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
const fmt = (n: number | null | undefined, digits = 5): string => (n == null || Number.isNaN(n) ? "-" : n.toFixed(digits));
const cls = (n: number | null | undefined): string => (n == null ? "" : n >= 0 ? "pos" : "neg");

// ---------------------------------------------------------------- state
let instrument = localStorage.getItem("fx.instrument") ?? "";
let states = new Map<string, LiveState>();
let bars: Bar[] = [];
let forming: Bar | null = null;
let control: Control | null = null;
let trades: TradeRow[] = [];
let decisions: DecisionRow[] = [];
let analogSeries: ISeriesApi<"Line">[] = [];
let priceLines: IPriceLine[] = [];
let linesKey = "";
let drawnKey = "";

// ---------------------------------------------------------------- chart
const chart: IChartApi = createChart($("chart"), {
  layout: { background: { type: ColorType.Solid, color: "#161b22" }, textColor: "#8794a3" },
  grid: { vertLines: { color: "#1d242e" }, horzLines: { color: "#1d242e" } },
  crosshair: { mode: CrosshairMode.Normal },
  rightPriceScale: { borderColor: "#262d38" },
  timeScale: { borderColor: "#262d38", timeVisible: true, secondsVisible: false, rightOffset: 80 },
  autoSize: true,
});
const candles: ISeriesApi<"Candlestick"> = chart.addCandlestickSeries({
  upColor: "#26a69a",
  downColor: "#ef5350",
  borderVisible: false,
  wickUpColor: "#26a69a",
  wickDownColor: "#ef5350",
});

// lightweight-charts only parses hex/rgb/rgba colours (not hsl), so convert.
function hslToRgba(h: number, s: number, l: number, a: number): string {
  const k = (n: number): number => (n + h / 30) % 12;
  const f = (n: number): number => l - s * Math.min(l, 1 - l) * Math.max(-1, Math.min(k(n) - 3, 9 - k(n), 1));
  return `rgba(${Math.round(f(0) * 255)}, ${Math.round(f(8) * 255)}, ${Math.round(f(4) * 255)}, ${a})`;
}
const analogColor = (rank: number): string => hslToRgba((190 + rank * 32) % 360, 0.75, 0.62, 0.75);

function barIndexAt(time: number): number {
  let idx = bars.length - 1;
  for (let i = bars.length - 1; i >= 0; i--) {
    if (bars[i].time <= time) {
      idx = i;
      break;
    }
  }
  return idx;
}

/** Analog path point j -> a chart time: past points sit on real bars, forward points continue hourly. */
function analogPoints(a: Analog, asofIdx: number): { time: UTCTimestamp; value: number }[] {
  const out: { time: UTCTimestamp; value: number }[] = [];
  const asofTime = bars[asofIdx].time;
  a.path.forEach((value, j) => {
    let time: number;
    if (j <= a.end_idx) {
      const idx = asofIdx - (a.end_idx - j);
      if (idx < 0) return;
      time = bars[idx].time;
    } else {
      time = asofTime + (j - a.end_idx) * HOUR;
    }
    out.push({ time: time as UTCTimestamp, value });
  });
  return out;
}

function drawChart(): void {
  const st = states.get(instrument);
  const analysis = st?.analysis ?? null;
  const showAnalogs = $<HTMLInputElement>("show-analogs").checked;
  const key = `${instrument}|${bars.length}|${bars[bars.length - 1]?.time}|${analysis?.asof}|${showAnalogs}|${trades.length}`;
  if (key === drawnKey) return;
  drawnKey = key;

  const last = bars[bars.length - 1]?.close ?? 1;
  const precision = last < 20 ? 5 : last < 1000 ? 3 : 2; // forex majors, JPY/commodities, indices
  candles.applyOptions({ priceFormat: { type: "price", precision, minMove: 1 / 10 ** precision } });
  candles.setData(bars.map((b) => ({ ...b, time: b.time as UTCTimestamp })));
  analogSeries.forEach((s) => chart.removeSeries(s));
  analogSeries = [];
  const markers: SeriesMarker<Time>[] = [];

  if (analysis && bars.length) {
    const asofIdx = barIndexAt(Date.parse(analysis.asof) / 1000);
    if (showAnalogs) {
      for (const a of analysis.analogs) {
        const s = chart.addLineSeries({
          color: analogColor(a.rank),
          lineWidth: 1,
          lastValueVisible: false,
          priceLineVisible: false,
          crosshairMarkerVisible: false,
        });
        s.setData(analogPoints(a, asofIdx));
        analogSeries.push(s);
      }
    }
    const winStart = Date.parse(analysis.window.start) / 1000;
    const wIdx = bars.findIndex((b) => b.time >= winStart);
    if (wIdx >= 0)
      markers.push({ time: bars[wIdx].time as UTCTimestamp, position: "aboveBar", color: "#4c9be8", shape: "arrowDown", text: `window start (${analysis.window.days}d)` });
    markers.push({ time: bars[asofIdx].time as UTCTimestamp, position: "belowBar", color: "#f0b429", shape: "arrowUp", text: `decision: ${analysis.decision.action}` });
  }
  for (const t of trades) {
    if (!t.placed_at) continue;
    const idx = barIndexAt(Date.parse(t.placed_at) / 1000);
    markers.push({ time: bars[idx].time as UTCTimestamp, position: "aboveBar", color: t.status === "closed" ? "#8794a3" : "#ef5350", shape: "circle", text: `#${t.ticket ?? "?"} ${t.status}` });
  }
  markers.sort((a, b) => (a.time as number) - (b.time as number));
  candles.setMarkers(markers);
  renderLegend(analysis?.analogs ?? []);
}

function setPriceLines(plan: Plan | undefined, order?: LiveState["runner"]): void {
  const defs: { price: number; color: string; title: string; style: LineStyle }[] = [];
  if (plan) {
    defs.push({ price: plan.limit, color: "#f0b429", title: `${plan.direction} limit`, style: LineStyle.Dashed });
    defs.push({ price: plan.sl, color: "#ef5350", title: "stop", style: LineStyle.Solid });
    defs.push({ price: plan.tp, color: "#26a69a", title: "target", style: LineStyle.Solid });
  }
  if (order?.order && order.order.state === "pending")
    defs.push({ price: order.order.limit, color: "#4c9be8", title: `working #${order.order.ticket}`, style: LineStyle.Dotted });
  const key = defs.map((d) => `${d.title}:${d.price}`).join("|");
  if (key === linesKey) return;
  linesKey = key;
  priceLines.forEach((l) => candles.removePriceLine(l));
  priceLines = defs.map((d) =>
    candles.createPriceLine({ price: d.price, color: d.color, lineWidth: 1, lineStyle: d.style, axisLabelVisible: true, title: d.title }),
  );
}

function updateForming(): void {
  const st = states.get(instrument);
  const tick = st?.tick;
  const open = st?.analysis?.decision.gates["market_open"]?.pass ?? true;
  if (!tick || !bars.length || !open) {
    if (forming) {
      forming = null;
      drawnKey = ""; // redraw to drop the stale forming candle
    }
    return;
  }
  const mid = (tick.bid + tick.ask) / 2;
  const hour = Math.floor(Date.parse(tick.ts) / 1000 / HOUR) * HOUR;
  const last = bars[bars.length - 1];
  if (hour <= last.time) return;
  forming =
    forming && forming.time === hour
      ? { ...forming, high: Math.max(forming.high, mid), low: Math.min(forming.low, mid), close: mid }
      : { time: hour, open: last.close, high: Math.max(last.close, mid), low: Math.min(last.close, mid), close: mid };
  candles.update({ ...forming, time: forming.time as UTCTimestamp });
}

// ---------------------------------------------------------------- panels
function renderLegend(analogs: Analog[]): void {
  $("legend").innerHTML =
    `<span><i style="border-color:#f0b429"></i>entry</span><span><i style="border-color:#ef5350"></i>stop</span>` +
    `<span><i style="border-color:#26a69a"></i>target</span>` +
    analogs.map((a) => `<span><i style="border-color:${analogColor(a.rank)}"></i>#${a.rank}</span>`).join("");
}

function median(xs: number[]): number | null {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

function renderStatus(): void {
  const st = states.get(instrument);
  if (!st) {
    $("status").innerHTML = `<span class="pill bad">no live data - is the runner running?</span>`;
    return;
  }
  const ageS = (Date.now() - Date.parse(st.updated_at)) / 1000;
  const r = st.runner ?? {};
  const pills: string[] = [];
  pills.push(ageS < STALE_RUNNER_S ? `<span class="pill ok">runner online</span>` : `<span class="pill bad">runner offline (${Math.round(ageS)}s)</span>`);
  pills.push(`<span class="pill ${r.allow_live ? "warn" : ""}">${esc(r.mode ?? "?")}${r.allow_live ? " - orders allowed" : " - no orders"}</span>`);
  if (r.error) pills.push(`<span class="pill bad">${esc(r.error)}</span>`);
  if (st.tick) pills.push(`<span class="pill">bid ${fmt(st.tick.bid)} / ask ${fmt(st.tick.ask)}</span>`);
  if (st.analysis) pills.push(`<span class="pill">${esc(st.analysis.regime)}</span>`);
  $("status").innerHTML = pills.join("");
}

function isArmed(c: Control | null): boolean {
  return !!c && c.enabled && !!c.armed_until && Date.parse(c.armed_until) > Date.now();
}

function renderTrading(): void {
  const st = states.get(instrument);
  const armed = isArmed(control);
  const paperOnly = st && !st.runner?.allow_live;
  const left = armed && control?.armed_until ? Math.max(0, Math.round((Date.parse(control.armed_until) - Date.now()) / 60000)) : 0;
  $("trading").innerHTML =
    `<h3>Live trading - ${esc(instrument)}</h3>` +
    `<button class="btn switch ${armed ? "on" : ""}" id="arm">${armed ? `LIVE ORDERS ON (${left} min left) - click to stop` : "Live orders OFF - click to arm"}</button>` +
    `<div class="note">${
      paperOnly
        ? "Runner is in paper mode (started without --allow-live): arming has no effect."
        : `Arming lasts ${ARM_MINUTES} min and needs the runner started with --allow-live. Places real orders on the connected MT4 account.`
    }</div>`;
  $("arm").onclick = async () => {
    if (!armed) {
      const ok = confirm(
        `Arm live order placement for ${instrument} for ${ARM_MINUTES} minutes?\n\nThe runner will place real fighter limit orders on the connected MT4 account whenever the decision engine says ENTER.`,
      );
      if (!ok) return;
    }
    try {
      await setControl(instrument, !armed, ARM_MINUTES);
      control = await getControl(instrument);
    } catch (e) {
      alert(`Could not update trading control: ${e instanceof Error ? e.message : e}`);
    }
    renderTrading();
  };
}

function renderDecision(): void {
  const a = states.get(instrument)?.analysis;
  if (!a) {
    $("decision").innerHTML = `<h3>Decision</h3><div class="dim">waiting for the first analysis...</div>`;
    return;
  }
  const gates = Object.entries(a.decision.gates)
    .map(([k, g]) => `<div class="gate"><span class="dim">${esc(k)}</span><span class="${g.pass ? "pos" : "neg"}">${g.pass ? "pass" : "FAIL"}</span></div>`)
    .join("");
  const s = a.signal;
  $("decision").innerHTML =
    `<h3>Decision engine</h3>` +
    `<div class="big ${a.decision.action === "ENTER" ? "pos" : "dim"}">${esc(a.decision.action)} ${esc(a.direction)}</div>` +
    `<div class="dim">${esc(a.decision.reason)}</div><div style="margin:6px 0">${gates}</div>` +
    `<div class="kv"><span>fit train</span><span class="${cls(s.train.mean_r)}">${fmt(s.train.mean_r, 2)}R (${s.train.fills} fills)</span>` +
    `<span>fit validate</span><span class="${cls(s.validate.mean_r)}">${fmt(s.validate.mean_r, 2)}R (${s.validate.fills} fills)</span>` +
    `<span>bar age</span><span>${fmt(a.bar_age_h, 1)} h</span><span>analog pool</span><span>${a.history_windows} windows</span></div>`;
}

function renderPlan(): void {
  const st = states.get(instrument);
  const p = st?.tick?.plan;
  const a = st?.analysis;
  if (!p || !a) {
    $("plan").innerHTML = `<h3>Entry / exit plan</h3><div class="dim">no plan yet</div>`;
    return;
  }
  const costR = Math.abs(p.sl - p.limit) > 0 ? a.spread / Math.abs(p.sl - p.limit) : null;
  $("plan").innerHTML =
    `<h3>Entry / exit plan (fighter limit)</h3><div class="kv">` +
    `<span>direction</span><span class="${p.direction === "LONG" ? "long" : "short"}">${p.direction}</span>` +
    `<span>limit entry</span><span>${fmt(p.limit)}</span><span>stop</span><span class="neg">${fmt(p.sl)}</span>` +
    `<span>target</span><span class="pos">${fmt(p.tp)}</span><span>reward:risk</span><span>${fmt(p.rr, 2)}</span>` +
    `<span>lots</span><span>${fmt(p.lots, 2)}</span><span>risk</span><span>$${p.risk_usd.toFixed(0)} of $${p.balance.toFixed(0)}</span>` +
    `<span>cancel after</span><span>${p.ttl_bars} bar(s)</span><span>re-price every</span><span>${p.reprice_every || "never"}</span>` +
    `<span>spread cost</span><span>${costR == null ? "-" : costR.toFixed(2)}R</span></div>`;
}

function renderAnalogs(): void {
  const a = states.get(instrument)?.analysis;
  if (!a) {
    $("analogs").innerHTML = "";
    return;
  }
  const med = median(a.analogs.map((x) => x.fwd_move_atr).filter((v): v is number => v != null));
  $("analogs").innerHTML =
    `<h3>Most similar past windows</h3><table><tr><th>#</th><th>ended</th><th class="num">dist</th><th class="num">next ${a.forward_days}d (ATR)</th></tr>` +
    a.analogs
      .map(
        (x) =>
          `<tr><td style="color:${analogColor(x.rank)}">${x.rank}</td><td>${esc(x.window_end.slice(0, 10))}${x.same_regime ? "" : ' <span class="dim">*</span>'}</td>` +
          `<td class="num">${x.distance.toFixed(2)}</td><td class="num ${cls(x.fwd_move_atr)}">${fmt(x.fwd_move_atr, 2)}</td></tr>`,
      )
      .join("") +
    `</table><div class="note">median forward move ${fmt(med, 2)} ATR. * different regime</div>`;
}

function renderTrades(): void {
  $("trades").innerHTML =
    `<h3>Live trades</h3>` +
    (trades.length
      ? `<table><tr><th>ticket</th><th>status</th><th class="num">entry</th><th class="num">R</th></tr>` +
        trades
          .map(
            (t) =>
              `<tr><td>${esc(t.ticket)}</td><td>${esc(t.status)}${t.exit_reason ? " (" + esc(t.exit_reason) + ")" : ""}</td>` +
              `<td class="num">${fmt(t.entry ?? t.limit_price)}</td><td class="num ${cls(t.r_multiple)}">${fmt(t.r_multiple, 2)}</td></tr>`,
          )
          .join("") +
        `</table>`
      : `<div class="dim">none yet</div>`);
}

function renderDecisions(): void {
  $("decisions").innerHTML =
    `<h3>Recent decisions</h3>` +
    (decisions.length
      ? `<table>` +
        decisions
          .map(
            (d) =>
              `<tr><td class="dim">${esc(d.decided_at.slice(5, 16).replace("T", " "))}</td><td class="${d.action === "ENTER" ? "pos" : "dim"}">${esc(d.action)}</td><td class="dim">${esc(d.reason)}</td></tr>`,
          )
          .join("") +
        `</table>`
      : `<div class="dim">none yet</div>`);
}

function renderInstruments(): void {
  const names = [...states.keys()];
  $("instruments").innerHTML = names
    .map((n) => `<button class="${n === instrument ? "active" : ""}" data-ins="${esc(n)}">${esc(n)}</button>`)
    .join("");
  $("instruments").querySelectorAll("button").forEach((b) => {
    b.addEventListener("click", () => void selectInstrument((b as HTMLButtonElement).dataset.ins!));
  });
}

function renderAll(): void {
  renderStatus();
  renderTrading();
  renderDecision();
  renderPlan();
  renderAnalogs();
  renderTrades();
  renderDecisions();
}

// ---------------------------------------------------------------- data flow
async function loadBars(): Promise<void> {
  if (!instrument) return;
  const fresh = await getBars(instrument);
  const changed = fresh.length !== bars.length || fresh[fresh.length - 1]?.time !== bars[bars.length - 1]?.time;
  if (changed) {
    bars = fresh;
    forming = null;
    drawnKey = "";
  }
  drawChart();
}

async function loadSlow(): Promise<void> {
  if (!instrument) return;
  [control, trades, decisions] = await Promise.all([getControl(instrument), getTrades(instrument), getDecisions(instrument)]);
  drawChart();
  renderAll();
}

async function pollTick(): Promise<void> {
  const list = await getLiveStates();
  states = new Map(list.map((s) => [s.instrument, s]));
  if (!instrument || !states.has(instrument)) instrument = list[0]?.instrument ?? instrument;
  renderInstruments();
  const st = states.get(instrument);
  setPriceLines(st?.tick?.plan, st?.runner);
  updateForming();
  drawChart();
  renderStatus();
  renderDecision();
  renderPlan();
  renderAnalogs();
}

async function selectInstrument(name: string): Promise<void> {
  instrument = name;
  localStorage.setItem("fx.instrument", name);
  bars = [];
  forming = null;
  control = null;
  trades = [];
  decisions = [];
  drawnKey = "";
  linesKey = "";
  renderInstruments();
  await Promise.all([loadBars(), loadSlow(), pollTick()]);
  chart.timeScale().fitContent();
  renderAll();
}

function guard(fn: () => Promise<void>): () => void {
  return () => {
    fn().catch((e) => {
      $("status").innerHTML = `<span class="pill bad">${esc(e instanceof Error ? e.message : e)}</span>`;
    });
  };
}

$("show-analogs").addEventListener("change", () => {
  drawnKey = "";
  drawChart();
});

void (async () => {
  try {
    await pollTick();
    await selectInstrument(instrument);
  } catch (e) {
    $("status").innerHTML = `<span class="pill bad">${esc(e instanceof Error ? e.message : e)}</span>`;
  }
  setInterval(guard(pollTick), TICK_MS);
  setInterval(guard(loadSlow), SLOW_MS);
  setInterval(guard(loadBars), BARS_MS);
})();
