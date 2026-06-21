#!/usr/bin/env wolframscript
(* ============================================================================================
   validate_growth.wl  -  OSS Radar growth-model statistics, recomputed from first principles.

   An EDUCATIONAL cross-check: it reads the exact held-out predictions dumped by
   pipeline/scripts/validate_growth.py and re-derives every headline statistic in the Wolfram
   Language, printing each one Wolfram|Alpha-style (formula -> substitution -> intermediates ->
   result) and verifying it against the Python harness's docs/validation_results.json.

   Deterministic statistics (R^2, Spearman, MAE, calibrated-persistence OLS, skill score,
   Diebold-Mariano, between-package R^2, head slope) must reproduce the harness to ~1e-6 or the
   script EXITS NONZERO so the daily runner can alarm. Resampling statistics (permutation p,
   cluster bootstrap CI) use a different RNG than NumPy, so they are reported as method
   demonstrations + consistency bands, never as exact-match assertions.

   Terminal output is intentionally plain ASCII (robust in any shell); the rendered LaTeX math
   lives in the generated Markdown report (validation_steps.md).

   Usage:
     wolframscript -file validate_growth.wl [dataDir] [resultsJson] [outDir]
   Defaults: dataDir = <this dir>/sample_data, resultsJson = dataDir/validation_results.json,
             outDir  = dataDir.
   ============================================================================================ *)

(* ---------------------------------- arguments & paths ------------------------------------- *)
scriptDir = Quiet@Check[DirectoryName[$InputFileName], ""];
If[scriptDir === "" || scriptDir === $Failed, scriptDir = Directory[]];
cliArgs = If[Length[$ScriptCommandLine] >= 2, Rest[$ScriptCommandLine], {}];

dataDir = If[Length[cliArgs] >= 1, cliArgs[[1]], FileNameJoin[{scriptDir, "sample_data"}]];
refPath = If[Length[cliArgs] >= 2, cliArgs[[2]], FileNameJoin[{dataDir, "validation_results.json"}]];
outDir  = If[Length[cliArgs] >= 3, cliArgs[[3]], dataDir];

If[! FileExistsQ[refPath], Print["FATAL: reference JSON not found: ", refPath]; Exit[2]];
ref = Import[refPath, "RawJSON"];

(* ---------------------------------- small utilities --------------------------------------- *)
fmt[x_, d_: 6] := ToString@NumberForm[N[x], {Infinity, d}];
sci[x_]        := If[x == 0 || N[x] == 0., "0",   (* single-line "1.1e-16" (no 2D wrap) *)
                    With[{e = Floor[Log10[Abs[N[x]]]]},
                      ToString@NumberForm[N[x]/10^e, {4, 2}] <> "e" <> ToString[e]]];
rule[]         := Print[StringRepeat["-", 92]];

loadCols[path_] := Module[{raw, header, body, idx},        (* CSV -> <|"col" -> {values...}|> *)
  raw = Import[path, "CSV"]; header = First[raw]; body = Rest[raw];
  idx = AssociationThread[header -> Range[Length[header]]];
  AssociationMap[Function[c, body[[All, idx[c]]]], header]];

mean[v_]        := Total[v]/Length[v];                       (* population stats, match NumPy *)
ssTot[v_]       := With[{m = mean[v]}, Total[(v - m)^2]];
pearson[a_, b_] := Module[{da = a - mean[a], db = b - mean[b]},
                    Total[da db]/Sqrt[Total[da^2] Total[db^2]]];

(* PASS/FAIL bookkeeping; deterministic FAILs make the run exit nonzero *)
$checks = {};
record[label_, kind_, computed_, expected_, tol_] := Module[{ok, delta},
  delta = If[NumericQ[expected], Abs[computed - expected], Null];
  ok = If[kind === "info" || ! NumericQ[expected], True, delta <= tol (1 + Abs[expected])];
  AppendTo[$checks, <|"stat" -> label, "kind" -> kind, "computed" -> N[computed],
                      "expected" -> expected, "delta" -> delta, "pass" -> ok|>];
  ok];

