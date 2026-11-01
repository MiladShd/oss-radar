# Portfolio evidence report

The existing homepage, model views, statistics, and navigation remain intact at `/`. An added Research navigation link opens the separate `/evidence` page, which reports the scientific question, latest research diagnostic, conservative stored-forecast scorecard, evaluation method, observed findings, and operating health. The report follows the incumbent dark palette, fonts, brand, and components. `/explorer` remains an alias; `?view=accuracy` and `?view=system` deep-link into the application. The read-only `/api/brief` endpoint uses the existing warehouse and 60-second application cache.

## Evidence boundaries

- Research metrics come from the stored backtest. That backtest refits a model; it is not evaluation of the exact served artifact. The report explicitly identifies the overlapping outcome windows and absence of an independent temporal test.
- Calibration buckets show `expm1(mean log growth)`, not the arithmetic mean of percentage growth. Negative R² is presented as a failure of magnitude estimates against the evaluation-set mean, not as a failure of every ranking use case.
- A strict stored-forecast scorecard accepts only the known 70-day, trailing-28-day growth target lineage. It requires the original feature date, a prediction issued before the next UTC midnight, and all 167 raw daily observations needed for the 28-day smoother and the two 70-day sums. It never zero-fills missing days or refits a model. Forecasts issued after the outcome window starts are excluded and counted separately from missing data and unknown target definitions.
- If evidence is available, the scorecard selects the latest eligible origin and reports its package count, top-k overlap, mean absolute log error, and a zero-growth baseline on identical rows. One origin is descriptive, not independent repeated temporal validation. Repeated daily rows are not additional independent trials. Historical downloads may have been revised since issuance.
- A missing prospective scorecard is displayed as an evidence gap. It does not erase the separate research results.
- Observed download changes use complete adjacent seven-day raw windows, ending on the latest available download-history date no later than the snapshot. This date is shown explicitly. Automated installations are not user adoption.
- Security and maintenance findings use explicit source-availability checks. Recent package advisories are not assumed to affect installed versions. The exact-version form checks that separate question.

## Next scientific experiment

Freeze prediction issuance, forecast origin, input-data version, target definition, served artifact, and baseline predictions before the outcome window begins. After maturity, score model and baselines on the identical complete cohort, including failures and missingness. Repeat on multiple non-overlapping origin windows. Report uncertainty clustered by package and origin. No ROI, avoided incidents, or predictive accuracy percentage is claimed in advance.

## Verification

Unit tests cover missing-source safeguards, complete download windows, observed version changes, frozen forecast evaluation, late-issuance rejection, missing dates, and immature outcomes. API tests cover the evidence endpoint and retained explorer. Browser checks cover desktop and mobile rendering, chart readability, error state, exact-version form, and explorer deep links. Existing explorer browser tests continue against `/explorer`.
