---
name: predictops-workorder
description: "Turn a flagged PredictOps machine into a maintenance work order, with the production and cost impact attached so the priority is justified rather than asserted. Use whenever the user wants to act on an alert, raise or create a work order, schedule maintenance, assign a technician, or asks what a failure would cost or what to do about a machine — even if they do not name this skill. Triggers: create work order, raise a work order, schedule maintenance, act on this alert, assign technician, fix this machine, what will it cost, cost of failure, production impact, order the part."
---

# PredictOps: raise a work order

Takes a machine PredictOps has flagged and turns it into a work order, after
gathering the evidence that justifies doing the work now.

The order is created from an **alert**, not from a bare machine id, wherever an
alert exists. Creating it from the alert is what auto-populates the failure mode,
the part needed, the priority and the due date from one place in
`workorder_service`, so a hand-assembled order cannot drift from the rules the
rest of the system applies.

## Prerequisites

- PredictOps backend running. Default `http://localhost:8000`, or the deployed
  origin. Call this `BASE`.
- Creating a work order needs the `workorder:create` permission, which only
  **planner** holds. `technician` can update an existing order but not open one;
  `viewer` can do neither.

Use `curl.exe`, not `curl` — in PowerShell 5.1 `curl` is an alias for
`Invoke-WebRequest` and will reject these flags.

**Sending JSON on Windows:** pipe the body into curl and read it from stdin with
`--data-binary "@-"`. An inline `-d '{"machine_id":3}'` is mangled by PowerShell's
quote handling and returns a 400 `VALIDATION_ERROR` — verified. On a POSIX shell
the inline form is fine.

Use stdin rather than a temporary file. A file holding a password survives a
failed command, and this working directory is a git repository — a credential
written here is one `git add .` away from being committed.

## Workflow

### Step 1: Identify the machine

If the user named a machine, find it:

```bash
curl.exe -s BASE/api/machines
```

Match on `label`. If they did not name one, run the ranking first and take the
worst machine — but say which one you picked and why, rather than acting silently:

```bash
curl.exe -s BASE/api/report
```

If nothing is flagged at all, stop and say so. A work order for a healthy machine
is waste, and raising one anyway would make the queue meaningless.

### Step 2: Find the open alert

```bash
curl.exe -s "BASE/api/alerts?status=Active"
```

Match on `machine_id`. Also check `status=Acknowledged` — an acknowledged alert
still describes the machine's current condition, so it is a valid basis for an
order.

Note the `id` (call it `ALERTID`), `risk_score`, `severity`, `failure_mode`,
`part_needed`, `recommended_action`, `maintenance_priority`.

**If there is no open alert**, do not fabricate one. Two honest options, and the
choice is the user's:

- the machine is genuinely fine → report that and stop;
- they want an order anyway (planned work, or a machine flagged only by deviation
  with no alert raised) → use the manual path in Step 5b and state plainly that
  it carries no alert evidence.

### Step 3: Gather the evidence

Two calls, both reads:

```bash
curl.exe -s BASE/api/report/machines/MACHINEID
curl.exe -s BASE/api/erp/machine-context/MACHINEID
```

From the machine report: `ml_risk_score`, `deviation_score`, `absolute_concerns`,
`signal_disagreement`, `confidence`, `value_at_risk`, `top_factors`.

From the ERP context: `cost_center`, `part` (stock on hand and lead time),
`production_impact`, `production_impact_basis`, `production_impact_detail`,
`committed_units`, `open_orders`.

`production_impact_basis` matters and should be reported: `erp_production_orders`
means the impact came from real scheduled orders, while `criticality_fallback`
means there were no orders to read and it was inferred from the machine's
criticality rating. Those deserve different confidence.

### Step 4: Present the case and stop

**⚠️ MANDATORY STOPPING POINT.** Present before creating anything:

