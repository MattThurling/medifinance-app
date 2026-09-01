/*
 * Initialise Chart.js charts from server-rendered data.
 *
 * Each chart is a `<canvas data-chart data-chart-kind="bar|line|doughnut"
 * data-chart-src="<json_script id>">` next to a `{{ data|json_script }}`
 * block (see templates/_chart.html). The payload is produced by
 * crm/stats.py and looks like:
 *
 *   {"labels": [...], "datasets": [{"label": "Funded", "data": [...],
 *                                   "format": "money|int|percent"}]}
 *
 * Colours come from the DaisyUI theme's CSS variables so charts stay in
 * step with the rest of the UI; a hex palette is the fallback. Loaded with
 * `defer` after vendor/chart.umd.min.js, so `window.Chart` is available.
 */
(function () {
  "use strict";

  var FALLBACK = ["#2563eb", "#7c3aed", "#0891b2", "#059669", "#d97706", "#dc2626", "#64748b"];
  var THEME_VARS = [
    "--color-primary", "--color-secondary", "--color-accent",
    "--color-info", "--color-success", "--color-warning", "--color-error",
  ];

  function cssVar(name) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return v ? v.trim() : "";
  }

  // DaisyUI v5 defines its colours as oklch(...), which Chart.js's own colour
  // parser doesn't understand. Round-tripping through a 2D context's fillStyle
  // makes the browser serialise the colour to #rrggbb / rgba(), which Chart.js
  // does accept. Returns "" if the browser couldn't parse the value either.
  var probe = document.createElement("canvas").getContext("2d");
  function resolveColour(css) {
    if (!css || !probe) return "";
    probe.fillStyle = "#010203";
    probe.fillStyle = css;
    var out = probe.fillStyle;
    return out === "#010203" ? "" : out;
  }

  function themeColour(name) {
    return resolveColour(cssVar(name));
  }

  function paletteColour(i) {
    return themeColour(THEME_VARS[i % THEME_VARS.length]) || FALLBACK[i % FALLBACK.length];
  }

  var FORMATS = {
    money: function (v) {
      return "£" + Number(v).toLocaleString("en-GB", { maximumFractionDigits: 0 });
    },
    int: function (v) { return Number(v).toLocaleString("en-GB"); },
    percent: function (v) { return Number(v).toFixed(1) + "%"; },
  };

  function init(canvas) {
    var src = document.getElementById(canvas.dataset.chartSrc);
    if (!src || !window.Chart) return;
    var data = JSON.parse(src.textContent);
    var kind = canvas.dataset.chartKind || "bar";
    var circular = kind === "doughnut" || kind === "pie";

    data.datasets.forEach(function (ds, i) {
      if (circular) {
        // One colour per slice rather than per dataset.
        // A payload may name theme colours per slice (e.g. success/error for
        // won/lost); otherwise slices walk the palette.
        var names = ds.themeColors || [];
        ds.backgroundColor = ds.backgroundColor || ds.data.map(function (_, j) {
          return (names[j] && themeColour(names[j])) || paletteColour(j);
        });
        ds.borderWidth = 1;
      } else {
        var c = ds.color || paletteColour(i);
        ds.backgroundColor = ds.backgroundColor || c;
        ds.borderColor = ds.borderColor || c;
        if ((ds.type || kind) === "line") {
          ds.fill = false;
          ds.tension = 0.3;
        }
      }
    });

    var textColour = themeColour("--color-base-content") || "#1f2937";
    var primaryFormat = FORMATS[(data.datasets[0] || {}).format] || FORMATS.int;

    function fmtFor(ds) {
      return FORMATS[ds.format] || primaryFormat;
    }

    var options = {
      responsive: true,
      maintainAspectRatio: false,
      color: textColour,
      plugins: {
        legend: { display: circular || data.datasets.length > 1 },
        tooltip: {
          callbacks: {
            label: function (ctx) {
              var f = fmtFor(ctx.dataset);
              var raw = circular ? ctx.parsed : ctx.parsed.y;
              return (ctx.dataset.label ? ctx.dataset.label + ": " : "") + f(raw);
            },
          },
        },
      },
    };

    if (!circular) {
      options.scales = {
        x: { grid: { display: false }, ticks: { color: textColour } },
        y: {
          beginAtZero: true,
          ticks: { color: textColour, callback: function (v) { return primaryFormat(v); } },
        },
      };
      // A second axis for datasets flagged `yAxisID: "y2"` (e.g. commission
      // alongside funded), formatted by its own dataset's format.
      var second = data.datasets.filter(function (ds) { return ds.yAxisID === "y2"; })[0];
      if (second) {
        var f2 = fmtFor(second);
        options.scales.y2 = {
          position: "right",
          beginAtZero: true,
          grid: { drawOnChartArea: false },
          ticks: { color: textColour, callback: function (v) { return f2(v); } },
        };
      }
    }

    new Chart(canvas, { type: kind, data: data, options: options });
  }

  document.querySelectorAll("canvas[data-chart]").forEach(init);
})();
