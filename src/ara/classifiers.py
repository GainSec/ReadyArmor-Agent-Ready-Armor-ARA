"""Pluggable classifiers for CoT / content alignment checks.

A **classifier** takes an agent's output (prompt response, tool call,
chain-of-thought excerpt) plus session context and returns a verdict:
allow / flag / deny. Unlike Bulwark rules which match deterministically
on regex/glob patterns, classifiers encode fuzzier signals — goal-drift,
prompt-injection payloads, jailbreak syntax, credential-like strings —
and are typically model-backed.

This module ships:

1. A ``Classifier`` interface (``classify(sample) -> Verdict``).
2. A ``ClassifierRegistry`` that the MITM and supervisor can query.
3. A built-in ``GoalDriftHeuristic`` — a zero-dependency regex
   classifier that flags obvious goal-hijack phrasings. Useful as a
   baseline and for CI; production deployments should register a
   real model-backed classifier.

Model-backed classifier integration pattern (not shipped here;
adopters drop a subclass into their own code and register it at
boot)::

    class LlamaGuard(Classifier):
        name = "llama-guard"
        def __init__(self):
            self.model = ...  # transformers, llama.cpp, OpenAI API, …
        def classify(self, sample: Sample) -> Verdict:
            score = self.model.score(sample.content)
            return Verdict(
                action="deny" if score > 0.8 else "allow",
                confidence=score,
                reason=f"llama-guard score={score:.2f}",
            )

    from ara import classifiers
    classifiers.registry.register(LlamaGuard())

The MITM calls ``registry.classify_reply(content, session_id)`` on
every response body; any classifier returning a non-allow verdict
fires a ``classifier_hit`` audit event.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Optional, Protocol


@dataclass(frozen=True)
class Sample:
    """Input to a classifier."""
    scope: str          # "ask" | "reply" | "cot" | "tool_call"
    content: str
    session_id: Optional[str] = None
    tool_name: Optional[str] = None
    turn_index: Optional[int] = None


@dataclass(frozen=True)
class Verdict:
    action: str         # "allow" | "flag" | "deny"
    classifier: str     # name of the classifier that produced this verdict
    confidence: float = 0.0
    reason: str = ""
    matched_spans: tuple[tuple[int, int], ...] = ()


class Classifier(Protocol):
    name: str

    def classify(self, sample: Sample) -> Verdict: ...


class ClassifierRegistry:
    """Thread-safe registry of active classifiers."""

    def __init__(self) -> None:
        self._classifiers: list[Classifier] = []
        self._lock = threading.Lock()

    def register(self, clf: Classifier) -> None:
        with self._lock:
            self._classifiers.append(clf)

    def unregister(self, name: str) -> None:
        with self._lock:
            self._classifiers = [c for c in self._classifiers if c.name != name]

    def names(self) -> list[str]:
        with self._lock:
            return [c.name for c in self._classifiers]

    def classify(self, sample: Sample) -> list[Verdict]:
        """Run every registered classifier against ``sample``. Returns
        all verdicts with action != 'allow' (callers can still inspect
        the full list if needed)."""
        with self._lock:
            clfs = list(self._classifiers)
        out: list[Verdict] = []
        for clf in clfs:
            try:
                v = clf.classify(sample)
            except Exception:  # noqa: BLE001
                continue
            if v.action != "allow":
                out.append(v)
        return out

    def classify_reply(self, content: str, *, session_id: str | None = None) -> list[Verdict]:
        return self.classify(Sample(scope="reply", content=content, session_id=session_id))

    def classify_ask(self, content: str, *, session_id: str | None = None) -> list[Verdict]:
        return self.classify(Sample(scope="ask", content=content, session_id=session_id))

    def classify_cot(self, content: str, *, session_id: str | None = None) -> list[Verdict]:
        return self.classify(Sample(scope="cot", content=content, session_id=session_id))


# ---- built-in heuristic ------------------------------------------------

_GOAL_DRIFT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE | re.DOTALL) for p in (
        # The "ignore previous instructions" family.
        r"ignore\s+(?:the|all|any)?\s*(?:previous|prior|above|earlier|preceding)"
        r"\s+(?:instructions?|prompts?|messages?|directives?|rules?|system)",
        r"disregard\s+(?:the|all|any)?\s*(?:previous|prior|above|earlier)"
        r"\s+(?:instructions?|prompts?|context)",
        r"forget\s+(?:the|all|any)?\s*(?:previous|prior|above|earlier)"
        r"\s+(?:instructions?|prompts?|context|rules?)",
        # Role-swap attempts.
        r"you\s+are\s+now\s+(?:a|an)\s+(?:\w+\s+)?(?:assistant|agent|ai|bot)"
        r"\s+(?:without|with\s+no)\s+(?:restrictions?|filters?|limits?)",
        r"pretend\s+(?:to\s+be|you\s+are)\s+(?:a|an)?\s*(?:unrestricted|uncensored|dan\b)",
        # Credential / key exfiltration phrasing.
        r"(?:print|echo|output|reveal|show|display)\s+(?:the\s+)?"
        r"(?:api[_\s-]?key|secret|password|token|env|environment)",
        # Subversion of tool-call boundaries.
        r"skip\s+(?:the\s+)?approval\s+(?:prompt|dialog|step|check)",
        r"bypass\s+(?:the\s+)?(?:policy|bulwark|ara|firewall|guard)",
    )
)


class GoalDriftHeuristic:
    """Regex-based classifier for obvious goal-hijack phrasings.

    Zero-dependency baseline — trivially bypassed by minor paraphrasing
    (this is a known failure mode for regex-based PI detection; see
    Trendyol's documented LlamaFirewall bypasses). Treat as a smoke
    alarm, not a vault door.
    """

    name = "goal-drift-heuristic"

    def classify(self, sample: Sample) -> Verdict:
        if sample.scope not in ("ask", "reply", "cot"):
            return Verdict(action="allow", classifier=self.name)
        spans: list[tuple[int, int]] = []
        for pat in _GOAL_DRIFT_PATTERNS:
            for m in pat.finditer(sample.content):
                spans.append(m.span())
                if len(spans) >= 8:
                    break
            if len(spans) >= 8:
                break
        if not spans:
            return Verdict(action="allow", classifier=self.name)
        return Verdict(
            action="flag",
            classifier=self.name,
            confidence=min(1.0, 0.4 + 0.15 * len(spans)),
            reason=f"{len(spans)} goal-drift phrase(s) matched",
            matched_spans=tuple(spans),
        )


# Process-wide singleton registry. Adopters register classifiers at
# boot (from a plugin file, a config entry, or a wire-time call).
registry = ClassifierRegistry()
registry.register(GoalDriftHeuristic())