```
Machine:            <label> (<type>, criticality <x>)
Signals:            ML risk <n>% · deviation <n> · <absolute concerns or "within limits">
Confidence:         <high|medium|low> — <reasons>
Failure mode:       <mode>
Part needed:        <part> — <stock on hand>, lead time <n> days
Production impact:  <impact> (basis: <basis>)
Committed units:    <n> across <n> open orders
Value at risk:      $<n> (modelled)

Create this work order?
```

Three things to be straight about here:

- **Value at risk is modelled**, from ERP cost-centre rates plus documented
  assumptions. It is not a measured or guaranteed saving — do not present it as
  money saved.
- If `signal_disagreement` is set, surface it now. The deviation score should be
  preferred, because the fitted model is non-monotonic at extreme sensor values.
- If the part has **zero stock and a long lead time**, say so before the order is
  raised. A work order that cannot be executed for two weeks is a scheduling fact
  the planner needs up front, not after the fact.

Wait for explicit approval. Do not chain from Step 3 into Step 5.

### Step 5a: Create from the alert (preferred)

```bash
curl.exe -s -X POST BASE/api/alerts/ALERTID/workorder `
  -H "Authorization: Bearer <token>"
```

This is **idempotent**: if an order already exists for the alert, the existing one
is returned rather than a duplicate created. So an unexpected "already exists"
result is the system protecting the queue, not an error — report the existing
order's id and status instead of retrying.

### Step 5b: Manual creation (no alert)

Only when Step 2 found no alert and the user still wants an order:

```bash
'{"machine_id":MACHINEID}' | curl.exe -s -X POST BASE/api/workorders `
  -H "Content-Type: application/json" `
  -H "Authorization: Bearer <token>" `
  --data-binary "@-"
```

Assignment, priority and due date are still filled in by `workorder_service`, so
this path stays consistent with the rest. `alert_id` is optional and should be
omitted when there is no alert rather than passed as a guess.

### Step 6: Confirm what was created

Report from the response: `id`, `machine_name`, `technician`, `part_needed`,
`priority`, `due_date`, `status`.

Then verify it is in the queue:

```bash
curl.exe -s "BASE/api/workorders?status=Open"
```

Tell the user it is now visible on the Work Orders page in the dashboard. Seeing
the same record in the UI is what shows the skill wrote to the real system rather
than reporting a plausible result.

## Authentication

Steps 5a and 5b need a planner token:

```bash
'{"username":"planner","password":"<password>"}' | curl.exe -s -X POST BASE/api/auth/login `
  -H "Content-Type: application/json" `
  --data-binary "@-"
```

Use `token` from the response. Ask the user for the password rather than guessing,
and do not echo it back. Never write it to a file — stdin keeps it out of the
working directory, which is a git repository. The shell may still retain the
command in its history, so prefer a throwaway or demo credential here.

A **403** on creation means the account is not a planner. Name the role gap
instead of retrying — a technician account will never succeed here.

If `AUTH_ENABLED=false`, no token is needed and the header can be dropped.

## Troubleshooting

| Response | Meaning | What to do |
|---|---|---|
| `404 ALERT_NOT_FOUND` | wrong `ALERTID` | re-read it from Step 2 |
| `404 MACHINE_NOT_FOUND` | wrong machine id | list machines again |
| `404 MACHINE_NOT_IN_REPORT` | machine exists but has no readings | it cannot be scored yet; ingest data first |
| `401` | auth enabled, no token | log in |
| `403` | role lacks `workorder:create` | only planner can create |
| an existing order comes back | idempotency guard | report it; do not retry |
| `VALIDATION_ERROR` "Unterminated string" | PowerShell mangled an inline `-d` body | use the stdin form |

## Stopping Points

- ✋ Step 2 if there is no open alert — the user chooses whether to proceed
- ✋ Step 4 before any write, always
- ✋ Any 403 — report the role gap rather than retrying

## Output

Either a created work order, reported with its id, assignee, part, priority, due
date, and the evidence that justified it — or a reasoned decision not to create
one, which is an equally valid result when nothing is actually wrong.
