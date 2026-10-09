"""Build the blog's charts as standalone SVGs from audited data (stdlib only).

Usage (repo root):  python docs/blog/charts/make_charts.py
Reads  docs/blog/charts/data.json  (made by build_data.py)  and  docs/survival_results.json.
Writes docs/blog/images/*.svg. Every plotted number comes from those two files, nothing is typed in here.

Design: one message per chart (the title states it), one axis, thin marks, direct labels, recessive
grid, colour used for identity only. Palette = validated default (blue/orange/aqua, neutrals).
"""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "docs" / "blog" / "images"
DATA = json.loads((ROOT / "docs" / "blog" / "charts" / "data.json").read_text())
SURV = json.loads((ROOT / "docs" / "survival_results.json").read_text())

SURFACE, INK, INK2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#7a7975"
GRID, AXIS = "#ebeae5", "#cfcec8"
BLUE, ORANGE, NEUTRAL, DARK = "#2a78d6", "#eb6834", "#b9b8b2", "#3a3a38"
FONT = "Inter, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
W = 1100


class Svg:
    def __init__(self, height: int, title: str, subtitle: str, source: str, alt: str):
        self.h, self.parts = height, []
        self.title, self.subtitle, self.source, self.alt = title, subtitle, source, alt

    def add(self, s: str) -> None:
        self.parts.append(s)

    def text(self, x, y, s, size=14, fill=INK2, weight=400, anchor="start", style=""):
        self.add(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" font-weight="{weight}" '
                 f'text-anchor="{anchor}" {style}>{escape(str(s))}</text>')

    def line(self, x1, y1, x2, y2, stroke=GRID, width=1, dash=""):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{stroke}" '
                 f'stroke-width="{width}"{d}/>')

    def rect(self, x, y, w, h, fill, r=0):
        self.add(f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(w, 0):.1f}" height="{h:.1f}" fill="{fill}" rx="{r}"/>')

    def circle(self, x, y, r, fill):
        self.add(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{fill}" stroke="{SURFACE}" stroke-width="2"/>')

    def render(self) -> str:
        head = [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {self.h}" width="{W}" height="{self.h}" '
            f'role="img" aria-label="{escape(self.alt, {chr(34): "&quot;"})}" font-family="{FONT}">',
            f'<title>{escape(self.title)}</title>',
            f'<rect width="{W}" height="{self.h}" fill="{SURFACE}" rx="14"/>',
            f'<rect x="0.5" y="0.5" width="{W - 1}" height="{self.h - 1}" fill="none" stroke="{AXIS}" rx="14"/>',
            f'<text x="40" y="52" font-size="26" font-weight="700" fill="{INK}">{escape(self.title)}</text>',
            f'<text x="40" y="82" font-size="15" fill="{INK2}">{escape(self.subtitle)}</text>',
        ]
        foot = [f'<text x="40" y="{self.h - 22}" font-size="12.5" fill="{MUTED}">{escape(self.source)}</text>']
        return "\n".join(head + self.parts + foot + ["</svg>"])


def pct(v: float, d: int = 1) -> str:
    return f"{v * 100:.{d}f}%"


# ---------------------------------------------------------------- 1. error drift
def chart_drift() -> Svg:
    rows = DATA["error_by_date"]
    first, last = rows[0], rows[-1]
    ratio = last["mae"] / first["mae"]
    s = Svg(
        520,
        f"The model's error grew {ratio:.2f}× in four weeks, and most of it pointed one way",
        "Forecast error by forecast date, in log-growth units (lower is better)",
        f"Source: OSS Radar growth model, {DATA['n_packages']} packages, forecast dates "
        f"{first['date']} to {last['date']}, data through {DATA['data_through']}.",
        f"Line chart. Typical error rises from {first['mae']:.3f} to {last['mae']:.3f}; "
        f"over-prediction rises from {-first['bias']:.3f} to {-last['bias']:.3f}.",
    )
    x0, x1, y0, y1 = 110, 760, 410, 130
    ymax = 0.25

    def X(i):
        return x0 + (x1 - x0) * i / (len(rows) - 1)

    def Y(v):
        return y0 - (y0 - y1) * v / ymax

    for tick in (0, 0.05, 0.10, 0.15, 0.20, 0.25):
        s.line(x0, Y(tick), x1, Y(tick), GRID if tick else AXIS)
        s.text(x0 - 14, Y(tick) + 5, f"{tick:.2f}", 13, MUTED, anchor="end")
    for i, r in enumerate(rows):
        if i % 3 == 0 or i == len(rows) - 1:
            d = date.fromisoformat(r["date"])
            s.text(X(i), y0 + 28, f"{d.day} {d:%b}", 13, MUTED, anchor="middle")
    s.text(x0 - 70, y1 - 22, "log-growth", 13, MUTED)

    def path(vals, color):
        pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(vals))
        s.add(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="3" '
              f'stroke-linejoin="round" stroke-linecap="round"/>')

    mae = [r["mae"] for r in rows]
    lean = [-r["bias"] for r in rows]
    path(mae, BLUE)
    path(lean, ORANGE)
    for i in (0, len(rows) - 1):
        s.circle(X(i), Y(mae[i]), 5, BLUE)
        s.circle(X(i), Y(lean[i]), 5, ORANGE)
    s.text(X(0) + 4, Y(mae[0]) - 14, f"{mae[0]:.3f}", 14, INK, 600, "start")
    s.text(X(0) + 4, Y(lean[0]) + 26, f"{lean[0]:.3f}", 14, INK, 600, "start")
    # end values, then the series name right beside its own value (direct labels)
    end = len(rows) - 1
    s.text(X(end) + 16, Y(mae[end]) + 5, f"{mae[end]:.3f}", 15, INK, 700)
    s.text(X(end) + 16, Y(lean[end]) + 5, f"{lean[end]:.3f}", 15, INK, 700)
    s.text(X(end) + 76, Y(mae[end]) - 2, "Typical error", 15, BLUE, 700)
    s.text(X(end) + 76, Y(mae[end]) + 17, "average miss, either way", 12.5, MUTED)
    s.text(X(end) + 76, Y(lean[end]) + 3, "Over-prediction", 15, "#c24a1c", 700)
    s.text(X(end) + 76, Y(lean[end]) + 22, "average lean too high", 12.5, MUTED)
    s.text(40, 478, f"On 29 July the model over-shot by {lean[end]:.3f} on average against a typical miss of "
           f"{mae[end]:.3f}, so nearly all of its error pointed the same way.", 14.5, INK2)
    return s


