import { h, api, follow, fmt, lineChart, flowStrip, traceList, flowStatus } from "/kit/kit.js";

const $ = (id) => document.getElementById(id);
const S = { meta: null, runs: [], runId: null, run: null, events: [], out: null, scored: null,
            history: [], live: false, stop: null, coin: null, open: null };

function latest() {
  const o = S.run?.output;
  if (o) { S.out = o; return; }
  // live: assemble a partial view from events
  const out = { market: null, crowd: [], whales: [], stances: [], audit: [], headline: null, desk_note: null };
  for (const ev of S.events) {
    if (ev.type === "market") out.market = ev.payload;
    if (ev.type === "whales") { out.whales = ev.payload.accounts; out.crowd = ev.payload.crowd; }
    if (ev.type === "briefing") Object.assign(out, ev.payload);
    if (ev.type === "audit") out.audit.push(ev.payload);
  }
  S.out = out.market ? out : null;
}

function renderLede() {
  const o = S.out, el = $("lede");
  if (!o || !o.headline) {
    el.replaceChildren(h("span", { class: "kicker" }, "desk briefing"),
      h("h3", {}, S.live ? "Reading the tape…" : "No briefing yet"),
      h("p", { class: "note" }, S.live ? "The agent is fetching market state and whale positions." : "Generate a briefing to see the desk's read on current positioning."));
    return;
  }
  const when = S.out.ts ? new Date(S.out.ts * 1000).toLocaleString("en-GB") : "";
  el.replaceChildren(
    h("span", { class: "kicker" }, "desk briefing"),
    h("h3", {}, o.headline),
    h("p", { class: "note" }, o.desk_note || ""),
    h("p", { class: "meta" }, [when, S.scored ? `scored ${S.scored.when} · hit rate ${fmt.pct(S.scored.hit_rate, 0)}${S.scored.final ? " · final" : " · interim"}` : `stances are judged over the next ${S.meta.horizon_h}h`].filter(Boolean).join("  ·  ")));
}

const tiltBar = (t) => {
  const w = Math.min(Math.abs(t) * 22, 22);
  return h("span", { class: "tilt" }, h("span", { class: "rail" },
    h("i", { style: { left: t < 0 ? (22 - w) + "px" : "22px", width: w + "px", background: t >= 0 ? "var(--good)" : "var(--bad)" } })),
    h("span", {}, fmt.signed(t, 2)));
};

function renderBoard() {
  const o = S.out, el = $("board");
  if (!o?.market) { el.replaceChildren(h("caption", { class: "empty" }, "The board fills when a briefing runs.")); return; }
  const stance = (c) => o.stances?.find((s) => s.coin === c);
  const verdict = (c) => S.scored?.stances?.find((s) => s.coin === c);
  const rows = [];
  for (const c of o.market.coins) {
    const cr = o.crowd.find((x) => x.coin === c.coin) || {};
    const st = stance(c.coin), v = verdict(c.coin);
    rows.push(h("tr", { class: S.open === c.coin ? "sel" : "", onclick: () => { S.open = S.open === c.coin ? null : c.coin; S.coin = c.coin; renderBoard(); renderChart(); } },
      h("td", { class: "coin" }, c.coin, h("span", { class: "sub" }, `OI ${fmt.usd(c.oi_usd)}`)),
      h("td", {}, c.mark < 1 ? c.mark.toFixed(4) : fmt.num(c.mark, c.mark < 100 ? 2 : 0)),
      h("td", { class: c.chg_24h >= 0 ? "pos" : "neg" }, fmt.signedPct(c.chg_24h)),
      h("td", { class: Math.abs(c.funding_apr) > 0.15 ? (c.funding_apr > 0 ? "neg" : "pos") : "dim" }, fmt.signedPct(c.funding_apr, 1), h("span", { class: "sub" }, "APR")),
      h("td", {}, cr.net_tilt != null ? tiltBar(cr.net_tilt) : "–", h("span", { class: "sub" }, cr.whales_long != null ? `${cr.whales_long}L / ${cr.whales_short}S · ${fmt.usd(cr.gross_usd)}` : "")),
      h("td", {}, cr.avg_leverage ? `${cr.avg_leverage}×` : "–"),
      h("td", {}, st ? h("span", { class: `stance ${st.stance}` }, st.stance, h("span", { class: "conv" }, "•".repeat(st.conviction))) : h("span", { class: "dim" }, "–")),
      h("td", {}, v ? [h("span", { class: `verdict ${v.verdict}` }, fmt.signedPct(v.move)), h("span", { class: "sub" }, v.verdict)] : h("span", { class: "dim" }, "–"))));
    if (S.open === c.coin && st) {
      rows.push(h("tr", { class: "detail-row" }, h("td", { colspan: 8 },
        h("b", {}, "Thesis: "), st.thesis, " ", h("span", { class: "inv" }, "Invalidation: " + st.invalidation),
        st.px_at_call ? h("span", { class: "dim" }, `  · called at ${st.px_at_call}`) : null)));
    }
  }
  el.replaceChildren(
    h("thead", {}, h("tr", {}, ["Coin", "Mark", "24h", "Funding", "Whale tilt", "Lev", "Desk call", "Outcome"].map((t) => h("th", {}, t)))),
    h("tbody", {}, rows));
}

