---
name: predictops-triage
description: "Rank a factory fleet by failure risk using PredictOps' three independent signals (ML model, model-free deviation, ISO absolute limits) and surface the machines where those signals disagree. Use whenever the user asks which machine is at risk, what to fix first, what is about to fail, which machines need attention, or wants a fleet health / risk summary — even if they do not name this skill. Triggers: which machine is at risk, what should I fix first, fleet health, risk ranking, triage, what is failing, machines needing attention, signal disagreement, risk report."
---

# PredictOps: triage the fleet

Ranks machines worst-first and explains *why* each one is flagged, using three
signals that are computed independently:

| Signal | Basis | Fails when |
|---|---|---|
| **ML risk** | GradientBoosting model, 7-day failure horizon | the model was trained on synthetic data, so transfer to real equipment is unvalidated; it is also non-monotonic at extreme values |
| **Deviation** | plain arithmetic vs each machine's own reference | blind if every reading supplied was already degraded — the fault becomes the baseline |
| **Absolute limits** | published ISO 10816-1 vibration zones, lubricant temperature limits | no view of a machine degrading inside its allowed band |

They are reported separately rather than averaged. Averaging would hide exactly
the case that matters most: when they disagree, something is wrong with one of
them, and the user needs to know which.

## Prerequisites

- PredictOps backend running. Default `http://localhost:8000`, or the deployed
  origin if the user names one. Call this `BASE`.
- No authentication needed — every endpoint here is a read, and reads are open by
  design so dashboards cannot break.

Use `curl.exe`, not `curl`: in PowerShell 5.1 `curl` is an alias for
`Invoke-WebRequest` and will not accept these flags.

**Sending JSON on Windows:** pipe the body into curl and read it from stdin with
`--data-binary "@-"`. An inline `-d '{"question":"..."}'` is mangled by
PowerShell's quote handling and returns a 400 `VALIDATION_ERROR` — verified. On a
POSIX shell the inline form is fine. stdin also avoids leaving a temporary file
behind in what is a git repository.

## Workflow

### Step 1: Choose the scope

```bash
curl.exe -s BASE/api/datasets
```

- Empty list, or the user means the built-in fleet → omit `dataset_id`.
- One dataset → use it, and say which one you picked.
- Several → ask which fleet they mean rather than guessing.

### Step 2: Pull the report

```bash
curl.exe -s "BASE/api/report"
curl.exe -s "BASE/api/report?dataset_id=DSID"
```

One call returns everything needed: `scope`, `headline`, `data_quality`,
`machines` (already sorted worst-first), `assumptions`, `limitations`.

If this returns **404 `NO_MACHINES_IN_SCOPE`**, the dataset has no machines yet.
Point the user at `/predictops-ingest` instead of reporting an empty fleet.

### Step 3: Read data quality before reading any score

From `data_quality.overall`:

- `unusable` → **lead with this.** State that the rankings below should not be
  acted on until it is fixed, and say what failed. Do not bury it under the table.
- `warnings` → report the ranking, but name what reduces confidence.
- `ok` → proceed.

### Step 4: Present the ranking

Lead with `headline`: `machines_needing_attention`, `highest_risk_machine`,
`total_value_at_risk`, `signal_disagreements`, `machines_over_absolute_limits`,
`machines_without_ml_score`.

Then a table of the top machines. Show **both** numeric signals, never just one —
a machine at 7% ML risk and 95 deviation is a genuine concern that a single-column
table would make look safe:

| Machine | ML risk % | Deviation | Severity | Confidence | Why |
|---|---|---|---|---|---|

Fill "Why" from `failure_mode`, `top_factors` labels, and
`deviation_components`. Render a null `ml_risk_score` as `n/a`, not `0` — those
mean different things, and printing zero reads as "healthy".

### Step 5: Call out the three exception cases

These are the point of the skill; do not skip them when present.

1. **Signal disagreement** — any machine with `signal_disagreement` set. Quote its
   `message`, and state that the deviation score should be preferred, because the
   fitted model is known to be non-monotonic at extreme sensor values.

2. **Over published limits** — any machine with a non-empty `absolute_concerns`.
   For each, give `sensor`, `value`, `threshold`, `basis`. This is the strongest
   evidence available: it does not depend on the model or on a learned baseline.

3. **No ML score** — any machine where `ml_available` is false. Name the
   `missing_sensors` and say the assessment rests on deviation and absolute
   limits. Do not present this as a failure; it is the system declining to guess
   from substituted values.

### Step 6: Optional — ask the model a follow-up

For a narrative explanation of one machine:

```bash
'{"question":"why is <machine> at risk?"}' | curl.exe -s -X POST BASE/api/investigate `
  -H "Content-Type: application/json" `
  --data-binary "@-"
```

The response carries `answer`, `source` (`cortex` / `gemini` / `deterministic` /
`cached`), `intent`, and an `evidence` array. **Report `source`.** A
`deterministic` answer is rule-based rather than model-phrased — still correct, but
the user should know which they are reading. The `evidence` array is the
grounding; prefer quoting it over the prose if the two ever diverge.

This endpoint is rate-limited (default 30/minute) because it spends metered
third-party quota. On **429**, wait rather than retrying in a loop.

**Expect the first call to be slow.** With `LLM_PROVIDER=auto` the backend tries
Snowflake Cortex first, and when its cached token is stale Cortex attempts a
browser login that cannot complete headlessly — measured at ~20s before falling
back. Later calls are ~2s. If latency matters (a timed demo, for instance), either
re-prime Cortex once with `python -m app.llm.prime_cortex_login` from `backend/`,
or set `LLM_PROVIDER=none` to use the deterministic path directly. Steps 1–5 need
no LLM at all, so this step is genuinely optional.

### Step 7: State the limitations

Pass through the report's own `limitations` array, or summarise it honestly. The
two that change how the numbers should be read:

- risk scores come from a model trained on **synthetic** data — indicative, not
  proven for the user's equipment;
- every monetary figure is a **modelled estimate** from ERP cost-centre rates plus
  documented assumptions, not a measured saving.

Do not present `total_value_at_risk` as money saved.

## Troubleshooting

| Symptom | Meaning | What to do |
|---|---|---|
| connection refused | backend not running | say so; offer to start it |
| `404 NO_MACHINES_IN_SCOPE` | empty dataset | route to `/predictops-ingest` |
| every `ml_risk_score` is null | model artefact missing | check `ml_model` in `/api/health`; scoring has fallen back to rules |
| all deviations 0 and references look wrong | baselines not yet backfilled | happens on a cold first boot; re-check shortly |
| `429` from investigate | LLM rate limit | wait; the ranking itself needs no LLM |
| investigate hangs ~20s | Cortex retrying a browser login | re-prime Cortex, or set `LLM_PROVIDER=none` |
| `VALIDATION_ERROR` "Unterminated string" | PowerShell mangled an inline `-d` body | use the stdin form |

## Stopping Points

- ✋ Step 1 if several datasets exist and the user has not said which
- ✋ Step 3 if quality is `unusable` — report before ranking, and ask whether to
  continue

## Output

A triage summary containing:

- scope and data-quality verdict
- headline counts and highest-risk machine
- worst-first table showing **both** signals per machine
- explicit callouts for signal disagreement, absolute-limit breaches, and missing
  ML coverage
- stated limitations

Then offer `/predictops-workorder <machine>` to act on the top machine.