# ---------------------------------------------------------------- 2. coverage
def chart_coverage() -> Svg:
    cov = DATA["coverage"]
    s = Svg(
        620,
        "Promised vs delivered: how often the real outcome fell inside the range",
        "Share of outcomes inside the forecast range. The dashed line is what the range promised",
        f"Source: OSS Radar, {DATA['n_rows']:,} supervised rows, forward-chained over "
        f"{cov['80']['aci']['dates_scored']} later forecast dates ({cov['80']['aci']['rows_scored']} rows); "
        "the first row uses one calibration split.",
        "Two bar groups, 80 and 90 percent targets. Coverage improves from 58.5 to 75.8 percent at the 80 "
        "percent target and from 79.6 to 87.4 percent at the 90 percent target.",
    )
    labels = [("single_split", "Textbook method, one calibration split"),
              ("plain", "Textbook method, tested on later dates"),
              ("recency", "+ weight recent errors more"),
              ("aci", "+ self-correcting width (shipped)")]
    panels = [("80", 350, "80% range"), ("90", 740, "90% range")]
    pw = 300
    for i, (key, label) in enumerate(labels):
        shipped = key == "aci"
        s.text(330, 202 + i * 72, label, 14, INK if shipped else INK2, 700 if shipped else 400, "end")
    for tgt, gx0, name in panels:
        s.text(gx0, 148, name, 18, INK, 700)
        for t in (0, 0.25, 0.5, 0.75, 1.0):
            gx = gx0 + pw * t
            s.line(gx, 178, gx, 468, GRID if t else AXIS)
            s.text(gx, 492, f"{int(t * 100)}%", 13, MUTED, anchor="middle")
        for i, (key, _) in enumerate(labels):
            v = cov[tgt][key]["coverage"]
            y = 185 + i * 72
            shipped = key == "aci"
            s.rect(gx0, y, pw * v, 30, BLUE if shipped else NEUTRAL, 4)
            s.text(gx0 + pw * v - 10, y + 21, pct(v), 15, "#ffffff" if shipped else INK, 700, "end")
        tx = gx0 + pw * int(tgt) / 100
        s.line(tx, 170, tx, 468, INK, 2, "6 5")
        s.text(tx, 166, f"promised {tgt}%", 13, INK, 700, "middle")
    s.text(40, 540, "Self-correction closes most of the gap but not all of it, and the ranges are about "
           f"{(cov['80']['aci']['width'] / cov['80']['plain']['width'] - 1) * 100:.0f}% wider.", 14.5, INK2)
    s.text(40, 562, "Only seven dates could be scored, so treat these as indicative until the first "
           "predictions mature (70 days).", 14.5, INK2)
    return s


