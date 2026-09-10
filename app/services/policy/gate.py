"""Turn findings into a pass/fail verdict."""

from __future__ import annotations

from app.core.models import Finding, GateLevel, Severity, Verdict


def evaluate(findings: list[Finding], gate: GateLevel) -> Verdict:
    if gate is GateLevel.NONE:
        return Verdict(
            passed=True,
            gate=gate,
            blocking_count=0,
            reason="Gate disabled; findings are advisory.",
        )

    threshold = Severity(gate.value)
    blocking = [f for f in findings if f.severity.rank >= threshold.rank]
    if not blocking:
        return Verdict(
            passed=True,
            gate=gate,
            blocking_count=0,
            reason=f"No findings at or above {gate.value}.",
        )

    counts: dict[str, int] = {}
    for finding in blocking:
        counts[finding.severity.value] = counts.get(finding.severity.value, 0) + 1
    detail = ", ".join(f"{count} {severity}" for severity, count in sorted(counts.items()))
    return Verdict(
        passed=False,
        gate=gate,
        blocking_count=len(blocking),
        reason=f"{detail} finding(s) at or above {gate.value}.",
    )
