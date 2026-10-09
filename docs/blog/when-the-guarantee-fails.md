# My prediction intervals were wrong 44% of the time. Here is what I did about it.

*Building honest uncertainty into OSS Radar, a daily open-source intelligence pipeline — conformal prediction under drift, and survival analysis for dependency risk.*

---

I build a project called [OSS Radar](https://radar.miladblog.com). Every day it ingests downloads, GitHub activity, vulnerabilities and security-health data for 91 Python and AI packages, then trains models to rank which ones are gaining momentum and which are becoming risky dependencies.

For months the dashboard showed a single number per package: *this one will grow 12% over the next 70 days.* A single number is a confident lie. I wanted a range, and I wanted the range to be **honest**.

So I added prediction intervals using conformal prediction, a method with a beautiful property: a mathematical guarantee that an "80% interval" contains the truth at least 80% of the time, for *any* model, with no assumptions about the data's distribution.

I ran it on my real data. An 80% interval covered **55.6%** of outcomes.

This post is about why a guarantee can fail without anything being broken, how I diagnosed it, what I replaced it with, and what I still can't promise. It's also about a second model — survival analysis — and a result I found uncomfortable to report.

## The guarantee, and the word it hides

Split conformal prediction is almost embarrassingly simple.

1. Train any forecaster `ŷ(x)`.
2. On held-out examples, compute how wrong it was: `sᵢ = |yᵢ − ŷ(xᵢ)|`.
3. Take the `⌈(n+1)(1−α)⌉`-th smallest score as `q̂`.
4. Predict `ŷ(x) ± q̂`.

If the held-out points and the future point are **exchangeable** — roughly, "the future looks like the past, in a statistical sense" — then the new point's score is equally likely to land in any of the `n+1` rank positions, so it falls at or below `q̂` with probability at least `1−α`. No distributional assumption. No dependence on the model. A genuinely remarkable result.

Everything rides on one word: *exchangeable*.

## Why mine wasn't

My data is 91 packages observed at 34 forecast-origin dates, 3,094 supervised rows. I calibrated on one chronological block and tested on the next. Coverage at an 80% target: **55.6%**. At 90%: 81.8%.

Nothing was buggy. The intervals were simply too narrow, and the reason was visible once I plotted the model's error by origin date:

| Origin date | Mean absolute error | Mean signed error |
|---|--:|--:|
| 2 Jul | 0.104 | −0.045 |
| 17 Jul | 0.161 | −0.119 |
| 29 Jul | 0.230 | −0.205 |

The error more than doubled in four weeks, and it was *biased*: the model kept predicting more growth than happened. Download growth slowed through the summer, and the model was trained on a faster regime.

So the residuals I calibrated on were systematically smaller than the ones I was about to make. The guarantee wasn't violated. Its assumption was false, and the method has no way to know.

This is the part I find most useful to say out loud: **a distribution-free guarantee is only as good as the assumption you can't test.** The only way I caught it was to evaluate on later dates than I calibrated on. If I had split the rows randomly, I'd have got a beautiful 80% and shipped a lie.

## Two standard remedies, combined

**Recency weighting** (Barber, Candès, Ramdas & Tibshirani, 2023). Instead of treating every calibration residual equally, weight the residual from origin date `d` by `2^(−(d*−d)/h)`, where `d*` is the newest calibration date and `h` is a half-life. Recent errors dominate, so the interval follows the current regime instead of the historical average. The quantile becomes a weighted quantile, with the unseen test point as a point mass at +∞ carrying the largest weight — the finite-sample correction.

**Adaptive Conformal Inference** (Gibbs & Candès, 2021). Treat the miscoverage level as a control variable and update it from what actually happened:

```
α_{t+1} = α_t + γ · (α − err_t)
```

If the last step missed more often than intended, `α_t` shrinks and the next interval widens. If it over-covered, it narrows. My version is a batch one: a "step" is a whole origin date, which resolves 91 packages at once, so `err_t` is a miss *rate*.

I evaluated by **forward-chaining**: every date is predicted using only strictly earlier dates for calibration and for the ACI state. No future information anywhere.

| Method | Coverage at 80% target | Coverage at 90% target |
|---|--:|--:|
| Plain split conformal, expanding window | 67.2% | 84.0% |
| + recency weighting | 70.3% | 86.3% |
| + ACI (γ = 0.1) | 73.9% | 87.0% |
| **+ ACI (γ = 0.2), shipped** | **76.6%** | **87.8%** |

That's a real improvement and **not a fix**. The intervals are about 21% wider and still fall short of the target.

## What I can't promise

I would rather put this in the post than in a footnote, because it's the difference between a result and a claim:

- **Seven scored dates.** After three warm-up dates, only seven origin dates could be scored. ACI's long-run guarantee needs many more steps.
- **Overlapping outcomes.** Origins are three days apart but outcome windows are 70 days, so rows share most of their outcomes. The effective sample is far smaller than 637.
- **The evaluation is lag-free.** It assumes yesterday's outcomes are known today. In production they take 70 days to resolve, so live calibration is staler and live coverage may be lower.
- **I chose γ on the same dates I report.** I compared a small grid and picked the best one. Treat 76.6% as indicative, not unbiased.

The real test is cheap: the intervals are stored with each prediction. Seventy days from now I can score them against what happened and publish the *realised* coverage. That's the number that matters.

## The second model: how long until the next advisory?

The project already had a risk classifier: given a package today, will something bad happen within 14 days? A 0/1 label has two problems. It throws away *when*. And it treats a package watched for 14 days the same as one watched for 110.

Survival analysis fixes both. I modelled the time until a package receives a **new security advisory** with a Cox proportional-hazards model:

```
λᵢ(t) = λ₀(t) · exp(xᵢ(t−1)ᵀ β)
```

Three design decisions mattered more than the algorithm:

1. **Calendar time as the clock.** Advisories arrive in bulk — on three separate days between late June and mid July, 21 to 26 packages each received new advisories at once, which looks like OSV ingesting a batch. With calendar time as the time axis, the baseline hazard `λ₀(t)` absorbs those shocks. They don't leak into the coefficients.
2. **Covariates measured the day before.** Features are as of `t−1`, predicting day `t`. Nothing from the outcome day gets in.
3. **Recurrent events, clustered errors.** A package can get many advisories, so it's an Andersen–Gill counting process, with standard errors clustered by package.

The data: 91 packages × 110 daily intervals = 10,010 package-days, 167 new-advisory events across 42 packages.

### I wrote the likelihood myself, then tried to break it

I implemented the Breslow partial likelihood in NumPy — Newton–Raphson, analytic gradient and Hessian — so I could show the mathematics and keep the production image lean. Then I tried to prove it wrong.

My first check, against `statsmodels`' Cox implementation, disagreed. The likelihoods differed by 121. I could have stopped there and trusted the library. Instead I computed the partial likelihood with plain Python loops. My implementation matched it exactly, and its gradient was zero at my solution. The library's fit sat at a point where the gradient was as large as 33 — it simply hadn't converged on this counting-process data.

Then a better check. The Breslow partial likelihood is *mathematically identical* to a Poisson regression with a free baseline for every event time. Fit that with a general-purpose GLM and compare:

```
max |β_mine − β_poisson|   = 1.8 × 10⁻¹⁴
max |SE_mine − SE_poisson| = 1.6 × 10⁻¹⁵
```

That test now runs in CI.

## What it found

Hazard ratios per standard deviation, with robust 95% intervals:

| Covariate | HR | 95% CI |
|---|--:|--:|
| Advisories already known (log) | **2.40** | 1.85 – 3.09 |
| Advisories in last 28 days (log) | 1.22 | 1.05 – 1.42 |
| Days since last release (log) | **0.75** | 0.61 – 0.94 |
| Monthly downloads, scorecard, bus factor | — | all include 1 |

Two things stand out.

**Past advisories dominate.** A package one standard deviation higher in advisory history gets new ones at 2.4× the rate. Security problems cluster.

**Staleness has the *opposite* sign from what I assumed.** My original risk heuristic gave release staleness a weight of 0.20, on the theory that abandoned packages are dangerous. The data say packages released *recently* receive new advisories *faster*. The likely reason is scrutiny, not safety: active projects have more eyes, more disclosure, more CVE assignment. But it's a reminder that "rate of new advisories" and "danger" aren't the same thing, and a heuristic can encode the wrong one.

## Does it beat what I had?

Train on the first 70 days, predict at weekly landmarks afterwards using only information available at each landmark:

| 14-day horizon | AUC | Brier | Mean predicted | Observed |
|---|--:|--:|--:|--:|
| **Cox survival** | 0.81 | **0.061** | 6.6% | 8.2% |
| Logistic, same covariates | 0.79 | 0.112 | 22.4% | 8.2% |
| Composite heuristic | 0.67 | — | — | 8.2% |
| "Known advisories" alone | 0.85 | — | — | 8.2% |
| No information (base rate) | 0.50 | 0.083 | — | 8.2% |

Read the logistic row carefully. Its ranking is nearly as good as Cox. But it predicts a 22% chance of an event that happens 8% of the time, and its Brier score is *worse than guessing the base rate*. It learned the event rate from the busy early period and couldn't separate it from the covariates. The survival model, with its time-varying baseline, predicts within two points of reality.

Now the uncomfortable row: **the single feature "how many advisories does it already have" ranks packages better than my whole model** — 0.85 against 0.81.

I could have left that row out. I put it in because it changes what the model is *for*. If you only need a ranking, sort by advisory history. What the survival model adds is **calibrated probabilities** — a figure like "24% chance of a new advisory in the next 30 days" that really means 24% — plus hazard ratios with confidence intervals, and correct handling of censoring. Those are different products from a ranking, and I'd rather describe the one I actually built.

## Testing the site, not just the math

A model nobody can see isn't much of a product, so the dashboard shows each package's interval and advisory probability. Behind it: more than 200 automated tests, including 22 headless-browser tests that drive the real page — every tab, filters, sorting, the package drawer, the audit form, and the mobile layout — against a seeded warehouse, on every pull request.

One of them caught a bug I'd introduced an hour earlier: my Cloudflare Worker proxy blocked all `POST` requests, which silently broke the dependency-audit form. Method allow-listing is great until you forget a method.

## What I'd do next

- **Publish realised coverage** once the first predictions mature, whatever it turns out to be.
- **Lag-aware calibration**: model the 70-day outcome delay explicitly instead of ignoring it.
- **Competing risks**: a package going stale and a package getting an advisory are different events and shouldn't be one label.

## The thing I'd keep

Not the model. The habit. Every result here came from a moment where the comfortable number was available and the honest one required a second look: a 55.6% that could have been a randomly-split 80%, a library disagreement I could have trusted, a one-feature baseline that out-ranked my model.

Uncertainty quantification is, in the end, a promise about what you don't know. The best thing I can do for anyone reading a forecast is to say how often I've been wrong.

---

*Code, derivations and tests: [github.com/MiladShd/oss-radar](https://github.com/MiladShd/oss-radar). The full conformal write-up is in `docs/CONFORMAL.md`. Live dashboard: [radar.miladblog.com](https://radar.miladblog.com).*

**References**
- Vovk, Gammerman & Shafer (2005). *Algorithmic Learning in a Random World.*
- Lei, G'Sell, Rinaldo, Tibshirani & Wasserman (2018). Distribution-free predictive inference for regression. *JASA.*
- Gibbs & Candès (2021). Adaptive conformal inference under distribution shift. *NeurIPS.*
- Barber, Candès, Ramdas & Tibshirani (2023). Conformal prediction beyond exchangeability. *Annals of Statistics.*
- Cox (1972). Regression models and life-tables. *JRSS B.*
- Andersen & Gill (1982). Cox's regression model for counting processes. *Annals of Statistics.*
