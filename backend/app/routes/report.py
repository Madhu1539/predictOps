"""
Machine analysis report — GET /api/report

One report shape for any dataset, the built-in demo fleet included. Accepting the
demo fleet as a valid target is deliberate: it means the artefact a judge gets for
their own data is produced by the same code path they just watched, not a special
"upload mode".

Every report carries its own basis and limitations, because a number without its
provenance invites more trust than it has earned.
"""
import html
import json
import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.alert import Alert
from app.models.cost_center import CostCenter
from app.models.dataset import Dataset
from app.models.machine import ORIGIN_SIMULATED, Machine
from app.models.maintenance import MaintenanceRecord
from app.models.oee import OeeSnapshot
from app.models.sensor import SensorReading
from app.services import cost_service
from app.services.anomaly_service import (
    ASSUMPTIONS as DEVIATION_ASSUMPTIONS,
    absolute_concerns,
    absolute_score,
    assess_confidence,
    detect_signal_disagreement,
    deviation_score,
)
from app.services.baseline_service import reference_for
from app.services.data_quality import assess_dataset_quality
from app.services.rules_engine import classify_risk

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/report", tags=["report"])

OPEN_STATUSES = ("Active", "Acknowledged")


def _limitations(has_external: bool, quality_overall: str, partial_sensors: bool = False) -> List[str]:
    """Stated plainly, in the report itself, every time."""
    notes = [
        "Risk scores come from a model trained on SYNTHETIC data with authored "
        "degradation patterns. Applied to a real factory this is unvalidated "
        "transfer: the numbers are indicative, not proven for your equipment.",
        "The deviation score is independent of that model. It is plain arithmetic "
        "against each machine's own reference values and can be recomputed by hand, "
        "so it remains meaningful where the model does not transfer.",
        "Relative deviation has a blind spot: if every reading supplied was already "
        "degraded, the derived baseline treats the fault as normal. Absolute limits "
        "(ISO 10816-1 vibration zones, lubricant temperature limits) are applied "
        "alongside it to cover that case.",
        "The fitted model is non-monotonic at extreme sensor values. Where it "
        "disagrees with the deviation score this is flagged, and the deviation "
        "score should be preferred.",
        "Any monetary figure is a MODELLED estimate from ERP cost-centre rates plus "
        "documented engineering assumptions. None of it is a measured saving.",
    ]
    if has_external:
        notes.append(
            "Machines in this report were supplied externally. The built-in "
            "simulator never writes to them, so every reading shown is yours."
        )
    if partial_sensors:
        notes.append(
            "Some machines here do not report all three sensor channels. The ML risk "
            "score requires vibration, temperature and RPM together, so for those "
            "machines it is reported as unavailable rather than estimated from "
            "substituted values. Their assessment rests on measured deviation and "
            "published absolute limits, which need only the channels present."
        )
    if quality_overall == "unusable":
        notes.insert(0, (
            "DATA QUALITY FAILED. At least one machine has a disqualifying problem, "
            "so the scores below should not be acted on until it is corrected."
        ))
    elif quality_overall == "warnings":
        notes.append(
            "Data quality warnings were raised; see the quality section for what "
            "reduces confidence here."
        )
    return notes


