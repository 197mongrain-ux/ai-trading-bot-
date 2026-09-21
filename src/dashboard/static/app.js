/* hl-bot dashboard — polls /api/state every 2s; TradingView embed */
(function () {
  "use strict";

  const POLL_MS = 2000;
  let tvSymbol = "HYPERLIQUID:BTCUSDC.P";
  let tvInterval = "1";
  let widgetReady = false;
  let seenTradeKeys = new Set();
  let firstPaint = true;
  let tvScriptLoading = false;

  function $(id) { return document.getElementById(id); }

  function fmtPx(n) {
    if (n == null || Number.isNaN(Number(n))) return "—";
    const x = Number(n);
    if (x >= 1000) return x.toLocaleString(undefined, { maximumFractionDigits: 2 });
    if (x >= 1) return x.toFixed(4);
    return x.toPrecision(4);
  }

  function fmtSz(n) {
    if (n == null || Number.isNaN(Number(n))) return "—";
    return Number(n).toPrecision(4);
  }

  function fmtUsd(n) {
    if (n == null || Number.isNaN(Number(n))) return "—";
    const x = Number(n);
    const sign = x < 0 ? "-" : "";
    return sign + "$" + Math.abs(x).toFixed(2);
  }

  function pnlClass(n) {
    if (n == null || Number.isNaN(Number(n))) return "";
    const x = Number(n);
    if (x > 0) return "pos";
    if (x < 0) return "neg";
    return "";
  }

  function fmtTs(ts) {
    if (ts == null) return "—";
    const d = new Date(Number(ts) * 1000);
    if (Number.isNaN(d.getTime())) return "—";
    return d.toLocaleTimeString(undefined, { hour12: false });
  }

  function fmtAgo(ts) {
    if (ts == null) return "—";
    const sec = Math.max(0, Math.floor(Date.now() / 1000 - Number(ts)));
    if (sec < 60) return sec + "s ago";
    if (sec < 3600) return Math.floor(sec / 60) + "m ago";
    return Math.floor(sec / 3600) + "h ago";
  }

  function tradeKey(t) {
    return [t.ts, t.event, t.trade_id || "", t.symbol || "", t.price || ""].join("|");
  }

  function loadTvScript(cb) {
    if (window.TradingView) { cb(); return; }
    if (tvScriptLoading) return;
    tvScriptLoading = true;
    const s = document.createElement("script");
    s.src = "https://s3.tradingview.com/tv.js";
    s.async = true;
    s.onload = function () { tvScriptLoading = false; cb(); };
    s.onerror = function () { tvScriptLoading = false; };
    document.head.appendChild(s);
  }

  function renderChart() {
    const box = $("tvContainer");
    if (!box) return;
    box.innerHTML = '<div class="tv-offline">Loading chart… (needs network)</div>';
    loadTvScript(function () {
      if (!window.TradingView) return;
      box.innerHTML = "";
      const host = document.createElement("div");
      host.id = "tv_chart";
      host.style.height = "100%";
      host.style.width = "100%";
      box.appendChild(host);
      try {
        // eslint-disable-next-line no-new
        new window.TradingView.widget({
          autosize: true,
          symbol: tvSymbol,
          interval: tvInterval,
          timezone: "Etc/UTC",
          theme: "dark",
          style: "1",
          locale: "en",
          toolbar_bg: "#0b0e14",
          enable_publishing: false,
          hide_top_toolbar: false,
          hide_legend: false,
          container_id: "tv_chart",
          allow_symbol_change: false,
        });
        widgetReady = true;
      } catch (e) {
        box.innerHTML = '<div class="tv-offline">TradingView unavailable. Tape still live.</div>';
      }
    });
  }

  function bindTabs() {
    document.querySelectorAll(".sym").forEach(function (btn) {
      btn.addEventListener("click", function () {
        document.querySelectorAll(".sym").forEach(function (b) { b.classList.remove("active"); });
        btn.classList.add("active");
        tvSymbol = btn.getAttribute("data-tv");
        renderChart();
      });
    });
    document.querySelectorAll(".iv").forEach(function (btn) {
      btn.addEventListener("click", function () {
        document.querySelectorAll(".iv").forEach(function (b) { b.classList.remove("active"); });
        btn.classList.add("active");
        tvInterval = btn.getAttribute("data-iv");
        renderChart();
      });
    });
  }

  function setPnlEl(el, value) {
    el.textContent = fmtUsd(value);
    el.classList.remove("pos", "neg");
    const c = pnlClass(value);
    if (c) el.classList.add(c);
  }

  function renderStatus(status) {
    const badge = $("modeBadge");
    const mode = (status.mode || "UNKNOWN").toUpperCase();
    badge.textContent = mode === "LIVE" ? "LIVE" : "PAPER";
    badge.className = "badge " + (mode === "LIVE" ? "live" : "paper");

    const running = !!status.running;
    const pill = $("runningPill");
    pill.textContent = running ? "RUNNING" : "IDLE / STOPPED";
    pill.className = "pill " + (running ? "on" : "off");

    $("lastEvent").textContent =
      "last: " + (status.last_event || "—") + " · " + fmtAgo(status.last_journal_ts);
    $("symbolsMeta").textContent =
      "symbols: " + ((status.symbols && status.symbols.join(", ")) || "—");
  }

  function renderStats(stats) {
    $("dayOpens").textContent = String(stats.day_opens ?? 0);
    $("dayCloses").textContent = String(stats.day_closes ?? 0);
    setPnlEl($("dayPnl"), stats.day_realized_pnl);
    $("sessOpens").textContent = String(stats.session_opens ?? 0);
    $("sessCloses").textContent = String(stats.session_closes ?? 0);
    setPnlEl($("sessPnl"), stats.session_realized_pnl);
    const upnl = stats.unrealized_pnl;
    const el = $("openUpnl");
    el.textContent = (stats.open_count ?? 0) + " / " + (upnl == null ? "—" : fmtUsd(upnl));
    el.classList.remove("pos", "neg");
    const c = pnlClass(upnl);
    if (c) el.classList.add(c);
  }

  function renderPositions(rows) {
    const body = $("posBody");
    if (!rows || !rows.length) {
      body.innerHTML = '<tr class="empty"><td colspan="8">No open positions</td></tr>';
      return;
    }
    body.innerHTML = rows.map(function (p) {
      const side = (p.side || "").toLowerCase();
      return (
        "<tr>" +
        "<td>" + (p.symbol || "—") + "</td>" +
        '<td class="side-' + side + '">' + (side || "—") + "</td>" +
        "<td>" + fmtSz(p.size) + (p.scaled ? ' <span class="muted">scaled</span>' : "") + "</td>" +
        "<td>" + fmtPx(p.entry) + "</td>" +
        "<td>" + fmtPx(p.stop) + "</td>" +
        "<td>" + fmtPx(p.tp) + "</td>" +
        "<td>" + fmtPx(p.mark) + "</td>" +
        '<td class="' + pnlClass(p.upnl) + '">' + fmtUsd(p.upnl) + "</td>" +
        "</tr>"
      );
    }).join("");
  }

  function renderTape(trades) {
    const tape = $("tape");
    $("tapeCount").textContent = String((trades && trades.length) || 0);
    if (!trades || !trades.length) {
      tape.innerHTML = '<div class="empty">Waiting for journal events…</div>';
      seenTradeKeys = new Set();
      return;
    }

    const nextKeys = new Set();
    const html = trades.map(function (t) {
      const key = tradeKey(t);
      nextKeys.add(key);
      const isNew = !firstPaint && !seenTradeKeys.has(key);
      const ev = t.event || "";
      const side = (t.side || "").toLowerCase();
      let right = "";
      if ((ev === "close" || ev === "scale_out") && t.pnl != null) {
        right = '<span class="pnl ' + pnlClass(t.pnl) + '">' + fmtUsd(t.pnl) + "</span>";
        if (t.reason) right += ' <span class="muted">' + t.reason + "</span>";
        if (ev === "scale_out" && t.remaining_size != null)
          right += ' <span class="muted">rem ' + fmtSz(t.remaining_size) + "</span>";
      } else if (ev === "open") {
        right = '<span class="muted">stop ' + fmtPx(t.stop) + " · tp " + fmtPx(t.tp) + "</span>";
      }
      return (
        '<div class="tape-row' + (isNew ? " flash" : "") + '" data-key="' + key + '">' +
        '<span class="muted">' + fmtTs(t.ts) + "</span>" +
        '<span class="ev-' + ev + '">' + ev.toUpperCase() + "</span>" +
        "<span>" + (t.symbol || "—") + "</span>" +
        '<span class="side-' + side + '">' + (side || "—") + "</span>" +
        "<span>" + fmtPx(t.price) + " × " + fmtSz(t.size) + "</span>" +
        "<span>" + right + "</span>" +
        "</div>"
      );
    }).join("");

    tape.innerHTML = html;
    seenTradeKeys = nextKeys;
    firstPaint = false;
  }

  async function poll() {
    try {
      const res = await fetch("/api/state", { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      renderStatus(data.status || {});
      renderStats(data.stats || {});
      renderPositions(data.open_positions || []);
      renderTape(data.trades || []);
    } catch (e) {
      $("runningPill").textContent = "API ERR";
      $("runningPill").className = "pill off";
    }
  }

  function tickClock() {
    $("clock").textContent = new Date().toLocaleString(undefined, {
      hour12: false,
      timeZoneName: "short",
    });
  }

  bindTabs();
  renderChart();
  tickClock();
  setInterval(tickClock, 1000);
  poll();
  setInterval(poll, POLL_MS);
})();
