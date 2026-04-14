"""
Fleet Health Monitor for SuperZ Runtime.

Aggregates health status from all fleet agents, computes fleet-wide scores,
maintains historical data, and generates status reports.
"""

from __future__ import annotations

import json
import time
import logging
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class HealthSnapshot:
    """A single health data point for an agent."""
    agent_name: str
    status: str                          # healthy | degraded | unhealthy | unknown
    response_time_ms: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)
    error: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d


@dataclass
class FleetHealthReport:
    """Aggregated health report for the entire fleet."""
    total_agents: int = 0
    healthy_count: int = 0
    degraded_count: int = 0
    unhealthy_count: int = 0
    unknown_count: int = 0
    health_score: float = 0.0           # 0.0–100.0
    timestamp: datetime = field(default_factory=datetime.now)
    agents: dict[str, HealthSnapshot] = field(default_factory=dict)
    uptime_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        d["agents"] = {k: v.to_dict() for k, v in self.agents.items()}
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


# ---------------------------------------------------------------------------
# Health Monitor
# ---------------------------------------------------------------------------

class HealthMonitor:
    """Aggregates health status from all fleet agents.

    Polls each agent's ``/health`` endpoint (HTTP GET), tracks uptime,
    computes a fleet-wide health score, and maintains historical data.

    Usage::

        monitor = HealthMonitor()
        monitor.add_agent("keeper", "http://127.0.0.1:8443/health")
        report = monitor.check_all()
        print(report.to_json())
    """

    # How many historical data points to keep per agent
    MAX_HISTORY = 1000

    # HTTP timeout for health checks (seconds)
    HEALTH_TIMEOUT = 5

    def __init__(
        self,
        health_timeout: int = HEALTH_TIMEOUT,
        max_history: int = MAX_HISTORY,
    ) -> None:
        self.health_timeout = health_timeout
        self.max_history = max_history
        self._endpoints: dict[str, str] = {}           # name -> url
        self._history: dict[str, list[HealthSnapshot]] = {}
        self._last_report: Optional[FleetHealthReport] = None
        self._started_at: datetime = datetime.now()
        self._lock = threading.Lock()
        self._error_counts: dict[str, int] = {}
        self._total_tests_passing: int = 0

    # ------------------------------------------------------------------
    # Agent registration
    # ------------------------------------------------------------------

    def add_agent(self, name: str, url: str) -> None:
        """Register an agent for health monitoring."""
        with self._lock:
            self._endpoints[name] = url.rstrip("/")
            if name not in self._history:
                self._history[name] = []
            logger.debug("Registered health endpoint: %s -> %s", name, url)

    def remove_agent(self, name: str) -> None:
        """Unregister an agent."""
        with self._lock:
            self._endpoints.pop(name, None)

    def set_total_tests(self, count: int) -> None:
        """Set the total passing test count (for display)."""
        self._total_tests_passing = count

    # ------------------------------------------------------------------
    # Health checks
    # ------------------------------------------------------------------

    def check_agent(self, name: str) -> HealthSnapshot:
        """Check health of a single agent via HTTP GET.

        Returns a :class:`HealthSnapshot` with status and timing.
        """
        url = self._endpoints.get(name, "")
        if not url:
            return HealthSnapshot(agent_name=name, status="unknown", error="No endpoint registered")

        start = time.monotonic()
        try:
            req = Request(url, method="GET")
            req.add_header("Accept", "application/json")
            with urlopen(req, timeout=self.health_timeout) as resp:
                elapsed_ms = (time.monotonic() - start) * 1000
                body = resp.read().decode("utf-8", errors="replace")

                # Try to parse JSON response
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {"raw": body}

                status_code = resp.status
                if status_code == 200:
                    snapshot = HealthSnapshot(
                        agent_name=name,
                        status="healthy",
                        response_time_ms=round(elapsed_ms, 1),
                        details=data,
                    )
                    # Reset error count on success
                    self._error_counts[name] = 0
                    return snapshot
                else:
                    snapshot = HealthSnapshot(
                        agent_name=name,
                        status="degraded",
                        response_time_ms=round(elapsed_ms, 1),
                        error=f"HTTP {status_code}",
                        details=data,
                    )
                    return snapshot

        except HTTPError as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            return HealthSnapshot(
                agent_name=name,
                status="unhealthy",
                response_time_ms=round(elapsed_ms, 1),
                error=f"HTTP {exc.code}: {exc.reason}",
            )
        except (URLError, OSError, TimeoutError) as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            return HealthSnapshot(
                agent_name=name,
                status="unhealthy",
                response_time_ms=round(elapsed_ms, 1),
                error=str(exc),
            )
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.exception("Unexpected health check error for %s", name)
            return HealthSnapshot(
                agent_name=name,
                status="unhealthy",
                response_time_ms=round(elapsed_ms, 1),
                error=f"Unexpected: {exc}",
            )

    def check_all(self) -> FleetHealthReport:
        """Check health of all registered agents and return a fleet report."""
        with self._lock:
            snapshots: dict[str, HealthSnapshot] = {}
            for name in list(self._endpoints.keys()):
                snapshot = self.check_agent(name)
                snapshots[name] = snapshot
                self._record_snapshot(snapshot)

            report = self._build_report(snapshots)
            self._last_report = report
            return report

    def check_all_async(self, callback: Any) -> None:
        """Run health checks in a background thread and call *callback* with the report."""
        def _worker() -> None:
            report = self.check_all()
            try:
                callback(report)
            except Exception as exc:
                logger.warning("Health check callback error: %s", exc)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def _build_report(self, snapshots: dict[str, HealthSnapshot]) -> FleetHealthReport:
        """Build a FleetHealthReport from agent snapshots."""
        total = len(snapshots)
        healthy = sum(1 for s in snapshots.values() if s.status == "healthy")
        degraded = sum(1 for s in snapshots.values() if s.status == "degraded")
        unhealthy = sum(1 for s in snapshots.values() if s.status == "unhealthy")
        unknown = total - healthy - degraded - unhealthy

        # Health score: healthy=100, degraded=50, unhealthy=0, unknown=25
        if total > 0:
            score = (
                healthy * 100
                + degraded * 50
                + unhealthy * 0
                + unknown * 25
            ) / total
        else:
            score = 100.0

        return FleetHealthReport(
            total_agents=total,
            healthy_count=healthy,
            degraded_count=degraded,
            unhealthy_count=unhealthy,
            unknown_count=unknown,
            health_score=round(score, 1),
            agents=snapshots,
            uptime_seconds=(datetime.now() - self._started_at).total_seconds(),
        )

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def _record_snapshot(self, snapshot: HealthSnapshot) -> None:
        """Append a snapshot to the history buffer."""
        name = snapshot.agent_name
        if name not in self._history:
            self._history[name] = []
        self._history[name].append(snapshot)

        # Trim to max history
        if len(self._history[name]) > self.max_history:
            self._history[name] = self._history[name][-self.max_history:]

    def get_history(self, name: str) -> list[HealthSnapshot]:
        """Return historical health data for an agent."""
        return list(self._history.get(name, []))

    def get_last_report(self) -> Optional[FleetHealthReport]:
        """Return the most recent fleet health report (or None)."""
        return self._last_report

    # ------------------------------------------------------------------
    # Alerting
    # ------------------------------------------------------------------

    def check_alerts(self) -> list[str]:
        """Return a list of alert messages for agents that need attention."""
        alerts: list[str] = []
        report = self._last_report
        if report is None:
            return alerts

        for name, snapshot in report.agents.items():
            # Track consecutive errors
            if snapshot.status == "unhealthy":
                self._error_counts[name] = self._error_counts.get(name, 0) + 1
                count = self._error_counts[name]
                if count >= 3:
                    alerts.append(
                        f"CRITICAL: {name} has been unhealthy for {count} consecutive checks "
                        f"(error: {snapshot.error})"
                    )
                elif count == 1:
                    alerts.append(
                        f"WARNING: {name} is unhealthy (error: {snapshot.error})"
                    )
            elif snapshot.status == "degraded":
                alerts.append(
                    f"WARNING: {name} is degraded (response: {snapshot.response_time_ms}ms)"
                )

        return alerts

    # ------------------------------------------------------------------
    # Human-readable report
    # ------------------------------------------------------------------

    def format_report(self, report: Optional[FleetHealthReport] = None) -> str:
        """Format a fleet health report as a human-readable string."""
        report = report or self._last_report
        if report is None:
            return "No health data available yet."

        lines: list[str] = []
        lines.append(f"Fleet Health: {report.healthy_count}/{report.total_agents} OK "
                      f"| Score: {report.health_score:.0f}/100")

        uptime_str = self._format_duration(report.uptime_seconds)
        lines.append(f"Uptime: {uptime_str} | Total Tests: {self._total_tests_passing} passing")

        for name, snapshot in report.agents.items():
            status_icon = {"healthy": "[OK]", "degraded": "[!!]", "unhealthy": "[XX]", "unknown": "[??]"}
            icon = status_icon.get(snapshot.status, "[??]")
            rt = f"{snapshot.response_time_ms:.0f}ms" if snapshot.response_time_ms > 0 else "—"
            err = f" | {snapshot.error}" if snapshot.error else ""
            lines.append(f"  {icon} {name:20s} {rt:>8s}{err}")

        return "\n".join(lines)

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Format seconds as HH:MM:SS or MM:SS."""
        seconds = int(seconds)
        if seconds < 0:
            seconds = 0
        h, remainder = divmod(seconds, 3600)
        m, s = divmod(remainder, 60)
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"