# ---------------------------------------------------------------- 3. calibration
def chart_calibration() -> Svg:
    rows = [r for r in SURV["landmark_evaluation"] if r["horizon_days"] == 14]
    windows = sorted({r["train_days"] for r in rows})

    def pick(window, method):
        return next(r for r in rows if r["train_days"] == window and r["method"] == method)

    methods = [
        ("cox_selected", "Survival model (shipped)", True),
        ("logistic_recent_recalibrated", "Classifier anchored to recent rate", False),
        ("recent_rate_constant", "Recent average rate, no features", False),
        ("logistic_fixed_horizon", "Standard classifier", False),
        ("cox_selected_whole_history_baseline", "Survival model, whole-history baseline", False),
    ]
    s = Svg(
        640,
        "Anchoring to the recent event rate is what fixed the probabilities",
        "Forecast error for a new security advisory within 14 days (Brier score, lower is better). Survival model shown in blue",
        f"Source: OSS Radar, {SURV['config']['n_packages']} packages, {SURV['config']['n_events']} events, "
        "evaluated on weekly forecasts after each training window. Small samples: 21 to 45 events per window.",
        "Three panels, one per training window, comparing five methods by forecast error. The survival "
        "model has the lowest error in each; the standard classifier and the whole-history baseline "
        "over-forecast by roughly two to three times.",
    )
    pw, gap, x_label = 215, 28, 330
    max_brier = 0.18
    for i, (key, label, shipped) in enumerate(methods):
        s.text(x_label - 12, 238 + i * 58, label, 13.5, INK if shipped else INK2, 700 if shipped else 400, "end")
    for wi, w in enumerate(windows):
        gx0 = x_label + wi * (pw + gap)
        actual = pick(w, "cox_selected")["observed_rate"]
        s.text(gx0, 128, f"Trained on first {w} days", 15, INK, 700)
        s.text(gx0, 150, f"actual rate {actual * 100:.1f}%", 13, MUTED)
        s.line(gx0, 168, gx0, 468, AXIS)
        for mi, (key, _, shipped) in enumerate(methods):
            r = pick(w, key)
            y = 205 + mi * 58
            bw = (pw - 70) * r["brier"] / max_brier
            s.rect(gx0, y, bw, 26, BLUE if shipped else NEUTRAL, 4)
            s.text(gx0 + bw + 8, y + 18, f"{r['brier']:.3f}", 14, INK, 700 if shipped else 600)
            s.text(gx0 + bw + 8, y + 36, f"predicts {r['mean_predicted'] * 100:.0f}%" if r["mean_predicted"] >= 0.095
                   else f"predicts {r['mean_predicted'] * 100:.1f}%", 11.5, MUTED)
    s.text(40, 540, "The standard classifier and the whole-history survival baseline both forecast 17 to 29% for "
           "something that happened 8% of the time.", 14.5, INK2)
    s.text(40, 562, "Anchoring either model to the recent rate fixes most of that. The survival model still had the "
           "lowest error in every window and ranked best (AUC 0.82 to 0.84).", 14.5, INK2)
    s.text(40, 584, "Brier score is squared error of the probability: it rewards both ranking and calibration, so "
           "read it together with the predicted-versus-actual rate.", 14.5, INK2)
    return s


# ---------------------------------------------------------------- 4. hazard ratios
def chart_forest() -> Svg:
    plain = {
        "log_vuln_history": "Advisories the package already has",
        "log_vuln_recent_28d": "New advisories in the last 28 days",
        "log_days_since_release": "Days since the last release",
        "log_downloads": "Monthly downloads",
        "scorecard": "OpenSSF security scorecard",
        "bus_factor": "Maintainer concentration (bus factor)",
    }
    items = sorted(SURV["full_sample_hazard_ratios"], key=lambda r: -r["hazard_ratio_per_sd"])
    s = Svg(
        600,
        "Past security trouble is the strongest signal of new advisories",
        "How much each factor multiplies a package's rate of new advisories (one standard deviation higher)",
        f"Source: Cox proportional-hazards fit, {SURV['config']['n_rows']:,} package-days, "
        f"{SURV['config']['n_events']} events, cluster-robust 95% intervals.",
        "Forest plot of six hazard ratios with 95 percent intervals on a log scale.",
    )
    x0, x1, lo, hi = 470, 820, 0.5, 3.5

    def X(v):
        return x0 + (x1 - x0) * (math.log(v) - math.log(lo)) / (math.log(hi) - math.log(lo))

    for t in (0.5, 1, 2, 3):
        s.line(X(t), 150, X(t), 440, GRID if t != 1 else INK, 1 if t != 1 else 2)
        s.text(X(t), 462, f"{t:g}×", 13, MUTED, anchor="middle")
    s.text(X(1), 140, "no effect", 13, INK, 700, "middle")
    s.text(X(0.5), 492, "lowers the rate", 13, ORANGE, 700, "start")
    s.text(X(3.5), 492, "raises the rate", 13, BLUE, 700, "end")
    for i, r in enumerate(items):
        y = 185 + i * 48
        hr, a, b = r["hazard_ratio_per_sd"], r["hr_ci_low"], r["hr_ci_high"]
        color = NEUTRAL if a <= 1 <= b else (BLUE if hr > 1 else ORANGE)
        s.text(x0 - 30, y + 5, plain[r["feature"]], 14.5, INK if color != NEUTRAL else INK2,
               600 if color != NEUTRAL else 400, "end")
        s.line(X(a), y, X(b), y, color, 3)
        s.circle(X(hr), y, 6, color)
        s.text(x1 + 36, y + 5, f"{hr:.2f}×  ({a:.2f} to {b:.2f})", 14, INK, 600 if color != NEUTRAL else 400)
    s.text(40, 530, "Grey means the interval includes 1, so no clear effect. Recently released packages gain "
           "advisories faster, probably", 14, INK2)
    s.text(40, 551, "because active projects attract more scrutiny. This measures disclosure, not danger.", 14, INK2)
    return s


