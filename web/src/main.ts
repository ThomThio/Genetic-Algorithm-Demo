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
import { createCommand, getBars, getCommands, getControl, getDecisions, getLiveStates, getTrades, setControl } from "./api";
import eventsData from "./events.json";
import type { Analog, Bar, Control, DecisionRow, LiveState, MarketEvent, Plan, TradeCommand, TradeRow } from "./types";

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
const usd = (n: number | null | undefined): string =>
  n == null ? "-" : `${n < 0 ? "-" : n > 0 ? "+" : ""}$${Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
const cls = (n: number | null | undefined): string => (n == null ? "" : n >= 0 ? "pos" : "neg");

// ---------------------------------------------------------------- state
let instrument = localStorage.getItem("fx.instrument") ?? "";
let states = new Map<string, LiveState>();
let bars: Bar[] = [];
let forming: Bar | null = null;
let control: Control | null = null;
let trades: TradeRow[] = [];
let decisions: DecisionRow[] = [];
let commands: TradeCommand[] = [];
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
// The analog paths sit BEHIND the price: their five line series are created before the candles (later series
// paint on top) and drawn faded and thin, so today's candles stay the focus. One colour, five line patterns
// (lightweight-charts has exactly five line styles). `dash` mirrors the chart pattern for the legend swatch.
const ANALOG_LINE_COLOR = "rgba(125, 190, 255, 0.32)";
const ANALOG_SWATCH_COLOR = "rgba(125, 190, 255, 0.75)";
const EVENT_MARKER_COLOR = "rgba(240, 180, 41, 0.6)";
const ANALOG_STYLES: { style: LineStyle; dash: string }[] = [
  { style: LineStyle.Solid, dash: "" },
  { style: LineStyle.Dashed, dash: "4 4" },
  { style: LineStyle.Dotted, dash: "2 2" },
  { style: LineStyle.LargeDashed, dash: "12 12" },
  { style: LineStyle.SparseDotted, dash: "2 8" },
];
const analogStyle = (rank: number): { style: LineStyle; dash: string } => ANALOG_STYLES[(rank - 1) % ANALOG_STYLES.length];
const analogLines: ISeriesApi<"Line">[] = ANALOG_STYLES.map((st) =>
  chart.addLineSeries({
    color: ANALOG_LINE_COLOR,
    lineWidth: 1,
    lineStyle: st.style,
    lastValueVisible: false,
    priceLineVisible: false,
    crosshairMarkerVisible: false,
  }),
);
const candles: ISeriesApi<"Candlestick"> = chart.addCandlestickSeries({
  upColor: "#26a69a",
  downColor: "#ef5350",
  borderVisible: false,
  wickUpColor: "#26a69a",
  wickDownColor: "#ef5350",
});

function swatch(rank: number, width = 34): string {
  const { dash } = analogStyle(rank);
  return (
    `<svg width="${width}" height="8" style="vertical-align:middle"><line x1="0" y1="4" x2="${width}" y2="4" ` +
    `stroke="${ANALOG_SWATCH_COLOR}" stroke-width="2"${dash ? ` stroke-dasharray="${dash}"` : ""}/></svg>`
  );
}

const shortDate = (iso: string, withYear: boolean): string =>
  new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", ...(withYear ? { year: "numeric" } : {}), timeZone: "UTC" });

/** The historical period an analog covers, e.g. "22 Jul - 29 Jul 2026". */
const period = (a: Analog): string => `${shortDate(a.window_start, false)} - ${shortDate(a.window_end, true)}`;

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

// Curated events (edit web/src/events.json). An event applies to every instrument unless it lists `instruments`.
const ALL_EVENTS = eventsData as unknown as MarketEvent[];
const eventTs = (e: MarketEvent): number => Date.parse(`${e.date}T${e.time ?? "12:00"}:00Z`) / 1000;
function eventsBetween(fromSec: number, toSec: number): MarketEvent[] {
  return ALL_EVENTS.filter((e) => !e.instruments || e.instruments.includes(instrument))
    .filter((e) => eventTs(e) >= fromSec && eventTs(e) <= toSec)
    .sort((x, y) => eventTs(x) - eventTs(y));
}
/** Events inside an analog's period (its window plus the forward days that followed). */
function analogEvents(a: Analog): MarketEvent[] {
  if (a.times?.length) return eventsBetween(a.times[0], a.times[a.times.length - 1]);
  return eventsBetween(Date.parse(a.window_start) / 1000, Date.parse(a.window_end) / 1000 + 3 * 86400);
}

/** Analog path point j -> a chart time: past points sit on real bars, forward points continue hourly. */
function analogTimeAt(a: Analog, asofIdx: number, j: number): number | null {
  if (j <= a.end_idx) {
    const idx = asofIdx - (a.end_idx - j);
    return idx < 0 ? null : bars[idx].time;
  }
  return bars[asofIdx].time + (j - a.end_idx) * HOUR;
}

function analogPoints(a: Analog, asofIdx: number): { time: UTCTimestamp; value: number }[] {
  const out: { time: UTCTimestamp; value: number }[] = [];
  a.path.forEach((value, j) => {
    const time = analogTimeAt(a, asofIdx, j);
    if (time != null) out.push({ time: time as UTCTimestamp, value });
  });
  return out;
}

/** Event dots on an analog's own path, at the bar where the event happened in that historical period. */
function analogEventMarkers(a: Analog, asofIdx: number): SeriesMarker<Time>[] {
  if (!a.times?.length) return [];
  const out: SeriesMarker<Time>[] = [];
  for (const e of analogEvents(a)) {
    const t = eventTs(e);
    const j = a.times.findIndex((x) => x >= t);
    const time = j < 0 ? null : analogTimeAt(a, asofIdx, j);
    if (time != null) out.push({ time: time as UTCTimestamp, position: "inBar", color: EVENT_MARKER_COLOR, shape: "circle", text: e.short });
  }
  return out.sort((x, y) => (x.time as number) - (y.time as number));
}

/** Events that fall inside the price history currently on the chart. */
function candleEventMarkers(): SeriesMarker<Time>[] {
  if (!bars.length) return [];
  return eventsBetween(bars[0].time, bars[bars.length - 1].time + HOUR).map((e) => ({
    time: bars[barIndexAt(eventTs(e))].time as UTCTimestamp,
    position: "belowBar" as const,
    color: "#f0b429",
    shape: "square" as const,
    text: e.short,
  }));
}

const PRICE_AT = 0.3; // the latest price sits this far from the left edge; the trajectories use the rest

/** Latest price at 30% of the width, and the forward trajectories filling the other 70%. */
function applyDefaultView(): void {
  if (!bars.length) return;
  const analogs = states.get(instrument)?.analysis?.analogs ?? [];
  const forward = Math.max(0, ...analogs.map((a) => a.path.length - 1 - a.end_idx));
  const ahead = (forward || 72) + 3; // bars to the right of the latest price
  const span = ahead / (1 - PRICE_AT); // visible bars in total
  const last = bars.length - 1;
  chart.timeScale().setVisibleLogicalRange({ from: last - PRICE_AT * span, to: last + (1 - PRICE_AT) * span });
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
  // The price scale takes its label format from the first series, which is now an analog line, so set all of them.
  const priceFormat = { type: "price" as const, precision, minMove: 1 / 10 ** precision };
  candles.applyOptions({ priceFormat });
  analogLines.forEach((l) => l.applyOptions({ priceFormat }));
  candles.setData(bars.map((b) => ({ ...b, time: b.time as UTCTimestamp })));
  analogLines.forEach((l) => {
    l.setData([]);
    l.setMarkers([]);
  });
  const markers: SeriesMarker<Time>[] = [];

  if (analysis && bars.length) {
    const asofIdx = barIndexAt(Date.parse(analysis.asof) / 1000);
    if (showAnalogs) {
      for (const a of analysis.analogs) {
        const line = analogLines[(a.rank - 1) % analogLines.length];
        line.setData(analogPoints(a, asofIdx));
        line.setMarkers(analogEventMarkers(a, asofIdx));
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
  markers.push(...candleEventMarkers());
  markers.sort((a, b) => (a.time as number) - (b.time as number));
  candles.setMarkers(markers);
  renderLegend(analysis?.analogs ?? []);
}

function setPriceLines(plan: Plan | undefined, order?: LiveState["runner"]): void {
  const defs: { price: number; color: string; title: string; style: LineStyle }[] = [];
  for (const t of trades.filter((x) => x.status === "filled")) {
    if (t.entry != null)
      defs.push({ price: t.entry, color: "#4c9be8", title: `#${t.ticket} open ${usd(t.open_pnl_usd)}`, style: LineStyle.Solid });
    if (t.sl != null) defs.push({ price: t.sl, color: "#ef5350", title: `#${t.ticket} stop`, style: LineStyle.Dotted });
    if (t.tp != null) defs.push({ price: t.tp, color: "#26a69a", title: `#${t.ticket} target`, style: LineStyle.Dotted });
  }
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
    analogs.map((a) => `<span>${swatch(a.rank)} #${a.rank} ${period(a)}</span>`).join("");
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
  if (st.tick) pills.push(`<span class="pill">bid ${fmt(st.tick.bid)} / ask ${fmt(st.tick.ask)}${st.tick.last != null ? " / last " + fmt(st.tick.last) : ""}</span>`);
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