async function renderChart() {
  const el = $("chart"), coin = S.coin || S.meta.coins[0];
  $("chart-title").textContent = `${coin} price`;
  try {
    const ks = await api(`/api/candles?coin=${coin}`);
    const st = S.out?.stances?.find((s) => s.coin === coin);
    lineChart(el, {
      height: 190, yFmt: (v) => (v < 1 ? v.toFixed(4) : fmt.num(v, v < 100 ? 2 : 0)), xFmt: (t) => new Date(t * 1000).toLocaleString("en-GB", { day: "2-digit", month: "short" }),
      series: [{ name: coin, points: ks.map((k) => [k.t, k.c]), color: "var(--accent)", width: 1.6 },
               ...(st?.px_at_call ? [{ name: "call", points: [[ks[0].t, st.px_at_call], [ks[ks.length - 1].t, st.px_at_call]], color: "var(--warn)", dash: "4 4", width: 1 }] : [])],
    });
  } catch (e) { el.replaceChildren(h("div", { class: "empty" }, e.message)); }
}

function renderWhales() {
  const el = $("whales"), ws = S.out?.whales || [];
  if (!ws.length) { el.replaceChildren(h("caption", { class: "empty" }, "Whale panel loads with a briefing.")); return; }
  el.replaceChildren(
    h("thead", {}, h("tr", {}, ["Account", "Equity", "30d ROI", "Positions (tracked coins)"].map((t) => h("th", {}, t)))),
    h("tbody", {}, ws.map((w) => h("tr", { onclick: () => {} },
      h("td", { class: "coin" }, w.display || w.address.slice(0, 8) + "…"),
      h("td", {}, fmt.usd(w.account_usd)),
      h("td", { class: w.roi_month >= 0 ? "pos" : "neg" }, fmt.signedPct(w.roi_month)),
      h("td", {}, w.positions.map((p) => h("span", { class: `stance ${p.side}`, style: { marginLeft: "6px" } }, `${p.coin} ${fmt.usd(p.ntl_usd)}${p.leverage ? " " + p.leverage + "×" : ""}`)))))));
}

function renderAudit() {
  const rows = S.out?.audit || [];
  $("audit").replaceChildren(...(rows.length ? rows.map((a) => h("div", { class: "audit-row" },
    h("span", { class: "coin" }, a.coin), h("span", { class: `v ${a.verdict}` }, a.verdict), h("p", {}, a.note)))
    : [h("div", { class: "empty" }, "The audit pass runs after the briefing is drafted.")]));
}

function renderScoreboard() {
  const el = $("scoreboard");
  const scored = S.history.filter((r) => r.scored);
  if (!scored.length) { el.replaceChildren(h("div", { class: "empty" }, "No scored briefings yet. Re-score after some time has passed.")); return; }
  const finals = scored.filter((r) => r.scored.final);
  const all = scored.flatMap((r) => r.scored.stances);
  const hit = all.filter((s) => s.verdict === "correct").length / all.length;
  const hi = all.filter((s) => s.conviction >= 4);
  el.replaceChildren(
    h("dl", { class: "scorecard" },
      h("div", {}, h("dt", {}, "Briefings scored"), h("dd", {}, `${scored.length}`, h("small", { class: "dim" }, ` (${finals.length} final)`))),
      h("div", {}, h("dt", {}, "Call hit rate"), h("dd", { class: hit >= 0.5 ? "pos" : "neg" }, fmt.pct(hit, 0))),
      h("div", {}, h("dt", {}, "Conv ≥4 hit rate"), h("dd", {}, hi.length ? fmt.pct(hi.filter((s) => s.verdict === "correct").length / hi.length, 0) : "–"))),
    ...scored.slice(-8).reverse().map((r) => h("div", { class: "score-row" },
      h("span", {}, new Date(r.ts * 1000).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })),
      h("span", { class: "marks" }, r.scored.stances.map((s) => h("i", { class: s.verdict, title: `${s.coin} ${s.stance}: ${fmt.signedPct(s.move)}` }))),
      h("span", {}, fmt.pct(r.scored.hit_rate, 0)))));
}