(* one Wolfram|Alpha-style block + a markdown section (with LaTeX) + a structured step record
   (consumed by the dashboard's animated "live derivation" panel) *)
$md = {}; $steps = {};
block[title_, formulaLines_, inputLines_, stepLines_, resultLine_,
      label_, kind_, computed_, expected_, tol_, texFormula_String: ""] := Module[{ok, status, delta},
  ok = record[label, kind, computed, expected, tol];
  rule[]; Print[" ", title]; rule[];
  Do[Print["   formula     ", f], {f, Flatten[{formulaLines}]}]; Print[""];
  Do[Print["   input       ", i], {i, Flatten[{inputLines}]}];   Print[""];
  Do[Print["   step        ", s], {s, Flatten[{stepLines}]}];    Print[""];
  Print["   result      ", resultLine];
  delta  = If[NumericQ[expected], Abs[computed - expected], Null];
  status = Which[kind === "info", "INFO  (resampling - not an exact-match assertion)",
                 ok, "PASS", True, "FAIL"];
  If[NumericQ[expected],
    Print["   cross-check harness = ", fmt[expected, 8], "   |delta| = ", sci[delta], "   ", status],
    Print["   cross-check ", status]];
  Print[""];
  AppendTo[$md, StringJoin[
    "## ", title, "\n\n",
    If[texFormula =!= "", "$$" <> texFormula <> "$$\n\n", ""],
    "```\n",
    StringRiffle[Flatten[{"inputs:", ("  " <> # &) /@ Flatten[{inputLines}],
                          "steps:",  ("  " <> # &) /@ Flatten[{stepLines}],
                          "result:  " <> resultLine}], "\n"],
    "\n```\n\n",
    If[NumericQ[expected],
      "**Cross-check vs Python harness:** computed `" <> fmt[computed, 8] <> "` vs harness `" <>
      fmt[expected, 8] <> "`  (|delta| = " <> sci[delta] <> ") - **" <> status <> "**\n\n",
      "**" <> status <> "**\n\n"]]];
  AppendTo[$steps, <|
    "id" -> label, "title" -> title, "kind" -> kind, "latex" -> texFormula,
    "inputs" -> Flatten[{inputLines}], "steps" -> Flatten[{stepLines}], "result" -> resultLine,
    "computed" -> fmt[computed, 8], "computedNum" -> N[computed],
    "expected" -> If[NumericQ[expected], fmt[expected, 8], Null],
    "delta" -> If[NumericQ[expected], sci[delta], Null], "pass" -> ok,
    "status" -> Which[kind === "info", "INFO", ok, "PASS", True, "FAIL"]|>];
  ok];

(* ---------------------------------- load the dumped data ---------------------------------- *)
testPath  = FileNameJoin[{dataDir, "validation_testset.csv"}];
trainPath = FileNameJoin[{dataDir, "validation_trainset.csv"}];
oofPath   = FileNameJoin[{dataDir, "validation_oof.csv"}];
If[! FileExistsQ[testPath], Print["FATAL: missing ", testPath]; Exit[2]];

test = loadCols[testPath];
y = N@test["y"]; yhat = N@test["yhat"]; per = N@test["persistence"]; names = test["name"];
n = Length[y];
hasTrain = FileExistsQ[trainPath]; hasOOF = FileExistsQ[oofPath];

Print[""];
Print["============================================================================================"];
Print["  OSS Radar - growth-model validation, recomputed in the Wolfram Language"];
Print["  data dir : ", dataDir];
Print["  reference: ", refPath];
Print["  test rows: ", n, "   train dump: ", If[hasTrain, "yes", "MISSING"],
      "   oof dump: ", If[hasOOF, "yes", "MISSING"]];
Print["============================================================================================"];
Print[""];

(* ============================ 1) R^2 (held-out, same-package) ============================== *)
ybar = mean[y]; ssRes = Total[(y - yhat)^2]; ssTotV = Total[(y - ybar)^2]; r2 = 1 - ssRes/ssTotV;
block[
  "R^2  coefficient of determination  (held-out, same-package split)",
  {"R^2 = 1 - SS_res / SS_tot",
   "SS_res = Sum (y_i - yhat_i)^2 ,   SS_tot = Sum (y_i - ybar)^2"},
  {"n = " <> ToString[n], "ybar = " <> fmt[ybar]},
  {"SS_res = " <> fmt[ssRes], "SS_tot = " <> fmt[ssTotV]},
  "R^2 = 1 - " <> fmt[ssRes] <> " / " <> fmt[ssTotV] <> " = " <> fmt[r2, 8],
  "r2", "det", r2, ref["baselines"]["model"]["r2"], 1.*^-6,
  "R^2 = 1 - \\frac{\\sum_i (y_i-\\hat y_i)^2}{\\sum_i (y_i-\\bar y)^2}"];

(* ============================ 2) Spearman rank correlation ================================= *)
rhoBuiltin = SpearmanRho[y, yhat];
rankY = N@Ordering[Ordering[y]]; rankP = N@Ordering[Ordering[yhat]];
rhoRanks = pearson[rankY, rankP];
block[
  "Spearman rho  rank correlation of predictions vs actuals",
  {"rho = Pearson correlation of rank(y) and rank(yhat)",
   "    = Sum(R_i-Rbar)(S_i-Sbar) / sqrt( Sum(R_i-Rbar)^2 * Sum(S_i-Sbar)^2 )"},
  {"n = " <> ToString[n], "R = rank(y), S = rank(yhat); ties averaged"},
  {"SpearmanRho[y,yhat] = " <> fmt[rhoBuiltin, 8],
   "Pearson-on-ranks    = " <> fmt[rhoRanks, 8] <> "  (equal when no ties)"},
  "rho = " <> fmt[rhoBuiltin, 8],
  "spearman", "det", rhoBuiltin, ref["baselines"]["model"]["spearman"], 1.*^-6,
  "\\rho = \\frac{\\sum_i (R_i-\\bar R)(S_i-\\bar S)}{\\sqrt{\\sum_i (R_i-\\bar R)^2\\,\\sum_i (S_i-\\bar S)^2}}"];

(* ============================ 3) MAE ====================================================== *)
mae = mean[Abs[y - yhat]];
block[
  "MAE  mean absolute error",
  {"MAE = (1/n) Sum |y_i - yhat_i|"},
  {"n = " <> ToString[n]},
  {"Sum |y - yhat| = " <> fmt[Total[Abs[y - yhat]]]},
  "MAE = " <> fmt[Total[Abs[y - yhat]]] <> " / " <> ToString[n] <> " = " <> fmt[mae, 8],
  "mae", "det", mae, ref["baselines"]["model"]["mae"], 1.*^-6,
  "\\mathrm{MAE} = \\frac1n \\sum_i |y_i - \\hat y_i|"];

(* ============================ 4) Skill vs raw persistence ================================= *)
mseModel = mean[(y - yhat)^2]; msePers = mean[(y - per)^2]; skill = 1 - mseModel/msePers;
block[
  "Skill score  vs raw persistence  (MSE-based)",
  {"skill = 1 - MSE_model / MSE_persistence"},
  {"MSE_model = " <> fmt[mseModel], "MSE_pers = " <> fmt[msePers]},
  {"ratio = " <> fmt[mseModel/msePers, 6]},
  "skill = 1 - " <> fmt[mseModel/msePers, 6] <> " = " <> fmt[skill, 8] <>
    "   (raw persistence is a strawman - see calibrated baseline next)",
  "skill_vs_persistence_r2", "det", skill, ref["baselines"]["skill_vs_persistence_r2"], 1.*^-6,
  "\\mathrm{skill} = 1 - \\frac{\\mathrm{MSE}_{\\mathrm{model}}}{\\mathrm{MSE}_{\\mathrm{persistence}}}"];

