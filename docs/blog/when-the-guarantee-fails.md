# My "80% confidence" forecasts were right 58.5% of the time

*What I found when I made a dependency-risk dashboard honest about what it doesn't know.*

---

Teams make real decisions about open-source dependencies: which one to adopt, which one to replace, which one to patch this sprint. Most tools help with a number — a score, a rank, a grade — and almost none say **how much to trust it**.

I built [OSS Radar](https://radar.miladblog.com) to explore that gap. It's a personal project on public data: every day it pulls downloads, repository activity, vulnerabilities and security-health signals for 91 Python and AI packages, forecasts which ones are gaining momentum, and flags which are becoming risky to depend on.

This post is about two upgrades that made its numbers trustworthy, and — more usefully — the two places where my first attempt was wrong.

## The problem with a confident number

The dashboard originally said things like *"this package will grow 12% over 70 days."* One number, no range. If the real range is −20% to +45%, that precision is false, and a decision made on it is a gamble you didn't know you were taking.

The fix is a **prediction interval**: "12%, likely between −5% and +35%." I added one using a technique that comes with a nice promise — an "80% interval" should contain the real outcome 80% of the time, regardless of the model underneath.

I tested the promise on my own data. **It held 58.5% of the time.**

## Why the guarantee broke

Nothing was buggy. The technique assumes the future behaves like the past it was calibrated on, and mine didn't. The model's errors were getting bigger over time:

| Forecast date | Typical error (log-growth) | Bias (actual − predicted) |
|---|--:|--:|
| 2 Jul | 0.096 | −0.036 |
| 17 Jul | 0.157 | −0.120 |
| 29 Jul | 0.221 | −0.191 |

Typical error more than doubled in four weeks, and it leaned one way: the model kept expecting more growth than materialised. I can't say why with certainty — slower summer download growth is a plausible candidate, but I didn't isolate it. What matters is the consequence: ranges sized from older, smaller errors were too narrow for the errors coming next.

The lesson I'd offer any team shipping forecasts: **a confidence guarantee is only as good as an assumption you can't directly test.** I only caught this because I evaluated on dates *later* than the ones I calibrated on. A random split would have produced a flattering 80% and I'd have shipped it.

## The fix, in plain terms

Two changes, both standard in the research literature and simple to explain:

1. **Weight recent mistakes more.** When sizing the range, recent errors count for more than old ones, so the range follows current conditions.
2. **Self-correct.** If the last few ranges missed more often than promised, widen the next one; if they were too generous, tighten.

Evaluated the honest way — each date predicted using only earlier dates:

| Method | Covers (80% target) | Covers (90% target) | Range width (80%) |
|---|--:|--:|--:|
| Textbook method, expanding window | 67.0% | 84.3% | 0.336 |
| + weight recent errors | 71.4% | 86.2% | 0.368 |
| **+ self-correction (shipped)** | **75.8%** | **87.4%** | 0.410 |

That's a real improvement and **not a fix**: ranges are 22% wider and still short of target. Here is why I'm not claiming more:

- Only **7 dates** could be scored, which is thin for a method whose long-run guarantee needs many steps.
- Outcomes take 70 days to resolve, so in production the calibration is staler than in my test, and live coverage may be lower.
- I picked one setting by comparing a small grid on the same dates I report, so 75.8% is indicative, not an unbiased estimate.

The real test costs nothing: ranges are stored with each prediction, so once 70 days pass I can score them against reality and publish the *measured* coverage.

## Second upgrade: "how likely is a new security advisory soon?"

The existing risk model answered a yes/no question over a fixed 14 days. That throws away *when* things happen and treats a package watched for two weeks the same as one watched for 110 days.

I rebuilt that question as a **time-to-event model** (survival analysis, the same family used for customer churn): given what we know today, what's the chance a package receives a new security advisory in the next 14 or 30 days?

Three decisions made it trustworthy:

- **Real events, checked.** 168 new-advisory events across 91 packages. I confirmed the advisory count only goes up (one decrease in 10,000+ package-days), so these are genuine disclosures, not data noise.
- **Common shocks handled.** On three days in June and July, 21–26 packages each got new advisories at once, which looks like bulk publication. The model uses calendar time as its clock so those spikes don't get blamed on package features.
- **No peeking.** Every input is as of the day *before* the thing being predicted.

### It beat the obvious alternative where it counts

I compared it with the approach most teams would reach for: a standard classifier on the same inputs. Trained on the first 60–80 days, then forecasting later dates:

| 14-day forecast | Ranking skill (AUC) | Forecast error (Brier, lower is better) | Average predicted | Actually happened |
|---|--:|--:|--:|--:|
| **Survival model** | 0.82–0.84 | **0.054–0.061** | 6.6–7.3% | 7.7–8.2% |
| Standard classifier | 0.79–0.82 | 0.075–0.167 | 17–29% | 7.7–8.2% |
| Guess the average rate | 0.50 | 0.076–0.087 | — | — |

Look at the middle rows. The classifier ranks packages almost as well, but **tells you there's a 17–29% chance of something that happens about 8% of the time** — and in two of the three test windows it scores *worse than guessing the average* on forecast error. For prioritisation that's the difference between a number you can set thresholds on and a number you have to second-guess.

### The result I almost left out

One input — *how many advisories the package already has* — ranks packages **slightly better than the whole model** (0.85 vs 0.82–0.84). Past security trouble is the strongest signal by a distance: each standard deviation more history means about 2.3× the rate of new advisories.

I'm reporting that because it changes what the model is for. If you only need a ranking, sort by advisory history. What the model adds is a **calibrated probability** you can act on, with honest uncertainty. Today, for example, it puts 9 of the 91 packages at 50% or higher for a new advisory within 30 days and 20 at 25% or higher, while the median package is around 10%.

And one result that cuts against a common instinct: **recently released packages gain advisories *faster***, not slower. My original risk score penalised "stale" packages. The data point the other way. The likely explanation is that active projects attract more scrutiny and disclosure, so this measures advisory *arrival*, not danger — but it's a good reminder to check that a heuristic encodes what you think it does.

## What made this trustworthy

The parts of this work I'd defend in any engineering review:

- **Evaluation that can't flatter itself.** Every number above is from predicting dates later than the training dates.
- **Independent verification of the maths.** I wrote the model's core by hand, then proved it against an unrelated implementation: agreement to 14 decimal places. A separate library I tried first disagreed — I traced it with a brute-force check to that library failing to converge on this data, rather than trusting either.
- **Tests that match the product.** 211 automated tests, 23 of them driving the real dashboard in a headless browser against a seeded database, on every pull request.
- **Limits written down.** Both methods ship with a document stating what they don't show.
- **A boring, safe rollout.** Changes go through a pull request, automated checks, then a deploy that is tied to the exact commit; the new forecasts are informational and don't alter the existing risk score.

## Limits, and what's next

This is a personal project on about 110 days of public data. 168 events is enough to estimate a few effects, not dozens; test windows contain only 14–45 positive cases, so expect the AUCs to wobble by around ±0.05. It predicts when advisories are *published*, not whether a package is exploitable.

Next: publish the measured coverage once the first predictions mature, model the 70-day delay explicitly, and separate "gets an advisory" from "goes stale" as competing events.

---

*Code, derivations, evidence scripts and tests: [github.com/MiladShd/oss-radar](https://github.com/MiladShd/oss-radar) — see `docs/CONFORMAL.md` and `docs/SURVIVAL.md`. Live dashboard: [radar.miladblog.com](https://radar.miladblog.com).*

*Stack: Python, LightGBM, BigQuery, Cloud Run, Terraform, GitHub Actions, FastAPI, Playwright.*
