#!/usr/bin/env Rscript
# Reference values for Gray's K-sample test (Gray 1988, rho = 0) and Aalen-Johansen cumulative incidence from
# R cmprsk::cuminc. SPEC 6.6 O-STAT-3 (and an AJ-with-ties cross-check for O-STAT-2).
#
# Writes tests/fixtures/reference/gray_cmprsk.json (relative to the repo root = two levels above this script).
# Run from anywhere:  Rscript scripts/reference/gen_gray.R
# Inputs are small synthetic datasets defined here, drawn from a Park-Miller "minimal standard" LCG
# (x <- 16807 x mod (2^31 - 1)); every product is < 2^46, so the stream is exact in doubles and independent of
# R's RNG and version. Every input is echoed into the JSON, so tests never regenerate it.
# Numbers are written with 17 significant digits (jsonlite digits = I(17)); jsonlite's digits = NA keeps only 15.

suppressPackageStartupMessages({
  library(cmprsk)
  library(jsonlite)
})

args_all <- commandArgs(trailingOnly = FALSE)
script <- normalizePath(sub("^--file=", "", args_all[grep("^--file=", args_all)]))
root <- dirname(dirname(dirname(script)))
out_path <- file.path(root, "tests", "fixtures", "reference", "gray_cmprsk.json")

lcg <- function(seed, n) {
  s <- seed
  out <- numeric(n)
  for (i in seq_len(n)) {
    s <- (16807 * s) %% 2147483647
    out[i] <- s
  }
  out
}

# Cause from a uniform integer u in [0, 2^31 - 1) with group-specific probabilities (censor, cause 1, cause 2).
draw_cause <- function(u, probs) {
  x <- (u %% 1000) / 1000
  ifelse(x < probs[1], 0, ifelse(x < probs[1] + probs[2], 1, 2))
}

make_case <- function(name, seed, n, k, tie_free, tmax, probs_by_group, note) {
  u <- lcg(seed, 3 * n)
  group <- 1 + (u[seq(1, 3 * n, 3)] %% k)
  if (tie_free) {
    time <- u[seq(2, 3 * n, 3)] / 2147483647 * tmax
    stopifnot(!anyDuplicated(time))
  } else {
    time <- 1 + (u[seq(2, 3 * n, 3)] %% tmax)
  }
  cause <- numeric(n)
  for (g in seq_len(k)) {
    idx <- which(group == g)
    cause[idx] <- draw_cause(u[seq(3, 3 * n, 3)][idx], probs_by_group[[g]])
  }
  list(name = name, note = note, time = time, cause = cause, group = group)
}

cases <- list(
  list(
    name = "hand_n12_k2_ties",
    note = "hand-written; ties within and across groups, a censoring tied with an event at t = 3",
    time = c(1, 2, 2, 3, 3, 4, 5, 5, 6, 7, 8, 9),
    cause = c(1, 1, 2, 1, 0, 2, 1, 0, 1, 2, 0, 1),
    group = c(1, 2, 1, 1, 2, 2, 1, 2, 2, 1, 1, 2)
  ),
  make_case("ties_n60_k2", 11, 60, 2, FALSE, 20, list(c(0.3, 0.45, 0.25), c(0.3, 0.25, 0.45)),
            "integer times 1..20 (heavy ties), two arms with different cause mixes"),
  make_case("ties_n200_k3", 12345, 200, 3, FALSE, 40,
            list(c(0.2, 0.5, 0.3), c(0.25, 0.4, 0.35), c(0.3, 0.3, 0.4)),
            "integer times 1..40, three groups"),
  make_case("tiefree_n80_k2", 777, 80, 2, TRUE, 100, list(c(0.25, 0.5, 0.25), c(0.25, 0.35, 0.4)),
            "distinct real times (tie-free), two arms"),
  make_case("tiefree_n40_k2_null", 4242, 40, 2, TRUE, 10, list(c(0.3, 0.4, 0.3), c(0.3, 0.4, 0.3)),
            "tie-free, identical cause mix in both arms (null case)"),
  make_case("ties_n150_k4_heavycens", 98765, 150, 4, FALSE, 15,
            list(c(0.6, 0.3, 0.1), c(0.55, 0.2, 0.25), c(0.5, 0.35, 0.15), c(0.65, 0.15, 0.2)),
            "integer times 1..15, four groups, about 60% censored")
)

as_num <- function(x) unname(as.numeric(x))

run_case <- function(cs) {
  fit <- cuminc(ftime = cs$time, fstatus = cs$cause, group = cs$group, rho = 0, cencode = 0)
  tests <- fit$Tests
  test_rows <- lapply(rownames(tests), function(r) {
    list(cause = as.integer(r), stat = as_num(tests[r, "stat"]), pv = as_num(tests[r, "pv"]),
         df = as.integer(tests[r, "df"]))
  })
  grid <- sort(unique(cs$time))
  tp <- timepoints(fit, grid)
  curves <- lapply(rownames(tp$est), function(r) {
    parts <- strsplit(r, " ")[[1]]
    list(group = as.integer(parts[1]), cause = as.integer(parts[2]),
         est = as_num(tp$est[r, ]), var = as_num(tp$var[r, ]))
  })
  pooled <- cuminc(ftime = cs$time, fstatus = cs$cause, cencode = 0)
  tpp <- timepoints(pooled, grid)
  pooled_curves <- lapply(rownames(tpp$est), function(r) {
    parts <- strsplit(r, " ")[[1]]
    list(cause = as.integer(parts[2]), est = as_num(tpp$est[r, ]), var = as_num(tpp$var[r, ]))
  })
  list(
    name = cs$name,
    note = cs$note,
    input = list(time = as_num(cs$time), cause = as.integer(cs$cause), group = as.integer(cs$group)),
    gray_tests = test_rows,
    cif_grid = as_num(grid),
    cif_by_group = curves,
    cif_pooled = pooled_curves
  )
}

doc <- list(
  reference = "gray",
  oracle = list("O-STAT-3", "O-STAT-2"),
  generator = "scripts/reference/gen_gray.R",
  description = paste(
    "cmprsk::cuminc(ftime = time, fstatus = cause, group = group, rho = 0, cencode = 0).",
    "gray_tests: one row per cause with Gray's K-sample statistic (stat), p-value (pv) and df = K - 1;",
    "gray_test(time, cause, group, cause_of_interest = c) must equal the row with cause = c.",
    "cif_by_group / cif_pooled: Aalen-Johansen CIF (right-continuous, value at each cif_grid time,",
    "i.e. including jumps at that time) from cmprsk::timepoints, with cmprsk's variance estimate (informative only)."
  ),
  software = list(R = paste(R.version$major, R.version$minor, sep = "."),
                  cmprsk = as.character(packageVersion("cmprsk")),
                  survival = as.character(packageVersion("survival")),
                  jsonlite = as.character(packageVersion("jsonlite"))),
  cases = lapply(cases, run_case)
)

dir.create(dirname(out_path), recursive = TRUE, showWarnings = FALSE)
txt <- toJSON(doc, auto_unbox = TRUE, digits = I(17), pretty = 2, na = "null")
writeLines(txt, out_path, useBytes = TRUE)
cat("wrote", out_path, "\n")