/** What a stop-out really costs as % of balance (below the target when the broker's max lot cuts the size). */
const realRiskPct = (p: Plan): string => (((p.risk_usd_actual ?? p.risk_usd) / p.balance) * 100).toFixed(1);

function planLine(p: Plan): string {
  return (
    `limit ${fmt(p.limit)} &middot; stop ${fmt(p.sl)} &middot; target ${fmt(p.tp)} &middot; ${fmt(p.lots, 2)} lots` +
    ` &middot; risk ${usd(p.risk_usd_actual ?? p.risk_usd).replace("+", "")} (${realRiskPct(p)}% of balance)` +
    ` &middot; R:R ${fmt(p.rr, 2)}`
  );
}

function renderManual(): void {
  const st = states.get(instrument);
  const plans = st?.tick?.plans;
  const live = !!st?.runner?.allow_live && isArmed(control);
  const risk = st?.runner?.risk_pct ?? control?.risk_pct ?? 0.03;
  const pending = commands.some((c) => c.status === "pending");
  const btn = (dir: "LONG" | "SHORT"): string => {
    const p = plans?.[dir];
    const warn = p?.warnings?.length ? `<div class="preview warn-txt">${p.warnings.map(esc).join("; ")}</div>` : "";
    return (
      `<div><button class="btn ${dir.toLowerCase()}" data-dir="${dir}" ${!p || pending ? "disabled" : ""}>Enter ${dir}</button>` +
      `<div class="preview">${p ? planLine(p) : "waiting for a plan..."}</div>${warn}</div>`
    );
  };
  const recent = commands
    .map((c) => {
      const r = c.result;
      const detail = r?.ticket ? `#${r.ticket}` : (r?.reason ?? "");
      const colour = c.status === "placed" ? "pos" : c.status === "rejected" || c.status === "expired" ? "neg" : "dim";
      return `<div class="cmd"><span>${esc(c.action.replace("ENTER_", ""))} <span class="${colour}">${esc(c.status)}</span></span><span class="dim">${esc(detail)}</span></div>`;
    })
    .join("");
  $("manual").innerHTML =
    `<h3>Manual fighter entry - ${esc(instrument)}</h3><div class="manual">${btn("LONG")}${btn("SHORT")}</div>` +
    `<div class="note">Sized so a stop-out loses ${(risk * 100).toFixed(1)}% of balance (spread included), with the algorithm's ` +
    `stop/target distances. ${live ? "<b>Live: places a real order.</b>" : "Simulated: builds the order, sends nothing (needs --allow-live and the switch armed)."} ` +
    `Distances were tuned on shorts, so a long is untested.</div>${recent ? `<div style="margin-top:6px">${recent}</div>` : ""}`;
  $("manual").querySelectorAll("button[data-dir]").forEach((b) => {
    b.addEventListener("click", () => void enter((b as HTMLButtonElement).dataset.dir as "LONG" | "SHORT"));
  });
}