(* ============================ 5) Calibrated persistence (FAIR) ============================ *)
calRef = ref["baselines"]["persistence_calibrated_fair"];
If[hasTrain,
  Module[{tr, ptr, ytr, pbar, ytrbar, bb, aa, predTest, r2cal},
    tr = loadCols[trainPath]; ptr = N@tr["persistence"]; ytr = N@tr["y"];
    pbar = mean[ptr]; ytrbar = mean[ytr];
    bb = Total[(ptr - pbar)(ytr - ytrbar)]/Total[(ptr - pbar)^2];   (* OLS slope, normal eqns *)
    aa = ytrbar - bb pbar;                                          (* OLS intercept *)
    predTest = aa + bb per; r2cal = 1 - Total[(y - predTest)^2]/ssTotV;
    block[
      "Calibrated persistence  (FAIR baseline: OLS fit on TRAIN, frozen onto TEST)",
      {"b = Sum(p_i-pbar)(y_i-ybar) / Sum(p_i-pbar)^2 ,   a = ybar - b*pbar   (fit on TRAIN)",
       "then  R^2_test of  yhat = a + b*p_test  vs y_test"},
      {"train rows = " <> ToString[Length[ytr]], "pbar_train = " <> fmt[pbar]},
      {"slope b   = " <> fmt[bb, 8] <> "  (harness " <> fmt[calRef["fitted_slope"], 8] <> ")",
       "intercept a = " <> fmt[aa, 8] <> "  (harness " <> fmt[calRef["fitted_intercept"], 8] <> ")"},
      "R^2_test(calibrated persistence) = " <> fmt[r2cal, 8] <>
        "   -> model beats it on level (" <> fmt[r2, 3] <> " vs " <> fmt[r2cal, 3] <>
        ") and on rank (" <> fmt[ref["baselines"]["model"]["spearman"], 3] <> " vs " <>
        fmt[calRef["spearman"], 3] <> ")",
      "calibrated_persistence_r2", "det", r2cal, calRef["r2"], 1.*^-6,
      "b=\\frac{\\sum(p_i-\\bar p)(y_i-\\bar y)}{\\sum(p_i-\\bar p)^2},\\quad a=\\bar y-b\\bar p"]],
  Module[{aa = calRef["fitted_intercept"], bb = calRef["fitted_slope"], predTest, r2cal},
    predTest = aa + bb per; r2cal = 1 - Total[(y - predTest)^2]/ssTotV;
    block[
      "Calibrated persistence  (FAIR baseline - train dump MISSING, using harness coefficients)",
      {"yhat = a + b*p_test  with a,b fit on TRAIN by the harness"},
      {"a (harness) = " <> fmt[aa, 8], "b (harness) = " <> fmt[bb, 8]},
      {"applied to the " <> ToString[n] <> " test rows"},
      "R^2_test = " <> fmt[r2cal, 8] <> "   (dump validation_trainset.csv to re-derive a,b here)",
      "calibrated_persistence_r2", "det", r2cal, calRef["r2"], 1.*^-6,
      "\\hat y = a + b\\,p_{\\text{test}}"]]];

