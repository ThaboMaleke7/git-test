/* RAT · Repo Analysis Tool — dependency-free SVG chart helpers.
 *
 * Every helper renders into a container element and re-renders on window
 * resize (debounced).  Charts scale to the container width, heights are
 * fixed, and tooltips use native SVG <title> elements.
 */
(function (global) {
  "use strict";

  var NS = "http://www.w3.org/2000/svg";

  /* colours come from CSS custom properties so switching between the light
     and dark theme restyles every chart without a page reload */
  var colCache = null;
  function cssVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v && v.trim()) || fallback;
  }
  function COL() {
    if (!colCache) {
      colCache = {
        added: cssVar("--chart-added", "#3fb950"),
        removed: cssVar("--chart-removed", "#f85149"),
        accent: cssVar("--chart-accent", "#4f8cff"),
        grid: cssVar("--chart-grid", "#232f3e"),
        muted: cssVar("--chart-muted", "#8b98a5"),
        zero: cssVar("--chart-zero", "#3d4a5c"),
        text: cssVar("--chart-text", "#e6edf3"),
        track: cssVar("--donut-track", "#1b2430")
      };
    }
    return colCache;
  }
  var PALETTE = ["#4f8cff", "#3fb950", "#d29922", "#f85149", "#a371f7",
                 "#39c5cf", "#db61a2", "#e0723f"];
  var OTHER_COLOR = "#6e7681";
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, sans-serif";

  var redraws = new Map();
  var resizeTimer = null;
  global.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      redraws.forEach(function (fn) { fn(); });
    }, 160);
  });

  function remember(el, fn) {
    redraws.set(el, fn);
    colCache = null;
    fn();
  }

  function redraw(el) {
    var fn = redraws.get(el);
    if (fn) { colCache = null; fn(); }
  }

  function redrawAll() {
    colCache = null;
    redraws.forEach(function (fn) { fn(); });
  }

  /* ── tiny SVG helpers ─────────────────────────────────────────── */

  function svgEl(tag, attrs, parent) {
    var node = document.createElementNS(NS, tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (attrs[k] !== undefined && attrs[k] !== null) node.setAttribute(k, attrs[k]);
      });
    }
    if (parent) parent.appendChild(node);
    return node;
  }

  function svgText(parent, x, y, str, attrs) {
    var t = svgEl("text", Object.assign({
      x: x, y: y, fill: COL().muted, "font-size": 11.5, "font-family": FONT
    }, attrs || {}), parent);
    t.textContent = str;
    return t;
  }

  function svgTip(parent, str) {
    var t = svgEl("title", null, parent);
    t.textContent = str;
    return t;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function emptyNote(el, msg) {
    clear(el);
    var d = document.createElement("div");
    d.className = "bar-empty";
    d.textContent = msg;
    el.appendChild(d);
  }

  /* ── formatting ───────────────────────────────────────────────── */

  function fmtInt(n) {
    return Number(n || 0).toLocaleString("en-US");
  }

  function fmtCompact(n) {
    var v = Number(n || 0);
    var abs = Math.abs(v);
    if (abs >= 1e9) return (v / 1e9).toFixed(1).replace(/\.0$/, "") + "B";
    if (abs >= 1e6) return (v / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
    if (abs >= 1e4) return (v / 1e3).toFixed(0) + "k";
    if (abs >= 1e3) return (v / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
    return String(v);
  }

  function pad2(n) { return (n < 10 ? "0" : "") + n; }

  function fullStamp(ts) {
    var d = new Date(ts * 1000);
    return d.getUTCFullYear() + "-" + pad2(d.getUTCMonth() + 1) + "-" + pad2(d.getUTCDate())
      + " " + pad2(d.getUTCHours()) + ":" + pad2(d.getUTCMinutes()) + " UTC";
  }

  function bucketLabel(ts, bucket) {
    var d = new Date(ts * 1000);
    var mon = MONTHS[d.getUTCMonth()];
    if (bucket === "hour") {
      return mon + " " + d.getUTCDate() + " " + pad2(d.getUTCHours()) + ":00";
    }
    if (bucket === "month") {
      return mon + " '" + String(d.getUTCFullYear()).slice(2);
    }
    return mon + " " + d.getUTCDate();
  }

  function niceCeil(v) {
    if (!(v > 0)) return 1;
    var p = Math.pow(10, Math.floor(Math.log10(v)));
    var m = v / p;
    var step = m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10;
    return step * p;
  }

  /* ── shared scaffolding ───────────────────────────────────────── */

  function surface(el, height) {
    var W = Math.max(320, el.clientWidth || 0);
    var svg = svgEl("svg", { width: W, height: height, viewBox: "0 0 " + W + " " + height }, el);
    return svg;
  }

  function yGrid(svg, opts) {
    /* opts: {pad, iw, ih, yMin, yMax, steps, format} */
    var steps = opts.steps || 4;
    for (var i = 0; i <= steps; i++) {
      var v = opts.yMin + (opts.yMax - opts.yMin) * (i / steps);
      var y = opts.pad.t + opts.ih - opts.ih * ((v - opts.yMin) / (opts.yMax - opts.yMin));
      var isZero = Math.abs(v) < 1e-9 && opts.yMin < 0;
      svgEl("line", {
        x1: opts.pad.l, x2: opts.pad.l + opts.iw, y1: y, y2: y,
        stroke: isZero ? COL().zero : COL().grid, "stroke-width": 1
      }, svg);
      svgText(svg, opts.pad.l - 7, y + 3.5, (opts.format || fmtCompact)(v),
              { "text-anchor": "end" });
    }
  }

  function pointsOf(timeline) {
    return (timeline && timeline.points) || [];
  }

  /* ── stacked bars: lines added / removed per bucket ───────────── */

  function stackedBars(el, timeline, opts) {
    opts = opts || {};
    var H = opts.height || 250;
    var pad = { l: 56, r: 14, t: 12, b: 26 };

    remember(el, function () {
      var points = pointsOf(timeline);
      if (!points.length) { emptyNote(el, opts.emptyNote || "No activity in this commit set."); return; }
      clear(el);
      var svg = surface(el, H);
      var W = Number(svg.getAttribute("width"));
      var iw = W - pad.l - pad.r;
      var ih = H - pad.t - pad.b;

      var maxV = 0;
      points.forEach(function (p) { maxV = Math.max(maxV, p.added + p.removed); });
      var yMax = niceCeil(maxV);
      yGrid(svg, { pad: pad, iw: iw, ih: ih, yMin: 0, yMax: yMax });

      var n = points.length;
      var slot = iw / n;
      var bw = Math.max(1.5, Math.min(30, slot * 0.66));
      var baseY = pad.t + ih;

      points.forEach(function (p, i) {
        var total = p.added + p.removed;
        var x = pad.l + slot * i + (slot - bw) / 2;
        var hTotal = (total / yMax) * ih;
        var hAdd = total ? (p.added / total) * hTotal : 0;
        var hRem = hTotal - hAdd;
        var rx = bw > 4 ? 2 : 0;
        if (hAdd > 0.2) {
          svgEl("rect", { x: x, y: baseY - hAdd, width: bw, height: hAdd, fill: COL().added, rx: rx }, svg);
        }
        if (hRem > 0.2) {
          svgEl("rect", { x: x, y: baseY - hAdd - hRem, width: bw, height: hRem, fill: COL().removed, rx: rx }, svg);
        }
        var g = svgEl("g", null, svg);
        svgEl("rect", { x: pad.l + slot * i, y: pad.t, width: slot, height: ih, fill: "transparent" }, g);
        svgTip(g, bucketLabel(p.t, timeline.bucket) + "  ·  +" + fmtInt(p.added)
          + " / −" + fmtInt(p.removed) + " lines  ·  " + fmtInt(p.commits) + " commits");
      });

      var stride = Math.max(1, Math.ceil(n / 8));
      points.forEach(function (p, i) {
        if (i % stride === 0 || i === n - 1) {
          svgText(svg, pad.l + slot * i + slot / 2, H - 8, bucketLabel(p.t, timeline.bucket),
                  { "text-anchor": "middle", "font-size": 11 });
        }
      });
    });
  }

  /* ── area: cumulative net lines ───────────────────────────────── */

  function areaNet(el, timeline, opts) {
    opts = opts || {};
    var H = opts.height || 220;
    var pad = { l: 56, r: 14, t: 12, b: 24 };
    var gid = "rat-area-" + Math.random().toString(36).slice(2, 8);

    remember(el, function () {
      var points = pointsOf(timeline);
      if (!points.length) { emptyNote(el, "No commits in this commit set."); return; }
      clear(el);
      var svg = surface(el, H);
      var W = Number(svg.getAttribute("width"));
      var iw = W - pad.l - pad.r;
      var ih = H - pad.t - pad.b;

      var vals = [];
      var acc = 0;
      points.forEach(function (p) { acc += p.added - p.removed; vals.push(acc); });

      var lo = Math.min.apply(null, vals.concat([0]));
      var hi = Math.max.apply(null, vals.concat([0]));
      if (lo === hi) hi = lo + 1;
      var padV = Math.max(1, (hi - lo) * 0.08);
      var yMin = lo - (lo < 0 ? padV : 0);
      var yMax = hi + padV;

      function yOf(v) { return pad.t + ih - ih * ((v - yMin) / (yMax - yMin)); }
      function xOf(i) { return points.length === 1 ? pad.l + iw / 2 : pad.l + (iw * i) / (points.length - 1); }

      yGrid(svg, { pad: pad, iw: iw, ih: ih, yMin: yMin, yMax: yMax });

      var defs = svgEl("defs", null, svg);
      var grad = svgEl("linearGradient", { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
      svgEl("stop", { offset: "0%", "stop-color": COL().accent, "stop-opacity": 0.35 }, grad);
      svgEl("stop", { offset: "100%", "stop-color": COL().accent, "stop-opacity": 0.02 }, grad);

      var line = "";
      vals.forEach(function (v, i) {
        line += (i ? " L" : "M") + xOf(i).toFixed(1) + " " + yOf(v).toFixed(1);
      });
      svgEl("path", {
        d: line + " L" + xOf(vals.length - 1).toFixed(1) + " " + yOf(yMin).toFixed(1)
           + " L" + xOf(0).toFixed(1) + " " + yOf(yMin).toFixed(1) + " Z",
        fill: "url(#" + gid + ")", stroke: "none"
      }, svg);
      svgEl("path", { d: line, fill: "none", stroke: COL().accent, "stroke-width": 2.2 }, svg);

      if (points.length <= 90) {
        vals.forEach(function (v, i) {
          svgEl("circle", { cx: xOf(i), cy: yOf(v), r: 2.6, fill: COL().accent }, svg);
        });
      }

      points.forEach(function (p, i) {
        var w = points.length === 1 ? slotWidth(iw, points.length) : iw / Math.max(1, points.length - 1);
        var g = svgEl("g", null, svg);
        svgEl("rect", { x: xOf(i) - w / 2, y: pad.t, width: Math.max(4, w), height: ih, fill: "transparent" }, g);
        svgTip(g, bucketLabel(p.t, timeline.bucket) + "  ·  net "
          + (vals[i] >= 0 ? "+" : "−") + fmtInt(Math.abs(vals[i])) + " lines  ·  +"
          + fmtInt(p.added) + " / −" + fmtInt(p.removed));
      });

      /* final cumulative value */
      var last = vals[vals.length - 1];
      svgText(svg, pad.l + iw, pad.t + 12, "net " + (last >= 0 ? "+" : "−") + fmtInt(Math.abs(last)),
              { "text-anchor": "end", fill: last >= 0 ? COL().added : COL().removed, "font-size": 13, "font-weight": 600 });

      var stride = Math.max(1, Math.ceil(points.length / 8));
      points.forEach(function (p, i) {
        if (i % stride === 0 || i === points.length - 1) {
          svgText(svg, xOf(i), H - 7, bucketLabel(p.t, timeline.bucket),
                  { "text-anchor": "middle", "font-size": 11 });
        }
      });
    });
  }

  function slotWidth(iw, n) { return n ? iw / n : iw; }

  /* ── mini bars (drawer activity) ──────────────────────────────── */

  function miniBars(el, timeline, opts) {
    opts = opts || {};
    var H = opts.height || 120;
    var pad = { l: 10, r: 10, t: 10, b: 16 };

    remember(el, function () {
      var points = pointsOf(timeline);
      if (!points.length) { emptyNote(el, "No activity for this object."); return; }
      clear(el);
      var svg = surface(el, H);
      var W = Number(svg.getAttribute("width"));
      var iw = W - pad.l - pad.r;
      var ih = H - pad.t - pad.b;

      var maxV = 0;
      points.forEach(function (p) { maxV = Math.max(maxV, p.added + p.removed); });
      var yMax = niceCeil(maxV || 1);

      var n = points.length;
      var slot = iw / n;
      var bw = Math.max(1.5, Math.min(22, slot * 0.7));
      var baseY = pad.t + ih;

      points.forEach(function (p, i) {
        var total = p.added + p.removed;
        var h = (total / yMax) * ih;
        if (h < 0.2) return;
        var x = pad.l + slot * i + (slot - bw) / 2;
        var g = svgEl("g", null, svg);
        svgEl("rect", { x: x, y: baseY - h, width: bw, height: h, fill: COL().accent, rx: bw > 4 ? 2 : 0 }, g);
        svgTip(g, bucketLabel(p.t, timeline.bucket) + "  ·  +" + fmtInt(p.added)
          + " / −" + fmtInt(p.removed) + " lines");
      });

      var stride = Math.max(1, Math.ceil(n / 4));
      points.forEach(function (p, i) {
        if (i % stride === 0 || i === n - 1) {
          svgText(svg, pad.l + slot * i + slot / 2, H - 4, bucketLabel(p.t, timeline.bucket),
                  { "text-anchor": "middle", "font-size": 10 });
        }
      });
    });
  }

  /* ── donut: ownership share by author ─────────────────────────── */

  function donut(el, slices, opts) {
    opts = opts || {};
    var size = opts.size || 150;
    var stroke = 18;
    var r = (size - stroke) / 2 - 2;

    remember(el, function () {
      clear(el);
      var total = 0;
      (slices || []).forEach(function (s) { total += s.value || 0; });
      if (!total) { emptyNote(el, "No churn recorded."); return; }

      var svg = svgEl("svg", { width: size, height: size, viewBox: "0 0 " + size + " " + size }, el);
      var cx = size / 2, cy = size / 2;
      var circ = 2 * Math.PI * r;

      svgEl("circle", { cx: cx, cy: cy, r: r, fill: "none", stroke: COL().track, "stroke-width": stroke }, svg);

      var acc = 0;
      slices.forEach(function (s) {
        var frac = (s.value || 0) / total;
        if (frac <= 0) return;
        var arc = svgEl("circle", {
          cx: cx, cy: cy, r: r, fill: "none",
          stroke: s.color, "stroke-width": stroke,
          "stroke-dasharray": (frac * circ).toFixed(2) + " " + circ.toFixed(2),
          "stroke-dashoffset": (-acc * circ).toFixed(2),
          transform: "rotate(-90 " + cx + " " + cy + ")"
        }, svg);
        svgTip(arc, s.label + "  ·  " + (frac * 100).toFixed(1) + "%  ·  churn " + fmtInt(s.value));
        acc += frac;
      });

      svgText(svg, cx, cy - 2, fmtCompact(total), {
        "text-anchor": "middle", fill: COL().text, "font-size": 18, "font-weight": 700
      });
      svgText(svg, cx, cy + 14, "churn", { "text-anchor": "middle", "font-size": 11 });

      var legend = document.createElement("div");
      legend.className = "donut-legend";
      slices.forEach(function (s) {
        var frac = (s.value || 0) / total;
        var row = document.createElement("div");
        row.className = "dl-row";
        var dot = document.createElement("span");
        dot.className = "dl-dot";
        dot.style.background = s.color;
        var name = document.createElement("span");
        name.textContent = s.label;
        name.title = s.label;
        name.style.overflow = "hidden";
        name.style.textOverflow = "ellipsis";
        name.style.whiteSpace = "nowrap";
        name.style.maxWidth = "150px";
        var val = document.createElement("span");
        val.className = "dl-val";
        val.textContent = (frac * 100).toFixed(1) + "%";
        row.appendChild(dot); row.appendChild(name); row.appendChild(val);
        legend.appendChild(row);
      });
      el.appendChild(legend);
    });
  }

  /* ── palette helpers ──────────────────────────────────────────── */

  function palette(i) { return PALETTE[i % PALETTE.length]; }

  global.Charts = {
    stackedBars: stackedBars,
    areaNet: areaNet,
    miniBars: miniBars,
    donut: donut,
    palette: palette,
    otherColor: OTHER_COLOR,
    redraw: redraw,
    redrawAll: redrawAll,
    fmtInt: fmtInt,
    fmtCompact: fmtCompact
  };
})(window);