async function enter(dir: "LONG" | "SHORT"): Promise<void> {
  const p = states.get(instrument)?.tick?.plans?.[dir];
  if (!p) return;
  const live = !!states.get(instrument)?.runner?.allow_live && isArmed(control);
  const ok = confirm(
    `${live ? "PLACE A REAL ORDER" : "Simulate"}: ${dir} fighter entry on ${instrument}\n\n` +
      `limit ${fmt(p.limit)}, stop ${fmt(p.sl)}, target ${fmt(p.tp)}\n${fmt(p.lots, 2)} lots, risk ${usd(p.risk_usd_actual ?? p.risk_usd).replace("+", "")} (${realRiskPct(p)}% of balance; target ${((p.risk_pct ?? 0) * 100).toFixed(1)}%)` +
      (p.warnings?.length ? `\n\nWarnings: ${p.warnings.join("; ")}` : ""),
  );
  if (!ok) return;
  try {
    await createCommand(instrument, dir === "LONG" ? "ENTER_LONG" : "ENTER_SHORT");
    commands = await getCommands(instrument);
    renderManual();
    for (let i = 0; i < 12 && commands.some((c) => c.status === "pending"); i++) {
      await new Promise((r) => setTimeout(r, 1500));
      commands = await getCommands(instrument);
      renderManual();
    }
  } catch (e) {
    alert(`Could not send the command: ${e instanceof Error ? e.message : e}`);
  }
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
    `<span>lots</span><span>${fmt(p.lots, 2)}${p.lot_capped ? " (capped)" : ""}</span>` +
    `<span>risk if stopped</span><span>$${(p.risk_usd_actual ?? p.risk_usd).toFixed(0)} of $${p.balance.toFixed(0)} (target $${p.risk_usd.toFixed(0)})</span>` +
    `<span>cancel after</span><span>${p.ttl_bars} bar(s)</span><span>re-price every</span><span>${p.reprice_every || "never"}</span>` +
    `<span>spread cost</span><span>${costR == null ? "-" : costR.toFixed(2)}R</span></div>` +
    (p.warnings?.length ? `<div class="note warn-txt">${p.warnings.map(esc).join("<br>")}</div>` : "");
}

