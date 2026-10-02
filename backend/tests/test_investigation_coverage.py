"""
Question coverage for the Investigation tab.

The defect these tests exist to prevent: the intent taxonomy used to be closed, with
`classify_intent_keywords` returning `fleet_summary` for anything it did not
recognise. Measured before the fix, 11 of 12 realistic questions came back as the
same fleet-statistics paragraph — fluent, grounded, and about a question nobody had
asked. A wrong-but-confident answer is worse than an admitted gap, so these tests
assert routing, and assert that answers never contradict themselves.

Gemini is forced off throughout. That is the honest default: the API quota can be
exhausted at any time, and the deterministic answer is then what an engineer reads.
"""
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import app.llm.investigate_service as inv
from app.database import AsyncSessionLocal, init_db
from app.llm.investigate_service import (
    INTENTS,
    UNSUPPORTED,
    classify_intent_keywords,
    deterministic_answer,
    investigate,
)
from app.main import app
from app.models.machine import Machine


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    """Exercise the deterministic path, which is what users get when no provider
    answers. Patches `llm_available` — the gate the service actually consults, not
    `gemini_available`, which no longer controls anything now that Cortex is the
    preferred provider."""
    monkeypatch.setattr(inv, "llm_available", lambda: False)


@pytest_asyncio.fixture
async def client():
    await init_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# The signature of the original bug: the fleet-summary paragraph.
FLEET_PARAGRAPH = "machines monitored. Open alerts:"


# ─── Routing ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("question,expected,machine_named", [
    # Machine facts — none of these had an intent before, so all became fleet stats.
    ("What is the current temperature of M-102?", "machine_facts", True),
    ("When was M-102 last maintained?", "machine_facts", True),
    ("How many readings do we have for CM-305?", "machine_facts", True),
    ("What is the baseline vibration for M-101?", "machine_facts", True),
    ("What is the install date of M-102?", "machine_facts", True),

    # The signals themselves.
    ("What is the deviation score for CM-305?", "signal_explain", True),
    ("How confident are you in the score for CM-305?", "signal_explain", True),
    ("Which machines exceed ISO vibration limits?", "signal_explain", False),
    ("Can I trust the score for M-102?", "signal_explain", True),

    # Provenance, coverage, quality.
    ("Is this real data or synthetic?", "data_coverage", False),
    ("Which machines have no maintenance records?", "data_coverage", False),
    ("Which uploaded machines are missing sensors?", "data_coverage", False),
    ("What is the data quality like?", "data_coverage", False),

    # Pre-existing intents must not regress.
    ("Why is M-102 at risk?", "why_at_risk", True),
    # Asking about the 0%-versus-Critical discrepancy routes to why_at_risk, which
    # is correct: that handler now leads with the signal that raised the severity.
    # `test_answer_explains_a_low_score_against_a_high_severity` covers the answer.
    ("Why is M-102 Critical when its risk score is 0%?", "why_at_risk", True),
    ("Which machines are trending worse?", "trending_up", False),
    ("Has this failure happened before?", "similar_past_failures", True),
    ("Do we have the parts to fix the critical alerts?", "part_readiness", False),
    ("What is our financial exposure right now?", "cost_exposure", False),
    ("Where are we losing the most OEE?", "oee_losses", False),
    ("Give me a fleet overview", "fleet_summary", False),
])
def test_questions_route_to_the_right_intent(question, expected, machine_named):
    assert classify_intent_keywords(question, machine_named=machine_named) == expected


@pytest.mark.parametrize("question", [
    "What is the weather in Paris?",
    "Tell me a joke",
    "Who won the football last night?",
])
def test_off_topic_questions_are_refused_not_answered_with_fleet_stats(question):
    """The old default made these return fleet statistics as though they were answers."""
    assert classify_intent_keywords(question, machine_named=False) == UNSUPPORTED


def test_naming_a_machine_alone_is_a_machine_question():
    assert classify_intent_keywords("M-102", machine_named=True) == "machine_facts"


def test_fleet_wording_about_one_machine_is_not_a_fleet_rollup():
    """'status' would previously have routed a single-machine question to the fleet."""
    assert classify_intent_keywords("What is the status of M-102?", machine_named=True) == "machine_facts"


# ─── Answers ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_question_silently_returns_the_fleet_summary(client):
    """The regression that mattered: 11 of 12 questions returning fleet stats."""
    questions = [
        "What is the current temperature of M-102?",
        "When was M-102 last maintained?",
        "Compare M-102 and M-103",
        "Which machines have no maintenance records?",
        "How many readings do we have for CM-305?",
        "What is the deviation score for CM-305?",
        "Which machines exceed ISO vibration limits?",
        "What is the baseline vibration for M-101?",
        "How confident are you in the score for CM-305?",
        "Is this real data or synthetic?",
        "Can I trust the score for M-102?",
        "What is the weather in Paris?",
    ]
    async with AsyncSessionLocal() as db:
        for question in questions:
            result = await investigate(db, question)
            assert FLEET_PARAGRAPH not in result["answer"], (
                f"{question!r} was answered with fleet statistics"
            )


@pytest.mark.asyncio
async def test_a_fleet_question_still_gets_the_fleet_summary(client):
    async with AsyncSessionLocal() as db:
        result = await investigate(db, "Give me a fleet overview")
    assert result["intent"] == "fleet_summary"
    assert FLEET_PARAGRAPH in result["answer"]


@pytest.mark.asyncio
async def test_answer_explains_a_low_score_against_a_high_severity(client):
    """A machine can be Critical on a near-zero ML score, because deviation or a
    published limit raised it. Reporting '0% failure risk (Critical)' reads as broken
    and buries the actual cause."""
    async with AsyncSessionLocal() as db:
        machines = (await db.execute(
            select(Machine).where(Machine.dataset_id.is_(None))
        )).scalars().all()

        checked = 0
        for machine in machines:
            result = await investigate(db, f"Why is {machine.name} at risk?")
            answer = result["answer"]
            if result["intent"] != "why_at_risk":
                continue
            # Only machines the answer asserts are currently flagged can contradict
            # themselves; "not currently flagged" plus a 0% score is consistent.
            if "not currently flagged" in answer:
                continue
            if not any(s in answer for s in ("Critical", "Warning")):
                continue
            checked += 1
            if any(f"{n}% failure risk" in answer for n in range(0, 25)):
                assert (
                    "model failing" in answer
                    or "understat" in answer
                    or "deviation" in answer
                    or "published limit" in answer
                ), f"unexplained contradiction for {machine.name}: {answer}"
        assert checked > 0, "no flagged machine was available to check"


@pytest.mark.asyncio
async def test_machine_facts_leads_with_what_was_asked(client):
    async with AsyncSessionLocal() as db:
        result = await investigate(db, "When was M-102 last maintained?")
    # The maintenance fact should come before the sensor dump, not after it.
    answer = result["answer"]
    assert result["intent"] == "machine_facts"
    assert "maintain" in answer.lower()
    assert answer.lower().index("maintain") < 80, answer


@pytest.mark.asyncio
async def test_comparison_uses_only_the_machines_named(client):
    """A machine with no display_name once matched every question, because
    `"" in text` is always True, dragging unrelated machines into the comparison."""
    async with AsyncSessionLocal() as db:
        result = await investigate(db, "Compare M-102 and M-103")
    assert result["intent"] == "comparison"
    assert "M-102" in result["answer"]
    assert "M-103" in result["answer"]
    assert "M-101" not in result["answer"]
    assert "M-104" not in result["answer"]
    assert len(result["evidence"]) == 2


@pytest.mark.asyncio
async def test_comparison_verdict_matches_its_own_evidence(client):
    """A plain max() on the top signal resolved ties to whichever machine was listed
    first, which declared M-102 worse than M-103 even though M-103's deviation was
    higher (80 vs 71). The verdict must follow the numbers shown.

    The ranking compares the three *scores* — absolute-limit severity, model-free
    deviation and ML risk — all on a 0-100 scale. An earlier version of this test
    substituted the limit-breach count for the absolute score, which ranked
    "2 breaches" below any non-zero score and disagreed with the application
    whenever the top two signals tied.
    """
    async with AsyncSessionLocal() as db:
        result = await investigate(db, "Compare M-102 and M-103")

    rows = result["evidence"]
    assert len(rows) == 2
    answer = result["answer"]

    def strength(row):
        return sorted(
            (
                row["absolute_score"] or 0.0,
                row["deviation"] or 0.0,
                row["ml_risk"] or 0.0,
            ),
            reverse=True,
        ) + [row["limit_breaches"] or 0]

    ranked = sorted(rows, key=strength, reverse=True)
    if strength(ranked[0]) != strength(ranked[1]):
        assert f"{ranked[0]['machine']} is the worse" in answer, answer
    else:
        assert "equally bad" in answer, answer


@pytest.mark.asyncio
async def test_comparison_names_the_signal_that_decided_it(client):
    """The deciding number can be one the summary line does not print.

    A machine at ML 0% can outrank one at ML 100% because its absolute-limit
    severity is higher. Saying only "on the strongest signal" makes that look
    like the verdict contradicts its own evidence, so the basis is named.
    """
    async with AsyncSessionLocal() as db:
        result = await investigate(db, "Compare M-102 and M-103")

    answer = result["answer"]
    if "equally bad" in answer:
        pytest.skip("machines are tied on every signal; no deciding basis exists")

    assert any(
        basis in answer
        for basis in ("published-limit severity", "model-free deviation", "ML risk")
    ), answer
    # Both sides of the comparison are quoted, so the claim is checkable.
    assert " vs " in answer, answer


@pytest.mark.asyncio
async def test_comparison_evidence_carries_every_ranked_signal(client):
    """Whatever the ranking uses has to be in the evidence, or the answer cannot
    be audited against the numbers it was given."""
    async with AsyncSessionLocal() as db:
        result = await investigate(db, "Compare M-102 and M-103")

    for row in result["evidence"]:
        for field in ("ml_risk", "deviation", "absolute_score", "limit_breaches"):
            assert field in row, f"{field} missing from {row}"


@pytest.mark.asyncio
async def test_signal_explain_cites_the_published_limit(client):
    async with AsyncSessionLocal() as db:
        result = await investigate(db, "Which machines exceed ISO vibration limits?")
    assert result["intent"] == "signal_explain"
    # The basis has to be citable, not asserted.
    assert "ISO" in result["answer"] or "no machine currently breaches" in result["answer"].lower()


@pytest.mark.asyncio
async def test_confidence_reasons_are_not_phrased_as_justifying_high_confidence(client):
    """The listed reasons are what REDUCED confidence, so 'high because no
    maintenance history' states the opposite of the truth."""
    async with AsyncSessionLocal() as db:
        result = await investigate(db, "How confident are you in the score for CM-305?")
    answer = result["answer"]
    assert "high, because" not in answer
    assert "medium, because" not in answer


@pytest.mark.asyncio
async def test_data_coverage_states_provenance(client):
    async with AsyncSessionLocal() as db:
        result = await investigate(db, "Is this real data or synthetic?")
    assert result["intent"] == "data_coverage"
    assert "synthetic" in result["answer"].lower() or "simulator" in result["answer"].lower()


@pytest.mark.asyncio
async def test_unsupported_says_what_it_can_answer(client):
    async with AsyncSessionLocal() as db:
        result = await investigate(db, "What is the weather in Paris?")
    assert result["intent"] == UNSUPPORTED
    answer = result["answer"].lower()
    assert "can't answer" in answer or "cannot answer" in answer
    # It must be useful about the gap, not merely refuse.
    assert "machine" in answer


@pytest.mark.asyncio
async def test_every_intent_has_a_deterministic_answer():
    """Gemini quota can be exhausted at any time, so no intent may depend on it."""
    for intent in INTENTS:
        answer = deterministic_answer(intent, {})
        assert isinstance(answer, str) and answer.strip(), intent


@pytest.mark.asyncio
async def test_answers_are_grounded_and_carry_evidence(client):
    async with AsyncSessionLocal() as db:
        for question in [
            "Why is M-102 at risk?",
            "Compare M-102 and M-103",
            "What is the deviation score for CM-305?",
            "What is the current temperature of M-102?",
        ]:
            result = await investigate(db, question)
            assert result["evidence"], f"{question!r} produced no evidence rows"


@pytest.mark.asyncio
async def test_route_is_reported_so_answers_can_be_audited(client):
    response = await client.post("/api/investigate", json={"question": "Why is M-102 at risk?"})
    body = response.json()
    assert body["intent"] == "why_at_risk"
    assert body["intent_source"] in ("keywords", "gemini")
    assert body["source"] in ("deterministic", "gemini", "cached")


@pytest.mark.asyncio
async def test_suggestions_cover_the_new_capabilities(client):
    body = (await client.get("/api/investigate/suggestions")).json()
    joined = " ".join(body["questions"]).lower()
    assert "confident" in joined or "trust" in joined
    assert "compare" in joined
    assert set(body["intents"]) == set(INTENTS)
