from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .monitor import MonitorAlert
from .state import StateStore


@dataclass(frozen=True)
class IncidentEvent:
    incident_id: str
    scope: str
    kind: str
    alert: MonitorAlert
    first_seen: float
    occurred_at: float


class IncidentManager:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    async def evaluate(
        self,
        scope: str,
        alerts: Iterable[MonitorAlert],
        reminder_seconds: int,
    ) -> list[IncidentEvent]:
        now = time.time()
        active_alerts = {alert.key: alert for alert in alerts}
        events: list[IncidentEvent] = []

        def mutate(incidents: dict[str, Any]) -> None:
            for alert_key, alert in active_alerts.items():
                incident_id = f"{scope}:{alert_key}"
                previous = incidents.get(incident_id)
                if not isinstance(previous, dict) or not previous.get("active"):
                    previous = {
                        "scope": scope,
                        "active": True,
                        "first_seen": now,
                        "last_seen": now,
                        "last_notified": 0.0,
                        "recovery_notified": False,
                        "alert": asdict(alert),
                    }
                    incidents[incident_id] = previous
                    kind = "opened"
                else:
                    previous["last_seen"] = now
                    previous["alert"] = asdict(alert)
                    last_notified = float(previous.get("last_notified", 0.0))
                    if last_notified <= 0:
                        kind = "opened"
                    elif (
                        reminder_seconds > 0 and now - last_notified >= reminder_seconds
                    ):
                        kind = "reminder"
                    else:
                        kind = ""

                if kind:
                    events.append(
                        IncidentEvent(
                            incident_id=incident_id,
                            scope=scope,
                            kind=kind,
                            alert=alert,
                            first_seen=float(previous["first_seen"]),
                            occurred_at=now,
                        )
                    )

            prefix = f"{scope}:"
            for incident_id, incident in list(incidents.items()):
                if not incident_id.startswith(prefix) or not isinstance(incident, dict):
                    continue
                alert_key = incident_id[len(prefix) :]
                if alert_key in active_alerts:
                    continue
                if incident.get("active"):
                    incident["active"] = False
                    incident["recovered_at"] = now
                    incident["recovery_notified"] = False
                elif incident.get("recovery_notified"):
                    continue
                alert_data = incident.get("alert", {})
                try:
                    alert = MonitorAlert(**alert_data)
                except (TypeError, ValueError):
                    alert = MonitorAlert(
                        key=alert_key,
                        title="Incidente encerrado",
                        description="A condicao de alerta nao foi encontrada na verificacao atual.",
                    )
                events.append(
                    IncidentEvent(
                        incident_id=incident_id,
                        scope=scope,
                        kind="recovered",
                        alert=alert,
                        first_seen=float(incident.get("first_seen", now)),
                        occurred_at=now,
                    )
                )

            retention_cutoff = now - (30 * 24 * 60 * 60)
            for incident_id, incident in list(incidents.items()):
                if not isinstance(incident, dict) or incident.get("active"):
                    continue
                recovered_at = float(incident.get("recovered_at", now))
                if (
                    incident.get("recovery_notified")
                    and recovered_at < retention_cutoff
                ):
                    del incidents[incident_id]

        await self.state.mutate_section("incidents", mutate)
        if events:
            await self._record_history(events)
        return events

    async def mark_notified(self, event: IncidentEvent) -> None:
        now = time.time()

        def mutate(incidents: dict[str, Any]) -> None:
            incident = incidents.get(event.incident_id)
            if not isinstance(incident, dict):
                return
            if event.kind == "recovered":
                incident["recovery_notified"] = True
            else:
                incident["last_notified"] = now

        await self.state.mutate_section("incidents", mutate)

    async def active_count(self) -> int:
        incidents = await self.state.get_section("incidents")
        return sum(
            1
            for incident in incidents.values()
            if isinstance(incident, dict) and incident.get("active")
        )

    async def history_counts(self, days: int = 7) -> dict[str, int]:
        history = await self.state.get_section("incident_history")
        cutoff = time.time() - (days * 24 * 60 * 60)
        counts = {"opened": 0, "reminder": 0, "recovered": 0}
        for item in history.values():
            if not isinstance(item, dict):
                continue
            if float(item.get("occurred_at", 0)) < cutoff:
                continue
            kind = str(item.get("kind", ""))
            if kind in counts:
                counts[kind] += 1
        return counts

    async def _record_history(self, events: list[IncidentEvent]) -> None:
        def mutate(history: dict[str, Any]) -> None:
            for event in events:
                key = (
                    f"{int(event.occurred_at * 1000)}:{event.incident_id}:{event.kind}"
                )
                history[key] = {
                    "incident_id": event.incident_id,
                    "scope": event.scope,
                    "kind": event.kind,
                    "title": event.alert.title,
                    "occurred_at": event.occurred_at,
                }
            cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).timestamp()
            for key, item in list(history.items()):
                if not isinstance(item, dict):
                    del history[key]
                    continue
                if float(item.get("occurred_at", 0)) < cutoff:
                    del history[key]

        await self.state.mutate_section("incident_history", mutate)