function eventChips(a: Analog): string {
  const evs = analogEvents(a);
  if (!evs.length) return "";
  return (
    `<tr><td></td><td colspan="3" class="ev">` +
    evs.map((e) => `<span class="${esc(e.category)}" title="${esc(e.title)}">${shortDate(`${e.date}T00:00:00Z`, false)} ${esc(e.short)}</span>`).join("") +
    `</td></tr>`
  );
}

function renderAnalogs(): void {
  const a = states.get(instrument)?.analysis;
  if (!a) {
    $("analogs").innerHTML = "";
    return;
  }
  const med = median(a.analogs.map((x) => x.fwd_move_atr).filter((v): v is number => v != null));
  $("analogs").innerHTML =
    `<h3>Most similar past windows</h3><table><tr><th>#</th><th>period</th><th class="num">dist</th><th class="num">next ${a.forward_days}d (ATR)</th></tr>` +
    a.analogs
      .map(
        (x) =>
          `<tr><td>${swatch(x.rank, 22)}</td><td>${esc(period(x))}${x.same_regime ? "" : ' <span class="dim">*</span>'}</td>` +
          `<td class="num">${x.distance.toFixed(2)}</td><td class="num ${cls(x.fwd_move_atr)}">${fmt(x.fwd_move_atr, 2)}</td></tr>` +
          eventChips(x),
      )
      .join("") +
    `</table><div class="note">median forward move ${fmt(med, 2)} ATR. * different regime</div>`;
}

function renderPnl(): void {
  const open = trades.filter((t) => t.status === "filled");
  const closed = trades.filter((t) => t.status === "closed");
  const openUsd = open.reduce((a, t) => a + (t.open_pnl_usd ?? 0), 0);
  const realUsd = closed.reduce((a, t) => a + (t.pnl_usd ?? 0), 0);
  const openR = open.reduce((a, t) => a + (t.open_r ?? 0), 0);
  const costs = open.reduce((a, t) => a + (t.open_costs_usd ?? 0), 0);
  const marked = open.find((t) => t.mark_price != null);
  const wins = closed.filter((t) => (t.pnl_usd ?? 0) > 0).length;
  $("pnl").innerHTML =
    `<h3>Live P&amp;L - ${esc(instrument)}</h3><div class="kv">` +
    `<span>open P&amp;L</span><span class="${cls(openUsd)} big">${usd(openUsd)}</span>` +
    `<span>open positions</span><span>${open.length}${open.length ? ` (${openR >= 0 ? "+" : ""}${openR.toFixed(2)}R)` : ""}</span>` +
    `<span>realized P&amp;L</span><span class="${cls(realUsd)} big">${usd(realUsd)}</span>` +
    `<span>closed trades</span><span>${closed.length}${closed.length ? ` (${wins} won)` : ""}</span>` +
    `<span>total</span><span class="${cls(openUsd + realUsd)}">${usd(openUsd + realUsd)}</span></div>` +
    (open.length
      ? `<div class="note">Open P&amp;L is marked from the polled ${marked ? `${marked.direction === "SHORT" ? "ask" : "bid"} ${fmt(marked.mark_price)}` : "bid/ask"}` +
        `${marked?.last_price != null ? ` (last ${fmt(marked.last_price)})` : ""}. Swap/commission so far: ${usd(costs)} (not included).</div>`
      : "");
}

