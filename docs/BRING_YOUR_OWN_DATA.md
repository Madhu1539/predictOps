# Bring Your Own Data

Upload a CSV of real sensor readings and get a report on your machines. Your data
runs through the **same pipeline** as the built-in demo fleet — the same feature
engineering, the same model, the same scoring, the same report.

A sample file is included at [`docs/sample_factory_data.csv`](sample_factory_data.csv):
one healthy pump and one visibly degrading CNC machine. For the far more common
single-channel case, [`docs/sample_vibration_only.csv`](sample_vibration_only.csv)
is the shape of a bearing run-to-failure dataset: one degrading bearing, one healthy,
vibration only.

[`docs/sample_factory_upload.csv`](sample_factory_upload.csv) is the larger one to
reach for when you want to see the whole path work: four machines at 48 hourly
readings each, so every rolling feature is populated rather than warned about. Its
headers are deliberately a third party's (`Asset`, `Vibration_mm_s (RMS)`,
`Bearing_Temp [C]`, `Shaft Speed [RPM]`), it carries a `Plant_Area` column nothing
maps to, and `CONV-03` runs on a fixed-setpoint drive whose RPM never varies — which
is reported as a warning, not the error a dead vibration channel would be.

---

## The guarantee that matters

The demo runs a simulator that fabricates sensor readings. **It is never permitted
to write to uploaded data.**

`Machine.data_origin` is `simulated` for the demo fleet and `external` for anything
you upload, and the simulator's query filters on it:

```python
# app/services/live_feed.py — CORRECTNESS BOUNDARY
result = await db.execute(
    select(Machine).where(Machine.data_origin == ORIGIN_SIMULATED)
)
```

Extending a real factory's history with invented values would be indistinguishable
from falsifying their data, so the boundary is enforced in one place and covered by
a test that runs the simulator and asserts nothing was added:

```
tests/test_unified_pipeline.py::test_simulator_never_writes_to_external_machines
```

Every reading in your report is yours.

---

## Accepted formats

### Required columns

**At least one** of vibration, temperature or RPM. Not all three.

Real plants are rarely fully instrumented, and public datasets almost never carry
all three together — a bearing rig logs vibration alone, a compressor logs oil
temperature. Demanding the full set would reject most genuine data, so partial
instrumentation is a first-class case:

| What you supply | What you get |
|---|---|
| All three channels | ML risk score + deviation + absolute limits |
| One or two channels | **Deviation + absolute limits.** ML risk reads `n/a` |
| No sensor channels | Rejected — there is nothing to assess |

When a channel is missing, the ML score is reported as **unavailable, never
estimated**. The model was fitted on all three together, so feeding it a substitute
for a channel that does not exist would produce a confident number with nothing
behind it. A missing value is stored as NULL, not as a zero or an average.

This is not a degraded mode in practice. A vibration-only bearing dataset is still
triaged correctly: in testing, a bearing degrading from 0.62 to 9.67 mm/s was
flagged **Critical** by ISO 10816-1 zone D, while a steady one at 0.6 mm/s stayed
**Normal** — from a single channel, with no ML score at all.

### Recognised automatically

Column names do not have to match ours. These are all understood:

| Field | Examples that work |
|---|---|
| machine | `machine`, `machine_id`, `asset`, `asset_id`, `equipment`, `tag`, `unit`, `device` |
| timestamp | `timestamp`, `time`, `datetime`, `date`, `reading_time`, `recorded_at`, `ts` |
| vibration | `vibration`, `vibration_mm_s`, `vibration_rms`, `vib`, `rms_velocity` |
| temperature | `temperature`, `temp`, `bearing_temp`, `motor_temp`, `temperature_c` |
| rpm | `rpm`, `speed`, `speed_rpm`, `shaft_speed`, `rotational_speed` |
| production_count | `production_count`, `units_produced`, `output`, `total_count` |
| good_count | `good_count`, `good_units`, `pass_count`, `accepted_units` |
| machine_status | `status`, `state`, `machine_status`, `run_status` |

Units in the header are ignored, so `Vibration_mm_s (RMS)` and `Shaft Speed [RPM]`
both map correctly.

