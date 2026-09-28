# PredictOps — Demo Script

A 5-minute run-through mapped to the problem statement. Every claim below is
backed by something visible on screen or a verifiable endpoint.

**The problem statement**: *"Manufacturers lose value to unplanned downtime
because OT sensor data sits apart from ERP and maintenance context. Build a
solution that converges IT and OT data to predict failures, automate work orders,
and lift Overall Equipment Effectiveness."*

---

## Before you start

```powershell
# Terminal 1 — backend
cd backend
.venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --reload --port 8000

# Terminal 2 — frontend
cd frontend
npm run dev
```

Open http://localhost:5173. Confirm before presenting:

- [ ] Dashboard loads with a Plant OEE ring between roughly 75% and 90%
- [ ] "Value at Risk" tile shows a dollar figure, not `—`
- [ ] At least 2 machines show Critical, and **some still show Normal**
- [ ] `GET /api/health` returns `ml_model: "loaded"`
- [ ] Investigation page answers "Why is M-102 at risk?"

### Reset for a clean demo

The simulator degrades failing machines over time, so after a long run most of the
fleet can read Critical — which weakens the prioritisation story, because
everything looks equally urgent. For the sharpest demo, reseed shortly beforehand:

```powershell
cd backend
Remove-Item predictops.db
cd ..
python data\generate_synthetic_data.py
cd backend
python -m app.ml.train        # only needed if the model is missing
```

That returns the fleet to roughly 4-6 Critical machines against a healthy
majority, which is the contrast worth showing.

If the model failed to load, `/api/health` reports `degraded_mode: true` and
alerts are flagged `degraded_mode`. **Say so out loud if it happens** — graceful
degradation is a design feature, and claiming ML is running when it is not is the
one thing that will lose the room.

---

## 1. Frame the problem (20 seconds)

> "A plant loses money to unplanned downtime not because nobody is watching the
> sensors, but because sensor data sits in a different system from the one that
> knows what a stopped machine costs. PredictOps joins those two, then acts."

Point at the **FEEDS** indicator in the header.

> "Those chips are reading provenance. Data reaches this system three ways — the
> simulator, a REST API, and CSV upload — and every reading is tagged with where
> it came from."

---

## 2. Convergence, made concrete (60 seconds)

Open the **Command Center**. Point at "Value at Risk".

> "This is not a sensor number. It is the modelled cost of every open alert if we
> let each machine run to failure — probability of failure multiplied by what a
> failure costs in that machine's ERP cost centre."

Then the key line:

> "Two machines can both be at 100% risk and be worth very different amounts of
> attention. A press in the press shop costs $6,500 an hour when it stops. A
> warehouse conveyor costs $1,400. The ranking follows the money, not the vibration
> amplitude."

Show it if asked:

```powershell
curl http://localhost:8000/api/erp/cost-centers
curl "http://localhost:8000/api/impact" # returns figures AND its own assumptions
```

**Be explicit**: *"Every currency figure is a modelled estimate from documented
assumptions, not a measured saving. The endpoint returns those assumptions so you
can audit them."*

---

## 3. Prediction with a real explanation (60 seconds)

Click the top Critical machine.

Point at the sensor tiles — including **RPM with its deviation**, then scroll to
**Top Risk Factors**.

> "This is not a hand-written rationale. These are the model's own per-feature
> contributions for this exact prediction, ranked, with direction. The bar is the
> relative contribution; the right column is the raw feature value."

Point at the Method line.

> "It says `tree importance weighted by feature value (approximation)`. The
> selected model is gradient boosting, so this is an approximation and it says so.
> A linear model would give exact contributions. I would rather label the method
> than overstate it."

Click **Explain This Alert**.

> "The narrative is generated from that same evidence. If the AI is unavailable,
> a deterministic explanation takes over and the badge changes to RULE-BASED —
> the feature never simply disappears."

---

## 4. Natural-language root-cause investigation (90 seconds)

Go to **Investigation**. This is the centrepiece — do not rush it.

Ask: **"Why is M-102 at risk?"**

Then click **Show evidence**.

> "That is the point. The answer is not the model's recollection — it is prose
> over rows retrieved from the database. Keyword routing selects which query runs,
> and the language model only phrases the result. It never writes SQL and never
> supplies a number."

Explain why that matters:

> "No SQL injection surface, no invented machine names, no hallucinated schema,
> and a reproducible answer. If the LLM is down, the same evidence still produces a
> deterministic answer of equal substance — not a degraded stub."

Ask two more:

- **"What is our financial exposure right now?"** — note it volunteers that the figures are modelled.
- **"Do we have the parts to fix the critical alerts?"** — joins maintenance stock to ERP lead time.

**The strongest question in the whole demo.** Ask:

**"Why is M-102 Critical when its risk score is 0%?"**

> "This is the system criticising its own model. The ML score is near zero, but the
> machine is Critical because vibration breaches ISO 10816-1 zone C and the
> temperature is past the lubricant limit. It names the standard, gives the measured
> value against the threshold, and says outright that the low score is the model
> failing rather than the machine being healthy."