function renderEvents(): void {
  const nowS = Date.now() / 1000;
  const recent = eventsBetween(nowS - 14 * 86400, nowS);
  const upcoming = eventsBetween(nowS, nowS + 45 * 86400);
  const rows = (list: MarketEvent[]): string =>
    list.length
      ? `<table>` +
        list
          .map((e) => `<tr><td class="dim" style="white-space:nowrap">${shortDate(`${e.date}T00:00:00Z`, false)}</td><td title="${esc(e.title)}"><span class="cat ${esc(e.category)}"></span>${esc(e.title)}</td></tr>`)
          .join("") +
        `</table>`
      : `<div class="dim">none recorded</div>`;
  $("events").innerHTML =
    `<h3>Events - last 14 days</h3>${rows(recent)}<h3 style="margin-top:8px">Upcoming (45 days)</h3>${rows(upcoming)}` +
    `<div class="note">Curated list (Fed calendar and a sourced conflict timeline), not exhaustive: no event listed does not mean nothing happened. Edit web/src/events.json.</div>`;
}

function renderTrades(): void {
  $("trades").innerHTML =
    `<h3>Recent live trades</h3>` +
    (trades.length
      ? `<table><tr><th>ticket</th><th>status</th><th class="num">entry</th><th class="num">open P&amp;L</th><th class="num">realized</th></tr>` +
        trades
          .slice(0, 8)
          .map(
            (t) =>
              `<tr><td>${esc(t.ticket)}</td><td>${esc(t.status)}${t.exit_reason ? " (" + esc(t.exit_reason) + ")" : ""}</td>` +
              `<td class="num">${fmt(t.entry ?? t.limit_price)}</td>` +
              `<td class="num ${cls(t.open_pnl_usd)}">${t.status === "filled" ? usd(t.open_pnl_usd) + (t.open_r != null ? ` (${t.open_r.toFixed(2)}R)` : "") : "-"}</td>` +
              `<td class="num ${cls(t.pnl_usd)}">${t.status === "closed" ? usd(t.pnl_usd) + (t.r_multiple != null ? ` (${t.r_multiple.toFixed(2)}R)` : "") : "-"}</td></tr>`,
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
  renderManual();
  renderPnl();
  renderDecision();
  renderPlan();
  renderEvents();
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
  [control, trades, decisions, commands] = await Promise.all([
    getControl(instrument),
    getTrades(instrument),
    getDecisions(instrument),
    getCommands(instrument),
  ]);
  drawChart();
  renderAll();
}

async function pollTick(): Promise<void> {
  const list = await getLiveStates();
  states = new Map(list.map((s) => [s.instrument, s]));
  if (!instrument || !states.has(instrument)) instrument = list[0]?.instrument ?? instrument;
  renderInstruments();
  const st = states.get(instrument);
  if (instrument) {
    trades = await getTrades(instrument);
    renderPnl();
    renderTrades();
  }
  setPriceLines(st?.tick?.plan, st?.runner);
  updateForming();
  drawChart();
  renderStatus();
  renderManual();
  renderDecision();
  renderPlan();
  renderEvents();
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
  commands = [];
  drawnKey = "";
  linesKey = "";
  renderInstruments();
  await Promise.all([loadBars(), loadSlow(), pollTick()]);
  drawChart();
  applyDefaultView();
  renderAll();
}

function guard(fn: () => Promise<void>): () => void {
  return () => {
    fn().catch((e) => {
      $("status").innerHTML = `<span class="pill bad">${esc(e instanceof Error ? e.message : e)}</span>`;
    });
  };
}

$("reset-view").addEventListener("click", applyDefaultView);

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