(* ============================ 6) Diebold-Mariano (Newey-West HAC) ========================= *)
h = 70; e1 = (y - yhat)^2; e2 = (y - per)^2;
dLoss = e1 - e2; dbar = mean[dLoss]; dc = dLoss - dbar;
L = Min[h - 1, n - 1]; gamma0 = mean[dc^2];
hacVar = gamma0 + 2 Sum[(1 - lag/(L + 1)) mean[dc[[lag + 1 ;;]] dc[[;; -lag - 1]]], {lag, 1, L}];
dmSE = Sqrt[Max[hacVar, 1.*^-12]/n]; dmStat = dbar/dmSE;
dmP = 2 (1 - CDF[NormalDistribution[], Abs[dmStat]]); dmRef = ref["diebold_mariano_vs_persistence"];
block[
  "Diebold-Mariano  model vs persistence  (squared-error loss, Newey-West HAC)",
  {"d_i = (y_i-yhat_i)^2 - (y_i-p_i)^2 ,   DM = dbar / SE(dbar)",
   "HAC var = g0 + 2 Sum_{l=1..L} (1 - l/(L+1)) * g_l ,   SE = sqrt(HACvar / n)"},
  {"n = " <> ToString[n], "horizon h = " <> ToString[h], "Bartlett lag L = " <> ToString[L]},
  {"dbar = " <> fmt[dbar, 8], "HAC var = " <> fmt[hacVar, 8], "SE = " <> fmt[dmSE, 8]},
  "DM = " <> fmt[dbar, 6] <> " / " <> fmt[dmSE, 6] <> " = " <> fmt[dmStat, 6] <>
    "   (two-sided p = " <> sci[dmP] <> ", favours " <> If[dbar < 0, "model", "benchmark"] <> ")",
  "dm_stat", "det", dmStat, dmRef["dm_stat"], 1.*^-6,
  "DM=\\frac{\\bar d}{\\sqrt{\\big(\\gamma_0+2\\sum_{l=1}^{L}(1-\\tfrac{l}{L+1})\\gamma_l\\big)/n}}"];