Then, to show it knows what it doesn't know:

- **"How confident are you in the score for CM-305?"** — reports confidence with the reasons that weaken it.
- **"Which machines exceed ISO vibration limits?"** — a baseline-free answer, citable against a published standard.
- **"Is this real data or synthetic?"** — states provenance plainly rather than dodging.
- **"What is the weather in Paris?"** — refuses, and says what it *can* answer.

> "That last one matters. Until recently any unrecognised question was answered with
> fleet statistics — fluent, grounded, and about a question nobody asked. Measured,
> 11 of 12 realistic questions did that. An admitted gap beats a confident
> irrelevance, so it now refuses and lists its actual scope."

If a machine has recovered, ask about it:

> "It reports current state and mentions the earlier alert as resolved history.
> An earlier version of this reported the resolved alert as if it were live, which
> contradicted the dashboard. That was a real bug and it is fixed."

**Watch the source badge.** `GEMINI` means the model phrased it; `RULE-BASED` means
the deterministic template did, from the same evidence.

> "The free Gemini tier allows 20 requests per day. When that runs out the badge
> flips to RULE-BASED and the answers keep coming, because the language model only
> ever phrases evidence the database already produced. It is never load-bearing."

---

## 5. Close the loop: action and accountability (60 seconds)

From the alert, click **Create Work Order**.

> "Priority, assignee, due date and the required part are all derived — the
> planner is not retyping what the system already knows. Part availability comes
> from maintenance stock, and cost and lead time come from ERP."

Go to **Work Orders**, progress one to **Completed**.

> "Completion resolves the alert and OEE recovers on the next cycle. That is the
> full loop: sensor to prediction to explanation to work order to measured effect."

Then show accountability:

```powershell
curl "http://localhost:8000/api/auth/audit?limit=5"
```

> "Every mutation is audited with actor, role, action and before/after state.
> Roles exist too: planner, technician, viewer. Reads are always open so a wall
> display cannot break, and mutations are gated. It ships disabled because this UI
> has no login screen — I would rather that be a documented decision than a
> surprise 401 during a demo."

---

## 6. Show OEE as a work queue (30 seconds)

```powershell
curl "http://localhost:8000/api/oee/losses"
```

> "OEE on its own tells an operator something is wrong but not what to fix. This
> decomposes it: worst machine first, which of availability, performance or quality
> dominates, and the modelled cost of the gap to an 85% target. The three losses
> plus achieved OEE sum to exactly 1.0, so nothing is double-counted."

---

## 7. Prove real data can get in (30 seconds — optional but strong)

First pick a machine currently showing **Normal** on the dashboard, so the jump is
visible. Substitute its name below (the examples use `M-105`, which may already be
degraded if the simulator has been running a while).

```powershell
# Healthy readings
curl -X POST "http://localhost:8000/api/readings" -H "Content-Type: application/json" `
  -d '{"machine_name":"M-105","readings":[{"vibration":1.7,"temperature":60,"rpm":1520}]}'

# Degraded readings — watch the risk score jump
curl -X POST "http://localhost:8000/api/readings" -H "Content-Type: application/json" `
  -d '{"machine_name":"M-105","readings":[{"vibration":4.8,"temperature":94,"rpm":1300},{"vibration":5.0,"temperature":95,"rpm":1290},{"vibration":5.1,"temperature":96,"rpm":1280}]}'
```

> "Ingested data goes through exactly the same scoring path as the simulator. It
> is not a second-class import route — this is how a real line would connect."

Refresh the dashboard: the FEEDS indicator now shows an `api` chip.

> "Reset note: those rows are tagged `api`, so they can be removed without
> touching the seeded history."

---

## 7b. "Don't take my word for it — use your own data" (90 seconds)

**This is the strongest moment in the demo.** Offer it before you are asked.

> "Everything so far ran on synthetic data, so the fair question is whether this is
> a rehearsed story. Let's put your data through it instead."

Open **Bring Your Own Data** in the sidebar. Use a judge's file if they have one,
otherwise `docs/sample_factory_data.csv` — a healthy pump and a degrading CNC.

1. **Select the file, click Check mapping.**

   > "Their column names aren't ours — `Vibration_mm_s (RMS)`, `Shaft Speed [RPM]`,
   > `Asset`. Detected automatically, units in the header ignored. Nothing has been
   > written yet; this is for confirmation. Unrecognised columns are listed as
   > ignored rather than silently swallowed."

2. **Import and analyse.** Point at the healthy pump versus the degrading CNC.

   > "Same feature engineering, same model, same report as the fleet you just saw.
   > One pipeline, not an upload mode bolted on the side."

3. **The point to make deliberately — provenance.**

   > "The demo runs a simulator that fabricates readings. It is physically prevented
   > from writing to your machines: one filter on `data_origin`, and a test that runs
   > the simulator and asserts nothing was added. Every reading in this report is
   > yours."