Anything unrecognised is listed as ignored rather than silently consumed.

### Timestamps

ISO (`2026-03-01T08:30:00`), space-separated (`2026-03-01 08:30`), `DD/MM/YYYY`,
`MM/DD/YYYY`, date-only, and epoch seconds or milliseconds. A value matching no
known format is **rejected with its line number** rather than guessed at.

### Units

Fahrenheit is detected from the column name (`Bearing_Temp_F`) and offered as a
conversion. It is never applied without you opting in — silently rescaling your
data would be worse than asking.

---

## How to use it

### In the UI

1. Open **Bring Your Own Data** in the sidebar
2. Name the dataset and select your CSV
3. **Check mapping** — shows how your headers were read. Nothing is saved yet
4. Confirm, then **Import and analyse**
5. Read the report; **Download report (HTML)** keeps a self-contained copy

**Delete this dataset** removes your machines, readings and alerts, and leaves the
demo fleet untouched.

### Via the API

```bash
# 1. Create a dataset
curl -X POST localhost:8000/api/datasets \
  -H 'Content-Type: application/json' \
  -d '{"name":"My Factory"}'

# 2. Preview the mapping — writes nothing
curl -X POST localhost:8000/api/datasets/1/preview \
  -H 'Content-Type: text/csv' \
  --data-binary @docs/sample_factory_data.csv

# 3. Commit
curl -X POST 'localhost:8000/api/datasets/1/upload?machine_type=Industrial%20Pump' \
  -H 'Content-Type: text/csv' \
  --data-binary @docs/sample_factory_data.csv

# 4. Report
curl localhost:8000/api/report?dataset_id=1
curl localhost:8000/api/report/html?dataset_id=1 -o report.html

# 5. Remove it
curl -X DELETE localhost:8000/api/datasets/1
```

`GET /api/datasets/0/quality` runs the quality checks against the **demo fleet** —
the same code, so you can verify the checks work on data you can inspect.

---

## Where to find real data to try

Public datasets rarely carry all three channels, which is exactly why one is enough.