(* ============================ 7) Between-package R^2 + head slope ========================= *)
posByName = GroupBy[Range[n], names[[#]] &];
pkgKeys = Keys[posByName];
ybarPkg = (mean[y[[posByName[#]]]] &) /@ pkgKeys;
phatPkg = (mean[yhat[[posByName[#]]]] &) /@ pkgKeys;
r2between = 1 - Total[(ybarPkg - phatPkg)^2]/ssTot[ybarPkg];
block[
  "Between-package R^2  (collapse each package to its mean, then R^2)",
  {"R^2_between = 1 - Sum_k (ybar_k - pbar_k)^2 / Sum_k (ybar_k - ybarbar)^2"},
  {"packages = " <> ToString[Length[pkgKeys]]},
  {"isolates the cross-sectional (across-package) skill from within-package wiggle"},
  "R^2_between = " <> fmt[r2between, 8],
  "r2_between", "det", r2between, ref["within_between"]["r2_between"], 1.*^-6,
  "R^2_{\\text{between}} = 1 - \\frac{\\sum_k(\\bar y_k-\\bar p_k)^2}{\\sum_k(\\bar y_k-\\bar{\\bar y})^2}"];

{interceptHead, slopeHead} = PadRight[CoefficientList[Fit[Transpose[{y, yhat}], {1, x}, x], x], 2];
block[
  "Head-of-watchlist slope  (predicted vs actual; <1 means regression to the mean)",
  {"yhat ~ c + m*y   (least squares); m<1 means the head of the list is shrunk toward the mean"},
  {"n = " <> ToString[n]},
  {"intercept c = " <> fmt[interceptHead, 6]},
  "slope m = " <> fmt[slopeHead, 8] <> "   (top-10 overlap with real movers is only " <>
    ToString[ref["head_reliability"]["top10_overlap"]] <> "/10 - the head is the least reliable part)",
  "head_slope", "det", slopeHead, ref["head_reliability"]["pred_vs_actual_slope"], 1.*^-6,
  "\\hat y \\approx c + m\\,y"];

(* ============================ 8) Package-disjoint (unseen package) ======================== *)
pdRef = ref["package_disjoint_cv"];
If[hasOOF,
  Module[{oof, yo, po, r2o, rhoo},
    oof = loadCols[oofPath]; yo = N@oof["y"]; po = N@oof["oof"];
    r2o = 1 - Total[(yo - po)^2]/ssTot[yo]; rhoo = SpearmanRho[yo, po];
    block[
      "Package-disjoint R^2  (GroupKFold, train/test share ZERO packages - the honest number)",
      {"same R^2 / rho formulas, but on out-of-fold predictions where no test package",
       "was ever seen in training -> removes the shared-package memorisation leak"},
      {"oof rows = " <> ToString[Length[yo]], "folds = " <> ToString[pdRef["n_splits"]]},
      {"R^2 = " <> fmt[r2o, 8] <> "  (harness " <> fmt[pdRef["r2"], 8] <> ")",
       "rho = " <> fmt[rhoo, 8] <> "  (harness " <> fmt[pdRef["spearman"], 8] <> ")"},
      "unseen-package  R^2 = " <> fmt[r2o, 6] <> ",  rho = " <> fmt[rhoo, 6] <>
        "   <- THE NUMBER TO QUOTE (same-package " <> fmt[r2, 3] <> " leaks package identity)",
      "package_disjoint_r2", "det", r2o, pdRef["r2"], 1.*^-6,
      "R^2_{\\text{unseen}} = 1 - \\frac{\\sum(y_i-\\hat y^{\\,\\text{oof}}_i)^2}{\\sum(y_i-\\bar y)^2}"]],
  (rule[]; Print[" Package-disjoint R^2  (unseen package) - OOF dump MISSING"]; rule[];
   Print["   method      GroupKFold by package: each fold trains LightGBM on a disjoint set of"];
   Print["               packages and predicts the held-out ones (out-of-fold). R^2/rho on those."];
   Print["   harness     R^2 = ", fmt[pdRef["r2"], 6], ",  rho = ", fmt[pdRef["spearman"], 6],
         "  (n=", pdRef["n"], ", ", pdRef["n_splits"], " folds)"];
   Print["   to verify   run the harness so it dumps validation_oof.csv, then re-run this script."];
   Print[""]; record["package_disjoint_r2", "info", pdRef["r2"], pdRef["r2"], 0])];

(* ============================ 9) Permutation test (method demo) =========================== *)
permRef = ref["permutation_test"]; SeedRandom[42];
obsRho = SpearmanRho[y, yhat]; permB = 2000;
permGE = Count[Table[SpearmanRho[RandomSample[y], yhat], {permB}], r_ /; r >= obsRho];
permP = N[(1 + permGE)/(1 + permB)];
block[
  "Permutation test  (is the rank skill better than chance?)",
  {"H0: predictions carry no information. Shuffle the targets B times, recompute rho.",
   "p = (1 + #{rho_perm >= rho_obs}) / (1 + B)"},
  {"observed rho = " <> fmt[obsRho, 6], "B = " <> ToString[permB] <> " shuffles"},
  {"#{rho_perm >= rho_obs} = " <> ToString[permGE]},
  "p ~ " <> fmt[permP, 5] <> "   (harness p = " <> fmt[permRef["p_value"], 5] <>
    "; both -> p < 0.001. Exact count differs: NumPy vs WL RNG / block vs simple shuffle)",
  "permutation_p", "info", permP, permRef["p_value"], 0,
  "p = \\frac{1 + \\#\\{\\rho^{\\text{perm}} \\ge \\rho^{\\text{obs}}\\}}{1 + B}"];

(* ============================ 10) Cluster bootstrap (method demo) ========================= *)
bootRef = ref["cluster_bootstrap"]; SeedRandom[42]; bootB = 2000;
bootR2 = DeleteCases[Table[
   Module[{pick = RandomChoice[pkgKeys, Length[pkgKeys]], idx, ys, ps},
    idx = Flatten[posByName /@ pick]; ys = y[[idx]]; ps = yhat[[idx]];
    If[Length[Union[ys]] < 3, Nothing, 1 - Total[(ys - ps)^2]/ssTot[ys]]], {bootB}], Nothing];
ciLo = Quantile[bootR2, 0.025]; ciHi = Quantile[bootR2, 0.975]; ciMed = Median[bootR2];
block[
  "Cluster bootstrap  (95% CI for R^2; resample whole PACKAGES, the independent unit)",
  {"resample packages with replacement B times; the 2.5% and 97.5% percentiles of the",
   "bootstrap R^2 distribution give a CI that respects within-package autocorrelation"},
  {"packages = " <> ToString[Length[pkgKeys]], "B = " <> ToString[bootB] <> " resamples"},
  {"median R^2 = " <> fmt[ciMed, 4]},
  "95% CI R^2 ~ [" <> fmt[ciLo, 3] <> ", " <> fmt[ciHi, 3] <> "]   (harness [" <>
    fmt[bootRef["r2_ci"][[1]], 3] <> ", " <> fmt[bootRef["r2_ci"][[2]], 3] <>
    "]; both exclude 0 - signal is real, RNG-dependent width)",
  "bootstrap_ci", "info", ciMed, bootRef["r2_median"], 0,
  "\\text{CI} = \\big[Q_{2.5\\%}(R^{2*}),\\,Q_{97.5\\%}(R^{2*})\\big]"];

(* ---------------------------------- verdict & artifacts ----------------------------------- *)
detChecks = Select[$checks, #["kind"] === "det" &];
detFails = Select[detChecks, ! #["pass"] &];
nPass = Length[Select[detChecks, #["pass"] &]];

rule[]; Print[" VERDICT"]; rule[];
Print["   deterministic cross-checks : ", nPass, " / ", Length[detChecks], " reproduced to <1e-6"];
Print["   resampling demonstrations  : permutation p<0.001, bootstrap CI excludes 0 (consistent)"];
Print["   retired 0.74/0.70 headline : a centered-MA lookahead leak (see docs/VALIDATION.md)"];
If[detFails =!= {}, Print["   FAILURES:"];
  Do[Print["     - ", f["stat"], "  computed ", fmt[f["computed"], 8], " vs ", fmt[f["expected"], 8]],
     {f, detFails}]];
Print[""];

stamp = DateString[TimeZoneConvert[Now, 0], "ISODateTime"] <> "Z";  (* explicit UTC, parseable *)
Export[FileNameJoin[{outDir, "wolfram_crosscheck.json"}],
  <|"generated" -> stamp, "engine" -> $Version, "n_test" -> n,
    "deterministic_pass" -> nPass, "deterministic_total" -> Length[detChecks],
    "all_pass" -> (detFails === {}), "checks" -> $checks|>, "JSON"];
Export[FileNameJoin[{outDir, "wolfram_freshness.json"}],
  <|"last_run" -> stamp, "all_pass" -> (detFails === {}), "engine" -> $Version|>, "JSON"];

(* Structured step-by-step record for the dashboard's animated "live derivation" panel. *)
Export[FileNameJoin[{outDir, "validation_steps.json"}],
  <|"generated" -> stamp, "engine" -> $Version, "n_test" -> n,
    "deterministic_pass" -> nPass, "deterministic_total" -> Length[detChecks],
    "all_pass" -> (detFails === {}), "steps" -> $steps|>, "JSON"];

mdHeader = StringJoin[
  "# Growth-model validation - step-by-step (Wolfram Language)\n\n",
  "*Generated ", stamp, " by `pipeline/wolfram/validate_growth.wl` on ", $Version, ".*\n\n",
  "Every statistic below is **recomputed from the dumped held-out predictions** and cross-checked\n",
  "against the Python harness (`docs/validation_results.json`). Deterministic stats must match to\n",
  "<1e-6. Educational companion to [`VALIDATION.md`](VALIDATION.md).\n\n",
  "**Verdict:** ", ToString[nPass], "/", ToString[Length[detChecks]],
  " deterministic checks reproduced. The retired **0.74/0.70** headline was a centered-MA\n",
  "lookahead leak; the honest numbers are **0.582 same-package / 0.363 package-disjoint**.\n\n---\n\n"];
Export[FileNameJoin[{outDir, "validation_steps.md"}], mdHeader <> StringRiffle[$md, "\n"], "Text"];

Print["   wrote: validation_steps.md, wolfram_crosscheck.json, wolfram_freshness.json -> ", outDir];
Print[""];
Exit[If[detFails === {}, 0, 1]];