async function renderPlaybook() {
  const rows = await api("/api/memory");
  $("playbook").replaceChildren(...(rows.length ? rows.sort((a, b) => b.utility - a.utility).map((m) =>
    h("div", { class: "lesson" }, h("div", { class: "u" }, fmt.num(m.utility, 2), h("small", {}, `${m.uses} recalls`)), h("p", {}, m.text)))
    : [h("div", { class: "empty" }, "Lessons appear once briefings reach their horizon and get scored.")]));
}

function renderTrace() {
  traceList($("trace"), S.events, (ev) => {
    const p = ev.payload, row = (label, note) => h("div", { class: "kit-trace-row sub" },
      h("span", { class: "kit-trace-time" }, ""), h("span", { class: "kit-trace-node" }, label), h("span", { class: "kit-trace-note" }, note), h("span", {}));
    if (ev.type === "recall") return row("recall", `${p.lessons.length} lessons · ${p.notes.join(", ")}`);
    if (ev.type === "audit") return row("audit", `${p.coin}: ${p.verdict}`);
    return null;
  });
  const calls = S.events.filter((e) => e.type === "llm" && e.payload.source !== "baseline");
  $("trace-hint").textContent = calls.length ? `${calls.length} model calls · ${fmt.ms(calls.reduce((s, e) => s + e.payload.ms, 0))}` : "";
}

function render() {
  latest();
  renderLede(); renderBoard(); renderWhales(); renderAudit(); renderScoreboard(); renderTrace();
  flowStrip($("flow"), S.meta.graph, flowStatus(S.events));
  renderChart();
  $("start").disabled = S.live; $("start").textContent = S.live ? "Briefing…" : "New briefing";
}

async function loadRuns() {
  S.runs = await api("/api/runs?kind=brief");
  S.history = await api("/api/history");
  $("run-select").replaceChildren(...(S.runs.length ? S.runs.map((r) =>
    h("option", { value: r.id, selected: r.id === S.runId },
      `#${r.id} · ${new Date(r.created * 1000).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}${r.status === "running" ? " · live" : ""}`))
    : [h("option", { value: "" }, "no briefings yet")]));
}

async function openRun(id) {
  S.stop && S.stop(); S.open = null;
  S.runId = id; S.run = await api(`/api/runs/${id}`); S.events = S.run.events; S.live = S.run.status === "running";
  S.scored = S.history.find((r) => r.run_id === id)?.scored || null;
  if (!S.scored && S.run.status === "done") {
    try { S.scored = await api(`/api/score/${id}`, {}); await loadRuns(); } catch {}
  }
  render(); renderPlaybook();
  if (S.live) {
    S.events = [];
    let timer = null;
    S.stop = follow(id, (ev) => { S.events.push(ev); if (!timer) timer = setTimeout(() => { timer = null; render(); }, 150); },
      async () => { S.live = false; await loadRuns(); await openRun(id); });
  }
}

async function main() {
  S.meta = await api("/api/meta");
  $("mode").textContent = S.meta.mode === "baseline" ? "no model configured: crowding heuristic only" : `${S.meta.model} via ${S.meta.mode}`;
  S.coin = S.meta.coins[0];
  await loadRuns();
  if (S.runs.length) await openRun(S.runs[0].id); else { render(); renderPlaybook(); }
  $("run-select").addEventListener("change", (e) => e.target.value && openRun(+e.target.value));
  $("start").addEventListener("click", async () => {
    try { const { run_id } = await api("/api/brief", {}); await loadRuns(); await openRun(run_id); await loadRuns(); }
    catch (err) { alert(err.message); }
  });
  $("score").addEventListener("click", async () => {
    if (!S.runId) return;
    try { S.scored = await api(`/api/score/${S.runId}`, {}); await loadRuns(); render(); renderPlaybook(); }
    catch (err) { alert(err.message); }
  });
}
main();