async def _build_report(db: AsyncSession, dataset_id: Optional[int]) -> Dict[str, object]:
    """Assemble the report for one dataset, or the demo fleet when None."""
    query = select(Machine).where(
        Machine.dataset_id.is_(None) if dataset_id is None else Machine.dataset_id == dataset_id
    )
    machines = (await db.execute(query)).scalars().all()
    if not machines:
        raise HTTPException(status_code=404, detail="NO_MACHINES_IN_SCOPE")

    cost_centers = {
        cc.code: cc for cc in (await db.execute(select(CostCenter))).scalars().all()
    }

    quality = await assess_dataset_quality(db, dataset_id)

    rows: List[dict] = []
    total_at_risk = 0.0
    has_external = False

    for machine in machines:
        if machine.data_origin != ORIGIN_SIMULATED:
            has_external = True

        reading_count = (
            await db.execute(
                select(func.count()).select_from(SensorReading)
                .where(SensorReading.machine_id == machine.id)
            )
        ).scalar_one()

        latest = (
            await db.execute(
                select(SensorReading)
                .where(SensorReading.machine_id == machine.id)
                .order_by(SensorReading.timestamp.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        alert = (
            await db.execute(
                select(Alert)
                .where(Alert.machine_id == machine.id)
                .where(Alert.status.in_(OPEN_STATUSES))
                .order_by(Alert.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        snapshot = (
            await db.execute(
                select(OeeSnapshot)
                .where(OeeSnapshot.machine_id == machine.id)
                .order_by(OeeSnapshot.timestamp.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        maintenance_count = (
            await db.execute(
                select(func.count()).select_from(MaintenanceRecord)
                .where(MaintenanceRecord.machine_id == machine.id)
            )
        ).scalar_one()

        reference = reference_for(machine)
        ml_risk = alert.risk_score if alert else (latest.risk_score if latest else None)

        # Computed here rather than read from the alert, so a machine with no open
        # alert still reports both independent signals. A report that only describes
        # already-flagged machines cannot be used to check whether the flagging
        # itself is right.
        current = {
            "vibration": latest.vibration if latest else None,
            "temperature": latest.temperature if latest else None,
            "rpm": latest.rpm if latest else None,
        }
        deviation_detail = deviation_score(current, {
            "vibration": reference["vibration"],
            "temperature": reference["temperature"],
            "rpm": reference["rpm"],
        }) if latest else {"score": None, "components": []}
        deviation = deviation_detail.get("score")

        concerns = absolute_concerns(current) if latest else []
        absolute = absolute_score(concerns)

        # Which channels this machine actually reports. Absent ones are absent, not
        # zero, and the ML model needs all three.
        present_sensors = [k for k, v in current.items() if v is not None]
        missing_sensors = [k for k, v in current.items() if v is None]

        confidence_detail = assess_confidence(
            reading_count=int(reading_count or 0),
            machine_type=machine.type,
            reference_source=reference["source"],
            maintenance_available=bool(maintenance_count),
            missing_sensors=missing_sensors or None,
        )
        confidence = confidence_detail["confidence"]

        disagreement = (
            detect_signal_disagreement(ml_risk, deviation or 0.0)
            if ml_risk is not None else None
        )
        attribution: List[dict] = []
        if alert and alert.attribution:
            try:
                attribution = json.loads(alert.attribution)[:4]
            except Exception:
                attribution = []

        cost_center = cost_centers.get(machine.cost_center_code)
        value_at_risk = 0.0
        if alert and alert.risk_score:
            probability = max(0.0, min(alert.risk_score, 100.0)) / 100.0
            value_at_risk = round(
                probability
                * cost_service.estimate_unplanned_failure_cost(alert.failure_mode, cost_center),
                2,
            )
            total_at_risk += value_at_risk

        rows.append({
            "machine": machine.label,
            "machine_id": machine.id,
            "type": machine.type,
            "criticality": machine.criticality,
            "data_origin": machine.data_origin,
            "readings": int(reading_count or 0),
            "maintenance_records": int(maintenance_count or 0),
            "ml_risk_score": round(ml_risk, 1) if ml_risk is not None else None,
            "severity": (alert.severity if alert else classify_risk(ml_risk or 0.0)),
            "deviation_score": deviation,
            "deviation_components": deviation_detail.get("components", []),
            "absolute_concerns": concerns,
            "absolute_score": absolute,
            "confidence": confidence,
            "confidence_reasons": confidence_detail["reasons"],
            "ml_available": confidence_detail.get("ml_available", True),
            "present_sensors": present_sensors,
            "missing_sensors": missing_sensors,
            "signal_disagreement": disagreement,
            "failure_mode": alert.failure_mode if alert else None,
            "recommended_action": alert.recommended_action if alert else None,
            "reference_source": reference["source"],
            "reference": {
                "vibration": reference["vibration"],
                "temperature": reference["temperature"],
                "rpm": reference["rpm"],
            },
            "baseline_method": machine.baseline_method,
            "baseline_sample_count": machine.baseline_sample_count,
            "current": current,
            "oee": round(snapshot.oee, 4) if snapshot else None,
            "value_at_risk": value_at_risk,
            "top_factors": attribution,
        })

    # Worst first, ranking on whichever signal is highest so a machine that only
    # one method flags cannot be buried at the bottom of the list.
    rows.sort(
        key=lambda r: max(
            r["ml_risk_score"] or 0.0,
            r["deviation_score"] or 0.0,
            r["absolute_score"] or 0.0,
        ),
        reverse=True,
    )

    dataset = None
    if dataset_id is not None:
        dataset = (
            await db.execute(select(Dataset).where(Dataset.id == dataset_id))
        ).scalar_one_or_none()

    flagged = [r for r in rows if r["signal_disagreement"]]

    return {
        "scope": {
            "dataset_id": dataset_id,
            "dataset_name": dataset.name if dataset else "Built-in demo fleet",
            "machine_count": len(rows),
            "total_readings": sum(r["readings"] for r in rows),
            "contains_external_data": has_external,
        },
        "headline": {
            "machines_needing_attention": sum(
                1 for r in rows
                if (r["ml_risk_score"] or 0) >= 45
                or (r["deviation_score"] or 0) >= 60
                or (r["absolute_score"] or 0) >= 60
            ),
            "highest_risk_machine": rows[0]["machine"] if rows else None,
            "total_value_at_risk": round(total_at_risk, 2),
            "signal_disagreements": len(flagged),
            "machines_over_absolute_limits": sum(1 for r in rows if r["absolute_concerns"]),
            "machines_without_ml_score": sum(1 for r in rows if not r["ml_available"]),
        },
        "data_quality": quality,
        "machines": rows,
        "assumptions": {
            "cost_model": cost_service.ASSUMPTIONS,
            "deviation_model": DEVIATION_ASSUMPTIONS,
        },
        "limitations": _limitations(
            has_external,
            quality["overall"],
            partial_sensors=any(r["missing_sensors"] for r in rows),
        ),
    }


@router.get("")
async def get_report(
    dataset_id: Optional[int] = Query(
        None, description="Omit or pass 0 for the built-in demo fleet."
    ),
    db: AsyncSession = Depends(get_db),
):
    """Full analysis report as JSON."""
    target = None if not dataset_id else dataset_id
    return await _build_report(db, target)


@router.get("/machines/{machine_id}")
async def get_machine_report(machine_id: int, db: AsyncSession = Depends(get_db)):
    """Report detail for a single machine, in whichever dataset it belongs to."""
    machine = (
        await db.execute(select(Machine).where(Machine.id == machine_id))
    ).scalar_one_or_none()
    if machine is None:
        raise HTTPException(status_code=404, detail="MACHINE_NOT_FOUND")

    report = await _build_report(db, machine.dataset_id)
    for row in report["machines"]:
        if row["machine_id"] == machine_id:
            return {
                "scope": report["scope"],
                "machine": row,
                "assumptions": report["assumptions"],
                "limitations": report["limitations"],
            }
    raise HTTPException(status_code=404, detail="MACHINE_NOT_IN_REPORT")


def _esc(value: object) -> str:
    """HTML escaping for interpolation into element text or a quoted attribute.

    Every value in the report that originates outside the code passes through
    here: machine names and CSV headers are user-supplied, and this document is
    served from the API origin, so an unescaped one would be script execution
    against it. `html.escape` is used rather than a hand-rolled chain so the
    replacement set cannot drift, and `quote=True` covers attribute contexts.
    """
    return html.escape("" if value is None else str(value), quote=True).replace("'", "&#39;")


@router.get("/html", response_class=HTMLResponse)
async def get_report_html(
    dataset_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Self-contained HTML report, downloadable and readable offline.

    No external assets: no CDN scripts, no remote fonts, no images. It has to be
    something a judge can keep and open later.

    Served with a Content-Security-Policy that forbids script execution outright.
    Every interpolated value is escaped by `_esc`, but this document mixes
    user-supplied machine names and CSV headers into markup on the API's own
    origin, so the policy is the backstop if one interpolation is ever missed.
    """
    target = None if not dataset_id else dataset_id
    report = await _build_report(db, target)
    scope, headline, quality = report["scope"], report["headline"], report["data_quality"]

    def badge(text: str, colour: str) -> str:
        return (
            f'<span style="padding:2px 7px;border-radius:4px;font-size:11px;'
            f'font-weight:700;color:{colour};background:{colour}22">{_esc(text)}</span>'
        )

    severity_colour = {
        "Critical": "#dc2626", "Warning": "#f59e0b",
        "Watch": "#3b82f6", "Normal": "#10b981",
    }
    confidence_colour = {"high": "#10b981", "medium": "#f59e0b", "low": "#dc2626"}

    rows_html = []
    for row in report["machines"]:
        disagreement = ""
        if row["signal_disagreement"]:
            disagreement = (
                '<div style="margin-top:6px;padding:7px 9px;border-radius:6px;'
                'background:#fef2f2;border:1px solid #fecaca;color:#991b1b;font-size:12px">'
                f'{_esc(row["signal_disagreement"]["message"])}</div>'
            )
        limits = ""
        if row["absolute_concerns"]:
            items = "; ".join(
                f"{_esc(c['sensor'])} {_esc(c['value'])} vs {_esc(c['threshold'])} ({_esc(c['basis'])})"
                for c in row["absolute_concerns"]
            )
            limits = (
                '<div style="margin-top:6px;padding:7px 9px;border-radius:6px;'
                'background:#fff7ed;border:1px solid #fed7aa;color:#9a3412;font-size:12px">'
                f'Exceeds published limits: {items}</div>'
            )
        coverage = ""
        if row["missing_sensors"]:
            coverage = (
                '<div style="margin-top:6px;padding:7px 9px;border-radius:6px;'
                'background:#f8fafc;border:1px solid #e2e8f0;color:#475569;font-size:12px">'
                f'No {_esc(", ".join(row["missing_sensors"]))} channel. ML score '
                'unavailable; assessed on deviation and published limits.</div>'
            )
        factors = ", ".join(
            _esc(f.get("label")) for f in (row["top_factors"] or []) if f.get("label")
        )
        rows_html.append(f"""
        <tr>
          <td><strong>{_esc(row['machine'])}</strong><br>
              <span style="color:#64748b;font-size:11px">{_esc(row['type'])} &middot;
              {_esc(row['readings'])} readings &middot; {_esc(row['data_origin'])}</span>
              {disagreement}{limits}{coverage}</td>
          <td style="text-align:right">{_esc(row['ml_risk_score'] if row['ml_risk_score'] is not None else 'n/a')}</td>
          <td style="text-align:right">{_esc(row['deviation_score'] if row['deviation_score'] is not None else '-')}</td>
          <td>{badge(row['severity'] or 'Normal', severity_colour.get(row['severity'] or 'Normal', '#64748b'))}</td>
          <td>{badge(row['confidence'] or 'n/a', confidence_colour.get(row['confidence'] or '', '#64748b'))}</td>
          <td style="font-size:11px;color:#475569">{_esc(row['reference_source'])}</td>
          <td style="font-size:11px;color:#475569">{factors or '-'}</td>
        </tr>""")

    limitations_html = "".join(f"<li>{_esc(n)}</li>" for n in report["limitations"])
    quality_issues = "".join(
        f"<li><strong>{_esc(i['severity'])}</strong> &middot; {_esc(i['check'])}: {_esc(i['detail'])}</li>"
        for i in quality["issues"][:15]
    ) or "<li>No issues found.</li>"

    html_document = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>PredictOps report - {_esc(scope['dataset_name'])}</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:32px;
   background:#f8fafc;color:#0f172a}}
 h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:15px;margin:26px 0 10px}}
 .sub{{color:#64748b;font-size:13px}}
 .cards{{display:flex;gap:12px;flex-wrap:wrap;margin:18px 0}}
 .card{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:12px 16px;min-width:150px}}
 .card .v{{font-size:22px;font-weight:800}} .card .l{{font-size:11px;color:#64748b;text-transform:uppercase}}
 table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden}}
 th{{text-align:left;font-size:10px;text-transform:uppercase;color:#64748b;padding:9px 11px;background:#f1f5f9}}
 td{{padding:10px 11px;border-top:1px solid #e2e8f0;font-size:13px;vertical-align:top}}
 ul{{font-size:12px;color:#334155;line-height:1.65}}
 .note{{background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:12px 14px;font-size:12px}}
</style></head><body>
<h1>Machine analysis report</h1>
<div class="sub">{_esc(scope['dataset_name'])} &middot; {_esc(scope['machine_count'])} machines
 &middot; {_esc(scope['total_readings'])} readings
 &middot; data quality: <strong>{_esc(quality['overall'])}</strong></div>

<div class="cards">
 <div class="card"><div class="l">Need attention</div><div class="v">{_esc(headline['machines_needing_attention'])}</div></div>
 <div class="card"><div class="l">Highest risk</div><div class="v" style="font-size:16px">{_esc(headline['highest_risk_machine'])}</div></div>
 <div class="card"><div class="l">Value at risk (modelled)</div><div class="v">${_esc(f"{headline['total_value_at_risk']:,.0f}")}</div></div>
 <div class="card"><div class="l">Signal disagreements</div><div class="v">{_esc(headline['signal_disagreements'])}</div></div>
 <div class="card"><div class="l">Over published limits</div><div class="v">{_esc(headline['machines_over_absolute_limits'])}</div></div>
 <div class="card"><div class="l">No ML score (partial sensors)</div><div class="v">{_esc(headline['machines_without_ml_score'])}</div></div>
</div>

<h2>Machines, worst first</h2>
<table><thead><tr>
 <th>Machine</th><th style="text-align:right">ML risk %</th><th style="text-align:right">Deviation</th>
 <th>Severity</th><th>Confidence</th><th>Reference</th><th>Top factors</th>
</tr></thead><tbody>{''.join(rows_html)}</tbody></table>

<h2>Data quality</h2>
<div class="sub" style="margin-bottom:8px">{_esc(quality['guidance'])}</div>
<ul>{quality_issues}</ul>

<h2>Basis and limitations</h2>
<div class="note"><ul style="margin:0;padding-left:18px">{limitations_html}</ul></div>

<p class="sub" style="margin-top:22px">
 ML risk comes from a GradientBoosting model trained on synthetic data. The deviation
 score is model-free arithmetic against each machine's own reference. Where the two
 disagree, prefer the deviation score.
</p>
</body></html>"""
    return HTMLResponse(
        content=html_document,
        headers={
            # Inline <style> is required by the no-external-assets rule; scripts,
            # frames and form submission are not needed at all and are denied.
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )
