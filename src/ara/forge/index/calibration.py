"""Calibration: forge-time gating + reporting.

Operator authors a list of CalibrationQuery; runner executes each
through the dispatcher; reports precision@5 + recall@10. Seal blocks if
mean precision@5 < threshold.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ara.forge.index.dispatcher import SearchDispatcher


@dataclass(frozen=True)
class CalibrationQuery:
    query: str
    expected_top_ids: list[str]
    kind: str | None = None
    min_precision_at_5: float = 0.6


@dataclass(frozen=True)
class CalibrationCaseResult:
    query: str
    expected: list[str]
    returned: list[str]
    precision_at_5: float
    recall_at_10: float
    passed: bool


@dataclass(frozen=True)
class CalibrationReport:
    weapon_id: str
    seal_id: str
    embedder: str
    cases: list[CalibrationCaseResult]
    queries_total: int
    queries_passed: int
    queries_failed: int
    mean_precision_at_5: float
    mean_recall_at_10: float
    seal_blocked: bool
    blocking_reasons: list[str] = field(default_factory=list)
    ran_at: float = 0.0
    elapsed_ms: float = 0.0


def precision_at_k(returned: list[str], expected: list[str], k: int) -> float:
    if k <= 0 or not returned:
        return 0.0
    top = returned[:k]
    expected_set = set(expected)
    hits = sum(1 for r in top if r in expected_set)
    return hits / float(k)


def recall_at_k(returned: list[str], expected: list[str], k: int) -> float:
    if not expected:
        return 1.0
    top = returned[:k]
    expected_set = set(expected)
    hits = sum(1 for r in top if r in expected_set)
    return hits / float(len(expected_set))


def run_calibration(
    dispatcher: SearchDispatcher,
    queries: list[CalibrationQuery],
    *,
    weapon_id: str,
    seal_id: str,
    embedder_name: str = "null",
    global_min_p_at_5: float = 0.6,
) -> CalibrationReport:
    t0 = time.time()
    cases: list[CalibrationCaseResult] = []
    blocking: list[str] = []
    for q in queries:
        resp = dispatcher.search(q.query, kind=q.kind, k=10)
        returned = [h.id for h in resp.hits]
        p5 = precision_at_k(returned, q.expected_top_ids, 5)
        r10 = recall_at_k(returned, q.expected_top_ids, 10)
        passed = p5 >= q.min_precision_at_5
        cases.append(CalibrationCaseResult(
            query=q.query, expected=list(q.expected_top_ids),
            returned=returned, precision_at_5=p5, recall_at_10=r10,
            passed=passed,
        ))
        if not passed:
            blocking.append(
                f"query {q.query!r}: precision@5={p5:.2f} < "
                f"required {q.min_precision_at_5:.2f}"
            )

    mean_p5 = (sum(c.precision_at_5 for c in cases) / len(cases)) if cases else 0.0
    mean_r10 = (sum(c.recall_at_10 for c in cases) / len(cases)) if cases else 0.0
    seal_blocked = mean_p5 < global_min_p_at_5 or bool(blocking)
    if seal_blocked and mean_p5 < global_min_p_at_5:
        blocking.append(
            f"mean precision@5 {mean_p5:.2f} < required {global_min_p_at_5:.2f}"
        )
    elapsed = (time.time() - t0) * 1000.0
    return CalibrationReport(
        weapon_id=weapon_id, seal_id=seal_id, embedder=embedder_name,
        cases=cases,
        queries_total=len(queries),
        queries_passed=sum(1 for c in cases if c.passed),
        queries_failed=sum(1 for c in cases if not c.passed),
        mean_precision_at_5=mean_p5, mean_recall_at_10=mean_r10,
        seal_blocked=seal_blocked, blocking_reasons=blocking,
        ran_at=time.time(), elapsed_ms=elapsed,
    )


def write_report(path: Path, report: CalibrationReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        **asdict(report),
        "cases": [asdict(c) for c in report.cases],
    }, sort_keys=True, indent=2, default=str))


def load_queries(path: Path) -> list[CalibrationQuery]:
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    items = data.get("queries", []) if isinstance(data, dict) else data
    return [CalibrationQuery(
        query=q["query"],
        expected_top_ids=list(q.get("expected_top_ids") or []),
        kind=q.get("kind"),
        min_precision_at_5=float(q.get("min_precision_at_5", 0.6)),
    ) for q in items]
