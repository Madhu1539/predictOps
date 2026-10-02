---
name: predictops-ingest
description: "Load a factory sensor CSV into PredictOps: create a dataset, show how the columns were understood, commit the readings, then report data quality. Use this whenever the user wants to ingest, upload, load, or import sensor / machine / factory / equipment data, or asks to try PredictOps against their own data — even if they do not name this skill. Triggers: ingest CSV, upload sensor data, load factory data, import readings, bring my own data, try my own data, new dataset, add machines."
---

# PredictOps: ingest sensor data

Turns a CSV of sensor readings into a scored PredictOps dataset.

The CSV goes through a **read-only preview** before anything is written. Column
detection is heuristic — it matches on header names — and a misread column is far
cheaper to catch in a preview than to unpick after machines and readings exist.

## Prerequisites

- PredictOps backend running. Default `http://localhost:8000`; use the deployed
  origin instead if the user names one. Call this `BASE` below.
- A CSV with **at least one** of vibration, temperature, or RPM. Machine name and
  timestamp columns are optional but make the result much more useful.
- Upload is gated on the `reading:ingest` permission (`planner` or `technician`).
  If `AUTH_ENABLED=false` no token is needed — see Authentication.

Use `curl.exe`, not `curl`. In PowerShell 5.1 `curl` is an alias for
`Invoke-WebRequest`, which does not accept curl's flags and will fail confusingly.

**Sending JSON on Windows:** pipe the body into curl and read it from stdin with
`--data-binary "@-"`. An inline `-d '{"name":"x"}'` is mangled by PowerShell's
quote handling and arrives as invalid JSON — verified, it returns a 400
`VALIDATION_ERROR` with "Unterminated string". On a POSIX shell the inline form
is fine.

Use stdin rather than a temporary file. A file holding a password survives a
failed command, and this working directory is a git repository — a credential
written here is one `git add .` away from being committed.

## Workflow

### Step 1: Confirm the backend is up

```bash
curl.exe -s BASE/api/health
```

Read `database`, `ml_model`, and `machines` from the response. If `database` is
not `connected`, stop and report that — every later step will fail on it.

If the request itself fails, the backend is not running. Say so plainly and offer
to start it rather than retrying.

### Step 2: Get the file and a dataset name

Ask the user for the CSV path if they have not given one. Derive a sensible
dataset name from the filename and confirm it, rather than inventing one.

Read the first few lines so you can describe the file before uploading it:

```bash
Get-Content <csv-path> -TotalCount 3
```

If the file is larger than ~8 MB, note that `MAX_UPLOAD_BYTES` defaults to
8388608 and the request will be refused. Suggest splitting it.

### Step 3: Create the dataset

```bash
'{"name":"<dataset name>","source":"upload"}' | curl.exe -s -X POST BASE/api/datasets `
  -H "Content-Type: application/json" `
  --data-binary "@-"
```

Keep the returned `id` — every later call needs it. Call it `DSID`.

### Step 4: Preview the column mapping (writes nothing)

```bash
curl.exe -s -X POST BASE/api/datasets/DSID/preview `
  -H "Content-Type: text/csv" `
  --data-binary "@<csv-path>"
```

Report these fields back to the user in plain language:

| Field | What to say about it |
|---|---|
| `mapping` | which CSV header became which sensor field |
| `confidence` | how sure the detection is |
| `unmapped_headers` | columns that will be ignored entirely |
| `present_sensors` / `missing_sensors` | which channels were found |
| `ml_scoreable` | false means no ML risk score — deviation and ISO limits still apply |
| `detected_machines` | the machines about to be created |
| `timestamp_parse_failures` | those rows will be rejected |
| `temperature_looks_fahrenheit` | if true, Step 5 needs `convert_fahrenheit=true` |
| `notes` | already written for a human; pass them through |

**⚠️ STOP — do not upload yet.** Present the mapping and ask the user to confirm
it is right. If `ready_to_commit` is false, explain what is missing (no
recognisable sensor column) and stop; uploading would create machines with no
measurements.

### Step 5: Commit the readings

Only after the user confirms:

```bash
curl.exe -s -X POST "BASE/api/datasets/DSID/upload?machine_type=<type>" `
  -H "Content-Type: text/csv" `
  --data-binary "@<csv-path>"
```

Add `&convert_fahrenheit=true` if the preview flagged Fahrenheit **and** the user
agreed the readings are in Fahrenheit. Converting correct Celsius data silently
corrupts every downstream score, so do not assume it.

Set `machine_type` from what the user tells you (e.g. `Pump`, `CNC`, `Compressor`).
It defaults to `Unknown`, which weakens the confidence assessment because
type-specific reference values cannot be applied.

Report `machines_created`, `machines_matched`, `readings_accepted`,
`readings_rejected`. If anything was rejected, show the first few `errors` entries
with their row numbers — a partially malformed export still loads, so rejected
rows are normal and worth naming rather than hiding.

### Step 6: Check data quality

```bash
curl.exe -s BASE/api/datasets/DSID/quality
```

Report `overall` (`ok` / `warnings` / `unusable`), `guidance`, and the issues.

If `overall` is `unusable`, say directly that the risk scores should not be acted
on until the problem is fixed. A confident-looking score over bad data is worse
than no score.

## Authentication

If any mutating call returns **401**, auth is enabled and a token is needed:

```bash
'{"username":"planner","password":"<password>"}' | curl.exe -s -X POST BASE/api/auth/login `
  -H "Content-Type: application/json" `
  --data-binary "@-"
```

Take `token` from the response and add `-H "Authorization: Bearer <token>"` to
Steps 3, 4 and 5.

Ask the user for the password; do not guess it, and do not echo it back in your
reply. Never write it to a file — stdin keeps it out of the working directory,
which is a git repository. Note that the shell may still retain the command in
its history, so prefer a throwaway or demo credential here over a real one.

A **403** means the account lacks `reading:ingest` — `viewer` cannot ingest. Say
which role is needed rather than retrying.

## Troubleshooting

| Response | Meaning | What to do |
|---|---|---|
| `CSV_EMPTY` | no body, or no columns | check the path resolved to a real file |
| `FILE_NOT_UTF8` | wrong encoding | re-save as UTF-8 |
| `CSV_UNPARSEABLE` | pandas could not read it | inspect the first lines for a stray header or delimiter |
| `CSV_NO_SENSOR_COLUMNS` | no vibration / temperature / rpm found | rename a column so it is recognisable |
| `CSV_NO_VALID_ROWS` | every row failed validation | usually unparseable timestamps |
| `TOO_MANY_MACHINES` | over 200 distinct machine names | the file is probably pivoted the wrong way |
| `DATASET_NOT_FOUND` | wrong `DSID` | re-read the id from Step 3 |
| `VALIDATION_ERROR` with "Unterminated string" | PowerShell mangled an inline `-d` body | use the stdin form above |

## Stopping Points

- ✋ Step 4: mapping confirmed before any write
- ✋ Step 5: Fahrenheit conversion confirmed before applying it
- ✋ Any 401/403: ask for credentials or report the role gap; do not retry blindly

## Output

A committed dataset, reported as:

- dataset id and name
- machines created and matched, with their names
- readings accepted and rejected, with example rejections
- data quality verdict and what it implies

Then tell the user they can run `/predictops-triage` to rank the new fleet by risk.
