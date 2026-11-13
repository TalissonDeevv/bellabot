from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable


class RecommendationEngine:
    """Organiza findings determinísticos em um plano de hardening audit-first."""

    PRIORITY = {
        "CRITICAL": "PRIORIDADE 1 — revisar imediatamente",
        "HIGH": "PRIORIDADE 1 — revisar imediatamente",
        "MEDIUM": "PRIORIDADE 2 — importante",
        "LOW": "PRIORIDADE 3 — melhoria recomendada",
        "INFO": "PRIORIDADE 3 — melhoria recomendada",
    }

    def hardening_plan(self, findings: Iterable[Any]) -> dict[str, list[dict[str, str]]]:
        plan: dict[str, list[dict[str, str]]] = defaultdict(list)
        for finding in findings:
            status = str(getattr(finding, "status", "OPEN"))
            if status in {"INTENTIONAL", "IGNORED_ONCE"}:
                continue
            severity = str(getattr(getattr(finding, "severity", "INFO"), "value", getattr(finding, "severity", "INFO")))
            priority = self.PRIORITY.get(severity, self.PRIORITY["INFO"])
            plan[priority].append(
                {
                    "finding_id": str(getattr(finding, "id", "")),
                    "problem": str(getattr(finding, "title", "Configuração para revisão")),
                    "reason": str(getattr(finding, "reason", "")),
                    "impact": str(getattr(finding, "description", "")),
                    "recommendation": str(getattr(finding, "recommendation", "")),
                    "resource": f"{getattr(finding, 'resource_type', 'resource')}:{getattr(finding, 'resource_id', '')}",
                }
            )
        return dict(plan)