4. **Expand a machine with "Why".** Show the three signals.

   > "Three independent signals, because I can't prove a model trained on synthetic
   > data transfers to your factory. ML risk is indicative. The deviation score is
   > arithmetic against this machine's own normal — no training, monotonic, you can
   > recompute it by hand from these numbers. And absolute ISO 10816 vibration zones
   > and lubricant temperature limits, which need no baseline at all."

5. **If a disagreement banner appears, lead with it.**

   > "Here the model says one thing and the sensors say another. We surface that
   > instead of hiding it, and we tell you which to trust. I found this by measuring:
   > the model scores a machine at 90 °C as 98% and at 95 °C as 0.1%. That's a real
   > defect, and 95 °C is inside its training range, so I can't clamp my way out of
   > it. The second signal is why a machine like that still raises an alert."

6. **Data quality.** Point at the verdict badge.

   > "Checked before anything is scored. A flat-lined vibration sensor is an error —
   > a dead transducer looks like an exceptionally healthy machine. Constant RPM is
   > only a warning, because a fixed-setpoint drive legitimately reads constant."

7. **Download report (HTML)**, then **Delete this dataset**.

   > "Self-contained, opens offline, keep it. And their data comes straight back out
   > — the demo fleet is untouched."

**If asked "how do I know the baselines are right?"**

> "Fair — so I validated it against machines whose true design spec I know. Run on
> the demo fleet, the derivation recovers 1.8 as 1.804, 2.0 as 2.0075, 1.9 as
> 1.8955. It was never given those numbers. That's the evidence for trusting it on
> your data, where I don't know the spec."

---

## 8. Model transparency (20 seconds)

```powershell
curl http://localhost:8000/api/model
```

> "Selected model, every candidate that lost and why, feature importance, and the
> exact risk-band thresholds the rules engine applies."

The strongest thing to say here:

> "I implemented probability calibration and then rejected it. It produced much
> better-behaved probabilities — Brier score halved — but recall collapsed from
> 0.76 to 0.18. In predictive maintenance a missed failure costs far more than a
> false alarm, so I kept the model that catches failures and left the calibration
> results in the report so you can see the trade-off I made."

---

## Likely questions

**"Is the data real?"**
> No — synthetic, and the degradation patterns were authored, so the model is
> learning a planted pattern. The pipeline, feature engineering, time-based split
> and leakage prevention are real. Rather than argue about it, **upload your own
> data** — section 7b. Your file goes through the identical pipeline, and the
> simulator is prevented from writing to it.

**"So would it actually work on my factory?"**
> Partly, and I can tell you exactly which parts. The ML score is unvalidated
> transfer — indicative, not proven. The deviation score is plain arithmetic against
> each machine's own normal, so it works anywhere and you can check it by hand. The
> absolute limits are ISO 10816-1 and lubricant temperature thresholds, which hold
> regardless. Where the model and the sensors disagree, the report says so and tells
> you to trust the sensors.

**"Isn't your model just wrong, then?"**
> It has a specific measured defect: non-monotonic at extreme values — 90 °C scores
> 98%, 95 °C scores 0.1%, and 95 °C is inside its training range, so clamping cannot
> fix it. I found it by sweeping the input range rather than by accident. That is
> precisely why there are two other independent signals and why a disagreement
> escalates to an alert instead of passing silently.

**"Are those savings real?"**
> No. They are modelled estimates. Downtime rates come from ERP cost centres;
> repair durations, crew sizes and labour rate are stated assumptions returned by
> `/api/impact`. I have deliberately not called them measured savings anywhere.

**"Is the ERP integration real?"**
> The tables are ERP-shaped and every machine is linked to a cost centre, but
> there is no live SAP connection. The read-only endpoints show where one attaches.

**"What if the AI is down?"**
> Every AI surface has a deterministic fallback and the UI shows which path
> produced the answer. During development the Gemini model name was retired and
> every call started 404-ing — the fallback meant the product kept working while
> I found it.

**"Why is precision only 0.53?"**
> A deliberate trade for recall of 0.74. A false alarm costs an inspection; a
> missed bearing failure costs eight hours of unplanned downtime. The selection
> rule enforces a minimum usable precision so it cannot degenerate into alarming
> on everything.

**"What would you do next?"**
> Replace the synthetic feed with a real historian, validate MTTR assumptions
> against actual work-order durations so the cost model stops being modelled, and
> add per-failure-mode models rather than one binary classifier.

---

## Timing

| Section | Time |
|---|---|
| Frame the problem | 0:20 |
| Convergence + value | 1:00 |
| Prediction + attribution | 1:00 |
| NL investigation | 1:30 |
| Work order + audit | 1:00 |
| OEE losses | 0:30 |
| **Core total** | **~5:20** |
| Ingestion + transparency (if time) | +0:50 |
| **Bring Your Own Data** | **+1:30** |

If you have only 3 minutes: sections 1, 2, 4 and 5. The convergence story and the
grounded investigation are what differentiate this.

If a judge sounds sceptical that the data is real, **drop section 4 and run 7b
instead.** Answering "is this a fake story?" by putting their own file through the
pipeline is worth more than any feature tour.
