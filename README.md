# PredictOps

**AI Predictive Maintenance & OEE Command Center**

> See the failure before it happens — and know exactly what to do about it.

Converges IT and OT data to predict equipment failures, automate work orders, and
lift Overall Equipment Effectiveness. Sensor streams are correlated with ERP and
maintenance records, so an alert arrives already ranked by money at stake, with the
part number, the assignee and the evidence behind the conclusion.

---

## Table of contents

- [Quick start](#quick-start)
- [Demo flow](#demo-flow-5-minutes)
- [Architecture](#architecture)
- [How IT and OT converge](#how-it-and-ot-converge)
- [The three independent signals](#the-three-independent-signals)
- [Natural-language investigation](#natural-language-investigation)
- [API reference](#api-reference)
- [Bring your own data](#bring-your-own-data)
- [Configuration](#configuration)
- [Enabling Snowflake Cortex](#enabling-snowflake-cortex-one-time)
- [Authentication](#authentication)
- [Deployment](#deployment-render-and-vercel)
- [Tests](#tests)
- [Honest limitations](#honest-limitations)
- [Tech stack](#tech-stack)

---

## Quick start

### Prerequisites

| Requirement | Version | Why this exact bound |
|---|---|---|
| Python | **3.13** (not 3.14) | pydantic requires ≤ 3.13 |
| Node.js | **≥ 20.19**, or ≥ 22.12 | Vite 8 declares `^20.19.0 \|\| >=22.12.0`; Node 18 fails to build |

### 1. Backend

```powershell
cd backend
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Generate data and train the model

Order matters: the trainer reads its training set out of the database, so the
generator has to run first.

```powershell
# from backend/, venv active
cd ..
python data/generate_synthetic_data.py   # 20 machines, 43,200 hourly readings, ERP master data
python data/seed_demo.py                 # the M-102 bearing-degradation scenario
cd backend
python -m app.ml.train                   # writes app/ml/model.joblib + model_report.json
```

Both data scripts are idempotent — they return early if the fleet already exists,
so re-running them is safe.

### 3. Start the backend

```powershell
# from backend/, venv active
uvicorn app.main:app --reload --port 8000
```

Verify: <http://localhost:8000/api/health>

```json
{ "status": "ok", "database": "connected", "ml_model": "loaded",
  "degraded_mode": false, "seed": "skipped", "machines": 20 }
```

`degraded_mode: true` means the model could not be loaded and scoring has fallen
back to rules. `seed` reports first-boot seeding (`running` / `completed` /
`skipped` / `failed`), so an empty fleet is never ambiguous.

### 4. Frontend

```powershell
cd frontend
npm install
npm run dev
```

Open <http://localhost:5173>.

> The dev server binds IPv6 `::1`. Use `localhost:5173` — `127.0.0.1:5173` will not
> connect.

---

## Demo flow (5 minutes)

Full narrated version with talking points and expected answers:
**[`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md)**

1. **Command Center** — *Value at Risk* shows modelled exposure across open alerts.
   The triage queue is ordered by `maintenance_priority`, not by sensor magnitude,
   and FEEDS chips show data provenance.
2. **Click the top Critical machine** — rising vibration and temperature, RPM
   deviation, risk trend over time.
3. **Top Risk Factors** — the model's own ranked per-feature contributions, with the
   attribution method labelled rather than implied.
4. **Explain This Alert** — an AI narrative over that same evidence, with a source
   badge naming the engine that actually answered.
5. **Investigation** — ask *"which machine is at high risk now and why?"* without
   naming a machine, then expand **Show evidence** to see the rows behind the prose.
6. **Create Work Order** — priority, assignee, part and due date pre-populated from
   ERP and maintenance context. Critical alerts create one automatically.
7. **Work Orders** — progress Open → In Progress → Completed; the alert resolves and
   OEE recovers.
8. **OEE Losses** — a Pareto of the worst machines that names the dominant loss
   (availability, performance or quality) and prices the gap.
9. **Model Card** — selected model, every rejected candidate with its metrics, and
   the risk bands.
10. **`GET /api/auth/audit`** — every mutation recorded with actor, role and
    before/after state.

---

## Architecture

```
React 19 + Vite 8 + TypeScript  (Recharts, react-router)
     |  REST  +  Server-Sent Events (/api/stream, 15s poll fallback)
FastAPI  (async SQLAlchemy 2.0)
     |
     +-- ML             GradientBoosting + per-prediction feature attribution
     +-- Rules engine   risk bands, alert policy, failure-mode inference
     +-- Deviation      model-free arithmetic vs each machine's own normal
     +-- Absolute       ISO 10816-1 vibration zones, lubricant temperature
     +-- Cost service   ERP-rate-driven value model, assumptions published
     +-- OEE service    A x P x Q, scaled loss decomposition
     +-- Work orders    automatic creation on Critical, duplicate-guarded
     +-- Investigation  intent routing -> fixed retrieval -> grounded answer
     +-- Auth           PBKDF2-HMAC-SHA256 + HMAC tokens, role-gated, audited
     |
SQLite (dev) / PostgreSQL (deployed)
     OT readings + maintenance   x   ERP cost centres, materials, production orders
     |
Snowflake Cortex -> Gemini -> deterministic template (always works)
```

The LLM never writes SQL. Retrieval is fixed code per intent; the model only
phrases an answer over evidence that was already fetched, and that evidence is
returned to the client so every claim can be checked.

---

## How IT and OT converge

| Side | Data | Source |
|---|---|---|
| OT | vibration, temperature, RPM, downtime, production counts | simulator, REST ingest, CSV upload |
| Maintenance | history, failure modes, parts used, spare stock | seeded records |
| IT / ERP | cost centres (downtime cost/hour), material cost + lead time, production orders | `cost_centers`, `materials`, `production_orders` |

This is what makes the output actionable rather than merely informative. OT data
says a bearing is degrading; maintenance history says it failed this way before; ERP
data says the machine sits in a cost centre where an hour of downtime costs 6,500 —
so alerts rank by money at stake instead of by sensor magnitude.

Production impact is computed from **open production orders in the next 7 days**
(`priority_service.resolve_production_impact`), scoring on remaining quantity
(`planned_qty - actual_qty`) and escalating when any order is already In Progress.
When a machine has no orders, it falls back to criticality and says so through
`basis: criticality_fallback` — the two are never silently conflated.

---

## The three independent signals

The model is trained on synthetic data and its transfer to real equipment is
unproven, so no conclusion rests on it alone.

| Signal | Basis | Property |
|---|---|---|
| **ML risk** | GradientBoosting on synthetic data | Unvalidated transfer, non-monotonic at extremes |
| **Deviation score** | Arithmetic vs each machine's own normal | No training, monotonic, checkable by hand |
| **Absolute limits** | ISO 10816-1 vibration zones, lubricant temperature | Independent of any baseline |

Each covers the others' blind spots, and **disagreement is reported rather than
hidden**:

- The model scored a machine at 110 °C as 0.1% risk. The deviation score caught it,
  and the alert says the model understated the severity.
- A machine hot for its entire recorded history has a baseline that encodes the fault
  as normal, so deviation reads zero — absolute limits still flag it Critical.

**Only one sensor channel is required, not all three.** Supply what you have: the
deviation score and absolute limits work from any subset, and the ML score is
reported as `n/a` rather than estimated from substituted values.

---

## Natural-language investigation

`POST /api/investigate` routes a question through a **closed taxonomy of 11 intents**
— `why_at_risk`, `trending_up`, `similar_past_failures`, `part_readiness`,
`cost_exposure`, `oee_losses`, `fleet_summary`, `machine_facts`, `signal_explain`,
`comparison`, `data_coverage` — plus an explicit `unsupported` outcome.

An unrecognised question returns a stated non-answer. It is **not** defaulted into a
fleet summary, because answering a question nobody asked with confident-looking
statistics is worse than admitting the question was not understood.

Every response carries an `evidence` array of the rows the prose was built from, and
a `source` naming the engine that answered (`cortex`, `gemini`, `cached` or
`deterministic`).

**Machine resolution.** A question that names no machine is still answered. *"Which
machine is at high risk now and why?"* selects the fleet's highest-ranked machine,
states that it was chosen and on what basis, and lists the runners-up. A question
that names a machine which cannot be resolved (`"Why is M-993 at risk?"`) is refused
and the known machine names are listed — answering about a *different* asset than the
one asked about would be worse than admitting the name did not match. Plural
selectors (*"which machines are at risk?"*) are answered as a ranked list rather than
with one machine's root cause.

The question is fenced inside `<question>` delimiters and the prompt states that its
contents are untrusted input, never instructions.

---

## API reference

| Capability | Endpoint | Notes |
|---|---|---|
| Fleet + risk scoring | `GET /api/machines`, `GET /api/machines/{id}` | Latest score, severity, baselines |
| Alerts | `GET /api/alerts`, `PATCH /api/alerts/{id}` | Model attribution and modelled cost |
| Alert explanation | `POST /api/alerts/{id}/explain` | AI narrative + `source` badge |
| Work orders | `GET/POST /api/workorders`, `PATCH /api/workorders/{id}` | Auto-created on Critical alerts |
| Work order from alert | `POST /api/alerts/{id}/workorder` | Priority, assignee, part pre-filled |
| OT ingestion | `POST /api/readings`, `POST /api/readings/csv` | Tagged `simulator` / `api` / `csv` |
| Ingestion provenance | `GET /api/readings/status` | Proves real data can enter the system |
| ERP master data | `GET /api/erp/{cost-centers,materials,production-orders}` | The IT half of the convergence |
| ERP context per machine | `GET /api/erp/machine-context/{id}` | Cost centre, orders, parts for one asset |
| Business impact | `GET /api/impact` | Value at risk, value protected, published assumptions |
| OEE | `GET /api/oee`, `GET /api/oee/losses` | Loss Pareto names the dominant loss and prices it |
| NL investigation | `POST /api/investigate`, `GET /api/investigate/suggestions` | 11 intents, returns evidence rows |
| Model transparency | `GET /api/model` | Selected model, all candidates, importance, risk bands |
| Datasets (BYOD) | `GET/POST /api/datasets`, `/preview`, `/upload`, `/quality` | Your own CSV through the same pipeline |
| Reports | `GET /api/report`, `/api/report/html` | Self-contained HTML, no external assets |
| Auth + audit | `POST /api/auth/login`, `GET /api/auth/me`, `GET /api/auth/audit` | Role-gated mutations, audit trail |
| Live push | `GET /api/stream` | SSE; the 15s poll stays as fallback |
| Health | `GET /api/health` | DB, model, seeding state, fleet size |

Reads never require a token. Only mutations are gated.

---

## Bring your own data

The dashboard runs on synthetic data, which invites a fair question: is any of this
real, or is it a rehearsed story? So the same pipeline accepts your data.

Open **Bring Your Data** in the sidebar, or:

```bash
curl -X POST localhost:8000/api/datasets -H 'Content-Type: application/json' -d '{"name":"My Factory"}'
curl -X POST localhost:8000/api/datasets/1/preview -H 'Content-Type: text/csv' --data-binary @docs/sample_factory_data.csv
curl -X POST localhost:8000/api/datasets/1/upload  -H 'Content-Type: text/csv' --data-binary @docs/sample_factory_data.csv
curl 'localhost:8000/api/report/html?dataset_id=1' -o report.html
```

Sample files: [`docs/sample_factory_data.csv`](docs/sample_factory_data.csv), and
[`docs/sample_vibration_only.csv`](docs/sample_vibration_only.csv) for the
single-channel case most real datasets resemble.

There is **one pipeline**, not an upload mode bolted alongside a demo mode. Uploaded
readings get the same feature engineering, the same model, the same scoring and the
same report as the demo fleet. The only genuine difference is provenance:

> **The simulator is never permitted to write to your data.** It fabricates readings,
> and extending a real factory's history with invented values would be
> indistinguishable from falsifying it. One filter enforces this, and a test runs the
> simulator and asserts nothing was added
> (`test_simulator_never_writes_to_external_machines`).

Full details, accepted column names and limitations:
**[`docs/BRING_YOUR_OWN_DATA.md`](docs/BRING_YOUR_OWN_DATA.md)**

---

## Configuration

Copy `.env.example` to `backend/.env`. **Every value has a working default — the
application runs with no `.env` file at all.**

### Core

```env
DATABASE_URL=sqlite:///./predictops.db
MODEL_PATH=app/ml/model.joblib
ENVIRONMENT=development          # production enables the startup safety guard
DEMO_MODE=true
LIVE_FEED_INTERVAL_SECONDS=15
```

### Language model

```env
# auto | cortex | gemini | none
# "auto" prefers Snowflake Cortex, then Gemini, then the deterministic template.
# Cortex is preferred because the Gemini free tier allows only 20 requests PER DAY,
# which is reached in ordinary use and silently forces every answer to rule-based.
LLM_PROVIDER=auto
CORTEX_MODEL=claude-sonnet-4-5
SNOWFLAKE_CONNECTION=            # blank resolves from connections.toml
GEMINI_API_KEY=                  # optional; the app works fully without it
GEMINI_MODEL=gemini-flash-latest # an alias, so a retired version cannot break it
```

With `LLM_PROVIDER=auto` and Cortex reachable, **Cortex answers and Gemini is only
the fallback** — setting a Gemini key does not switch the engine. Pin
`LLM_PROVIDER=gemini` if you want Gemini to answer.

### Work-order automation

```env
AUTO_WORK_ORDER_ENABLED=true
AUTO_WORK_ORDER_SEVERITIES=Critical
```

### Security

```env
AUTH_ENABLED=false               # gates MUTATIONS only; reads are always open
SECRET_KEY=predictops-dev-secret-change-me
DEMO_PASSWORD=predictops

CORS_ALLOWED_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
MAX_UPLOAD_BYTES=8388608         # request bodies are buffered in memory

RATE_LIMIT_ENABLED=true
LOGIN_ATTEMPT_LIMIT=10           # PBKDF2 at 120k rounds is also a CPU lever
LOGIN_ATTEMPT_WINDOW_SECONDS=300
LLM_REQUEST_LIMIT=30             # these endpoints spend metered third-party quota
LLM_REQUEST_WINDOW_SECONDS=60

TRUSTED_PROXY_HOPS=0             # set to 1 behind Render/Railway/Fly — see Deployment
HOST=127.0.0.1                   # 0.0.0.0 publishes an API whose mutations may be open
PORT=8000

NOTIFICATION_WEBHOOK_URL=        # empty = delivery logged as "skipped", never silent
```

`CORS_ALLOWED_ORIGINS` is an explicit allowlist, not a wildcard: `*` cannot be
combined with credentials per the Fetch standard, and a `*` value is downgraded to
the localhost defaults outside development.

With `ENVIRONMENT=production`, `verify_startup_configuration()` **refuses to boot**
on an unsafe configuration — `AUTH_ENABLED=false`, a placeholder `SECRET_KEY`, the
default `DEMO_PASSWORD`, or `DEMO_MODE=true`. The failure is loud at startup rather
than latent.

---

## Enabling Snowflake Cortex (one-time)

Nothing here is required — without any provider the Investigation tab still answers,
deterministically. This only changes *who phrases* the answer.

`LLM_PROVIDER=auto` prefers Cortex, but preferring it is not the same as reaching it.
The backend refuses connections whose authenticator needs a browser
(`oauth_authorization_code`, `externalbrowser`, `username_password_mfa`), because a
server cannot complete that handshake — measured, opting in anyway opened a browser
window and blocked **122 s** before failing. If every entry in your
`~/.snowflake/connections.toml` uses browser auth, which is the common case, Cortex is
never selected and answers fall through to Gemini's 20/day and then to the template.

Fix it once:

```powershell
cd backend
python -m app.llm.prime_cortex_login
```

It completes the browser login, verifies `SNOWFLAKE.CORTEX.COMPLETE` actually runs on
that account, and caches a reusable token in `~/.snowflake/predictops_token_cache`.
From then on the guard stands down and startup opens Cortex without prompting —
measured **10.0 s** with the prompt, **3.3 s** without. Key-pair auth and PATs need
none of this and are preferred for anything unattended.

Re-run it after switching Snowflake accounts: the cached token is account-bound.

> **Not using keyring is deliberate**, despite the connector recommending it. On
> Windows `CredWrite` caps a credential blob at 2560 bytes and keyring encodes it as
> UTF-16, which doubles it — the cached token measures ~1.6 KB, so it exceeds the cap
> once encoded and the failed write escapes `connect()` as
> `(1783, 'CredWrite', 'The stub received bad data')`. Installing keyring therefore
> made Cortex *unreachable* rather than persistent: the login itself succeeded and
> only the cache write failed. The connector's own `FileTokenCache` is forced instead;
> it needs no extra dependency and is verified with keyring absent.

---

## Authentication

Auth gates **mutating endpoints only**; reads are always open. Tokens are
HMAC-signed, passwords are PBKDF2-HMAC-SHA256 at 120,000 rounds, and the whole
implementation uses only the standard library.

Demo accounts are seeded at startup and all use `DEMO_PASSWORD`:

| Username | Role | Can do |
|---|---|---|
| `planner` | planner | acknowledge alerts, create + progress work orders, ingest |
| `technician` | technician | acknowledge alerts, progress work orders, ingest |
| `viewer` | viewer | read-only — every mutation returns 403 |

With `AUTH_ENABLED=true`, mutations without a token return **401** and mutations by
`viewer` return **403**. Sign in from the header menu in the UI, or:

```powershell
curl -X POST http://localhost:8000/api/auth/login `
  -H "Content-Type: application/json" `
  -d '{"username":"planner","password":"predictops"}'
```

Changing `DEMO_PASSWORD` re-hashes these three accounts on the next boot. This
matters: they previously kept whatever password they were first seeded with, so an
account seeded while the shipped default was active kept accepting that published
default afterwards — the startup guard inspects the *setting*, but the stored *hash*
is what authenticates.

---

## Deployment: Render and Vercel

Backend and managed Postgres on Render via [`render.yaml`](render.yaml); frontend on
Vercel via [`frontend/vercel.json`](frontend/vercel.json).

### Prerequisite: this project must be its own Git repository

Both platforms deploy by cloning from a Git host.

```powershell
cd PredictOps
git init
git add .
git commit -m "PredictOps"
git remote add origin <your repository url>
git push -u origin main
```

### Render

Dashboard → **New → Blueprint** → point at the repository. `render.yaml` declares the
web service and a Postgres instance, and sets every environment variable except two
you must enter yourself (they are marked `sync: false`):

| Variable | Value |
|---|---|
| `DEMO_PASSWORD` | your choice — judges sign in with it |
| `GEMINI_API_KEY` | your key |
| `CORS_ALLOWED_ORIGINS` | **edit in `render.yaml`** to your real Vercel origin |

The build trains the model against a **throwaway SQLite file**, not the live
database. Three reasons: `model.joblib` is deliberately gitignored because a pickle
should be rebuilt from source rather than trusted from a repository; Render's build
output becomes the running instance's filesystem, so the artefact is present at
runtime without committing it; and it removes any dependency on the database being
reachable at build time.

Application data is seeded separately, into Postgres, **on first boot** — as a
background task, because it inserts 43,200 readings row-by-row and would otherwise
trip the health check. Watch `GET /api/health` → `seed` and `machines`.

### Vercel

Set the project root to `frontend/`, then set **`VITE_API_BASE_URL`** to your Render
URL (e.g. `https://predictops-api.onrender.com`). Vite inlines this at **build**
time, so a runtime value has no effect. Leaving it empty makes the browser call its
own origin, which only works behind the local dev proxy.

### What changes on a deployment, and why

- **Postgres URL rewriting.** Render emits `postgres://…?sslmode=require`, which
  fails three separate ways: SQLAlchemy 2.0 removed the `postgres` alias, a bare
  `postgresql://` selects the synchronous psycopg2 driver, and `sslmode` is a libpq
  parameter that asyncpg rejects outright. `normalise_database_url` handles all
  three, so the connection string can be pasted verbatim.
- **Schema creation is dialect-gated.** `init_db` carries a SQLite-only migration
  shim (`PRAGMA`, `sqlite_master`, table rebuild) for databases created by earlier
  versions. On Postgres it returns early — `create_all` builds the current schema
  straight from the models.
- **`TRUSTED_PROXY_HOPS=1`.** Behind a reverse proxy every request carries the
  proxy's address, so without this all visitors share one rate-limit bucket and a
  handful of users lock out everyone else. It reads the Nth entry from the *right* of
  `X-Forwarded-For`, since each trusted proxy appends the address it actually saw;
  forged entries can only be prepended and are ignored.
- **Snowflake Cortex is unavailable on Render.** It reads credentials from
  `~/.snowflake/connections.toml`, which cannot exist on a PaaS instance, and
  browser auth cannot complete headlessly. `render.yaml` pins `LLM_PROVIDER=gemini`
  to make that explicit rather than relying on silent failover.

### Known constraints

- Render's free Postgres expires after a fixed window — move to a paid plan before a
  demo you cannot afford to lose.
- Gemini's free tier is ~20 requests/day; past that every answer is the deterministic
  template.
- **Single instance only.** The answer cache, rate-limit windows, SSE subscriber set
  and live-feed loop are all in-process.
- Free instances spin down when idle, which stops the live feed until the next
  request wakes them.

---

## Tests

```powershell
cd backend
.venv\Scripts\Activate.ps1
python -m pytest -q
```

**329 tests.** The suite forces `LLM_PROVIDER=none`, so it never calls a language
model: real Cortex and Gemini requests made tests slow (measured 70–127 s),
non-deterministic, and able to fail because a quota was exhausted rather than because
the code was wrong. Tests that exercise the LLM path stub the provider and assert on
the prompt.

Coverage by area:

| Area | What is asserted |
|---|---|
| API contract | Response shapes and status codes for every endpoint |
| IT/OT convergence | ERP joins resolve, cost centres link, production impact drives priority |
| Unified pipeline | Upload = demo parity, simulator containment, NULL sensors never substituted |
| ML | Feature engineering, attribution (including no-scaler and calibrated paths), risk bands |
| Investigation | All 11 intents route correctly, answers stay grounded, evidence is non-empty |
| Security | Secret-key resolution, rate limiting, body caps, webhook validation, prompt fencing |
| Auth | 401/403 gating per role, audit writes |
| LLM provider | Selection order, the browser-auth token-cache guard, the priming command |
| Deployment | Postgres URL normalisation, proxy-aware client identity, demo-password realignment |

```powershell
python -m app.ml.train        # retrain and regenerate the model report
```

---

## Honest limitations

Stated plainly, because a judge will find these anyway.

- **Data is synthetic.** Model metrics demonstrate the workflow, not real factory
  performance. Degradation patterns were authored, so the model is learning a pattern
  that was deliberately planted.
- **Currency figures are modelled estimates, never measured savings.** Downtime rates
  come from ERP cost centres; repair durations, crew sizes and labour rates are
  documented engineering assumptions in `cost_service.ASSUMPTIONS`, returned by
  `/api/impact` so they can be audited.
- **ERP tables are ERP-shaped, not ERP-connected.** There is no live SAP link; the
  schema and read-only endpoints show where one would attach.
- **The model is non-monotonic at extreme sensor values.** Measured: a Conveyor Motor
  at 90 °C scores ~98%, at 95 °C ~0.1% — and 95 °C is *inside* the training range, so
  input clamping cannot repair it. Mitigated by an independent monotonic deviation
  score, absolute published limits, explicit disagreement reporting and alert
  escalation. **Mitigated, not fixed.**
- **Recall dropped from 0.739 to 0.559 deliberately.** Training previously assigned
  `machine_age_days` where maintenance records were absent, which taught the model
  that a large value meant safe: 53% of training rows sat in that region with a 0.0
  failure rate. The consequence was severe — a healthy-looking machine scored 37.2%
  with records and **0.0% without**, so any upload lacking maintenance history was
  reported as fine regardless of condition. Removing that crutch cost recall and is
  the honest trade.
- **Risk scores still saturate somewhat.** About 6% of the selected model's positive
  predictions sit at the top of the range (`saturated_fraction` = 0.058 for Gradient
  Boosting in `/api/model`). Probability calibration was implemented, tested, and
  **rejected**: calibrated Logistic Regression reached 0.919 precision but only
  **0.127 recall** against Gradient Boosting's 0.559, and catching failures is the
  priority.
- **Tree attribution is an approximation.** For GradientBoosting, contributions are
  global importance weighted by feature value, labelled `tree_importance_approx`.
  Only linear models get exact contributions.
- **A baseline derived from already-degraded data understates deviation.** If every
  reading supplied is faulty, the derived normal encodes the fault. Absolute limits
  cover this case; supply design specs where you have them.
- **Auth ships disabled** (`AUTH_ENABLED=false`) so the local demo runs open. The full
  stack is implemented, tested and wired into the UI; deployments set it to `true`,
  and `ENVIRONMENT=production` refuses to boot without it.
- **Cortex is local-only.** Deployed instances run on Gemini and its daily cap, then
  the deterministic template.

---

## Tech stack

| Layer | Technology |
|---|---|
| Frontend | React 19, Vite 8, TypeScript 6, Tailwind CSS 4, Recharts 3, react-router 7 |
| Backend | Python 3.13, FastAPI, SQLAlchemy 2.0 (async) |
| Database | SQLite + aiosqlite (dev) · PostgreSQL + asyncpg (deployed) |
| ML | scikit-learn — GradientBoostingClassifier, 7-day failure horizon |
| AI phrasing | Snowflake Cortex (`claude-sonnet-4-5`) → Gemini → deterministic template |
| Auth | PBKDF2-HMAC-SHA256 + HMAC-signed tokens, standard library only |
| Deployment | Render (API + Postgres), Vercel (static frontend) |

---

> **Note:** this is a hackathon MVP running on synthetic data. Metrics and OEE values
> demonstrate the workflow concept and do not represent real factory performance.