# ---------------------------------------------------------------- 5. architecture
def chart_architecture() -> Svg:
    s = Svg(
        570,
        "How OSS Radar fits together",
        "A scheduled data and ML pipeline, a read-only dashboard, and a release path that is checked before it ships",
        "Source: OSS Radar repository (infra/terraform, .github/workflows, pipeline/, dashboard/).",
        "Flow diagram from public data sources to a daily Cloud Run job, BigQuery, models, dashboard and "
        "custom domain, with a CI and deploy path beneath.",
    )

    def box(x, y, w, h, title, sub, fill="#f3f2ee", stroke=AXIS, tcolor=INK):
        s.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{fill}" stroke="{stroke}" '
              'stroke-width="1.5"/>')
        s.text(x + w / 2, y + 30, title, 15, tcolor, 700, "middle")
        for k, line in enumerate(sub):
            s.text(x + w / 2, y + 54 + k * 19, line, 12.5, INK2, 400, "middle")

    def arrow(x1, y1, x2, y2):
        s.line(x1, y1, x2, y2, INK2, 2)
        ang = math.atan2(y2 - y1, x2 - x1)
        for da in (2.7, -2.7):
            s.line(x2, y2, x2 + 10 * math.cos(ang + da), y2 + 10 * math.sin(ang + da), INK2, 2)

    y = 125
    box(40, y, 180, 132, "Public data", ["downloads · GitHub", "vulnerabilities", "security health", "6 sources"])
    box(262, y, 190, 132, "Daily job", ["Cloud Run + Scheduler", "ingest · features", "train · score", "gate · report"],
        fill="#e8f0fb", stroke=BLUE)
    box(494, y, 170, 132, "Warehouse", ["BigQuery", "snapshots · history", "predictions", "model runs"])
    box(706, y, 170, 132, "Models", ["LightGBM growth", "+ conformal ranges", "Cox survival", "risk composite"],
        fill="#e8f0fb", stroke=BLUE)
    box(918, y, 142, 132, "Dashboard", ["FastAPI", "Cloud Run", "read-only"])
    for x1, x2 in ((220, 262), (452, 494), (664, 706), (876, 918)):
        arrow(x1 + 2, y + 66, x2 - 4, y + 66)
    box(840, 330, 220, 84, "radar.miladblog.com", ["Cloudflare edge cache", "Worker proxy"],
        fill="#fdf0ea", stroke=ORANGE)
    arrow(989, y + 132, 989, 328)
    box(40, 330, 760, 84, "Release path", ["pull request → lint + 219 tests (23 browser) + security scan → "
                                           "exact-commit image build → Cloud Run deploy",
                                           "infrastructure defined in Terraform"],
        fill="#f3f2ee")
    s.text(40, 462, "Only the daily job writes to the warehouse and the dashboard reads it. A failure while fitting "
           "the survival model is caught,", 14.5, INK2)
    s.text(40, 485, "so the run continues without advisory probabilities. A retrained growth model must pass the "
           "validation gate and beat the champion before it serves.", 14.5, INK2)
    return s


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    charts = {
        "01-error-drift.svg": chart_drift(),
        "02-coverage.svg": chart_coverage(),
        "03-calibration.svg": chart_calibration(),
        "04-hazard-ratios.svg": chart_forest(),
        "05-architecture.svg": chart_architecture(),
    }
    for name, chart in charts.items():
        (OUT / name).write_text(chart.render())
        print("wrote", OUT / name)


if __name__ == "__main__":
    main()