| Dataset | Real or synthetic | Channels it provides |
|---|---|---|
| **NASA IMS Bearing** / **CWRU Bearing** | Real run-to-failure | vibration |
| **MetroPT** (metro compressor) | Real | oil temperature, pressures, timestamps |
| **UCI Hydraulic condition monitoring** | Real test rig | vibration + temperatures |
| **NASA CMAPSS turbofan** | Simulated | 21 unnamed sensors, cycles not timestamps |
| **AI4I 2020** | **Synthetic** (UCI's own description) | RPM, 2 temperatures in Kelvin |

Useful Kaggle search terms:

```
bearing vibration temperature rpm
run to failure vibration dataset
machine condition monitoring sensor data
industrial iot sensor time series
hydraulic condition monitoring
```

Searching `predictive maintenance` alone mostly returns re-uploads of AI4I 2020,
which is synthetic and has no vibration column — so it proves little about whether
the system works on real data.

Two things to watch for:

- **Kelvin temperatures.** AI4I uses ~300 K. Convert to Celsius first, or the
  absolute limits will read it as a cold machine.
- **Cycles instead of timestamps.** CMAPSS has no clock. It will load using ingest
  time, but gap detection and cadence will be meaningless.

---

## What you get, and how much to trust it

Three **independent** signals, because no single one is reliable on data the system
has never seen:

### 1. ML risk score (0–100%)

GradientBoosting, trained on synthetic data. Honest limitations:

- Applied to your factory this is **unvalidated transfer**. Indicative, not proven.
- It is **non-monotonic at extreme sensor values**. Measured: a Conveyor Motor at
  90 °C scores ~98%, and at 95 °C scores ~0.1%. That is a real defect, not a
  rounding artefact.
- Inputs beyond the training range are clamped and reported, since tree ensembles
  cannot extrapolate.
- If your machine type is not one of the four it was trained on, it has no type
  signal and confidence drops.

### 2. Deviation score (0–100)

Weighted distance of current sensors from **that machine's own normal**. No
training, monotonic by construction, and recomputable by hand from the numbers on
screen. This is the signal to trust when the model and it disagree.

Where they do disagree, the report says so explicitly:

> Sensors deviate strongly from this machine's normal (deviation 74/100) but the
> model scores it 0%. Treat the deviation score as authoritative here.

### 3. Absolute limits

Published thresholds, independent of any baseline:

- **ISO 10816-1** vibration velocity zones (2.8 mm/s unsatisfactory, 7.1 mm/s
  unacceptable, Class II machines)
- Bearing temperature above ~90 °C a concern, ~100 °C severe (mineral-oil limits)

These exist because of a real blind spot: **if every reading you supply is already
degraded, the derived baseline treats the fault as normal** and relative deviation
reads zero. A machine that ran at 115 °C for its entire recorded history scores 0
deviation but is still flagged Critical by absolute limits.

---

## Reference values

Deviations need a definition of "normal". One precedence rule, applied to demo and
uploaded machines alike:

| Source | Meaning |
|---|---|
| `design_spec` | Nominal values were genuinely supplied |
| `derived_baseline` | Learned from the machine's own earliest readings |
| `assumed_default` | Nothing supplied and nothing derivable — least trustworthy |

Baselines use the **earliest** stable window, not the whole history: a machine that
degrades over 90 days would otherwise raise its own baseline and mask the
degradation. The median is used so a single spike cannot move it.

**Independent validation.** Run against demo machines whose true design spec is
known, the derivation recovers it to within 0.5% (1.804 vs 1.8, 2.0075 vs 2.0,
1.8955 vs 1.9). It was never told those values. That is the evidence for trusting
it where the spec is unknown.

---

## Data quality

Checked before anything is scored, because scoring unchecked data is how a system
produces confident nonsense.

| Check | Severity | Why |
|---|---|---|
| No sensor channels at all | **error** | Nothing to assess |
| Partial instrumentation | warning | Accepted; ML score unavailable, other signals apply |
| Flat-lined vibration/temperature | **error** | A dead transducer reads perfectly steady, which looks like an exceptionally healthy machine |
| Flat-lined RPM | warning | A fixed-setpoint drive legitimately reports a constant value |
| Implausible values | **error** | Wrong sensor or wrong unit, not a real reading |
| Missing values | warning / error | Error when a whole column is absent |
| Gaps > 3× cadence | warning | Missing history weakens trend features |
| Duplicate timestamps | warning | Usually a double-export |
| Fewer than 24 readings | warning | The rolling window is incomplete |

Verdicts: `ok`, `warnings` (usable, reduced confidence), `unusable` (fix and
re-upload — scores would be misleading rather than merely uncertain).

---

## Confidence

Computed from **evidence, not origin**. The rule does not know where your data came
from, so a well-populated upload legitimately outranks a poorly-evidenced demo
machine:

| Situation | Confidence |
|---|---|
| 6 months of CNC history, derived baseline | **high (85)** |
| 10 readings, unrecognised machine type, assumed defaults | **low (5)** |

Every downgrade is listed with its reason. Confidence drops to `low` automatically
whenever the model and the deviation score disagree.

---

## Limits

- 200 machines per upload
- 10,000 rows per request (split larger files; uploads accumulate in the dataset)
- UTF-8 CSV

---

## Known limitations, stated plainly

1. The model is trained on **synthetic** data. Metrics (recall 0.559, precision
   0.512, ROC-AUC 0.837) describe that synthetic test set, **not your factory**.
2. The model is **non-monotonic** at extreme values. Mitigated by two independent
   signals and explicit disagreement reporting, not fixed.
3. **Every monetary figure is modelled**, from ERP cost-centre rates plus stated
   engineering assumptions. Nothing is a measured saving.
4. A baseline derived from **already-degraded** data understates deviation.
   Absolute limits cover this; supply design specs where you have them.
5. `days_since_last_maintenance` is **neutralised** when no records are supplied.
   "Never maintained" and "records not provided" are indistinguishable here, so the
   non-alarmist reading is used and disclosed rather than assumed.
6. **A partially instrumented machine gets no ML score at all.** That is deliberate
   — the alternative is inventing values for channels that were never measured. The
   deviation score and absolute limits carry those machines, and both are reported
   with the channels they used.
