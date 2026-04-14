"""
Process Manager — spawn, track, health-check, and restart fleet agents.

Each agent runs as a subprocess.Popen instance.  The manager tracks PIDs,
captures stdout/stderr to per-agent log directories, and implements
exponential-backoff auto-restart.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.error import URLError
from urllib.request import urlopen

logger = logging.getLogger("superz.process")

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class AgentProcess:
    """Runtime state for a single managed agent."""

    name: str
    port: int
    command: str
    cwd: str
    subprocess: Optional[subprocess.Popen] = None
    pid: Optional[int] = None
    health_status: str = "DOWN"       # OK | WARN | ERR | DOWN
    last_heartbeat: Optional[float] = None
    restart_count: int = 0
    started_at: Optional[float] = None
    env: dict[str, str] = field(default_factory=dict)
    log_dir: Optional[str] = None

    @property
    def uptime(self) -> float:
        """Seconds since the agent was last started."""
        if self.started_at is None:
            return 0.0
        return time.time() - self.started_at


class ProcessManager:
    """Manage fleet agent subprocesses with health-checking and auto-restart.

    Parameters
    ----------
    base_instance_dir :
        Path to ``~/.superinstance/`` — used for log storage.
    max_restart_attempts :
        Maximum consecutive restart attempts before giving up.
    backoff_max :
        Upper bound (seconds) for exponential back-off.
    """

    def __init__(
        self,
        base_instance_dir: str | Path = "~/.superinstance",
        max_restart_attempts: int = 5,
        backoff_max: int = 60,
    ) -> None:
        self._base = Path(base_instance_dir).expanduser().resolve()
        self._max_restarts = max_restart_attempts
        self._backoff_max = backoff_max
        self._agents: dict[str, AgentProcess] = {}

    # ---- public API --------------------------------------------------------

    def register(
        self,
        name: str,
        command: str,
        port: int,
        cwd: str,
        env: Optional[dict[str, str]] = None,
    ) -> AgentProcess:
        """Register an agent without starting it."""
        log_dir = self._base / "logs" / name
        log_dir.mkdir(parents=True, exist_ok=True)
        proc = AgentProcess(
            name=name,
            port=port,
            command=command,
            cwd=str(cwd),
            env=env or {},
            log_dir=str(log_dir),
        )
        self._agents[name] = proc
        return proc

    def start_agent(self, name: str) -> Optional[int]:
        """Launch a registered agent subprocess; returns PID or *None*."""
        proc = self._agents.get(name)
        if proc is None:
            logger.error("Unknown agent: %s", name)
            return None

        if proc.subprocess is not None and proc.subprocess.poll() is None:
            logger.warning("%s is already running (pid %s)", name, proc.pid)
            return proc.pid

        # Ensure log directory exists
        log_path = Path(proc.log_dir) if proc.log_dir else None
        if log_path:
            log_path.mkdir(parents=True, exist_ok=True)

        stdout_fh = open(log_path / "stdout.log", "a") if log_path else subprocess.DEVNULL  # noqa: SIM115
        stderr_fh = open(log_path / "stderr.log", "a") if log_path else subprocess.DEVNULL  # noqa: SIM115

        merged_env = os.environ.copy()
        merged_env.update(proc.env)

        try:
            popen = subprocess.Popen(
                proc.command.split(),
                cwd=proc.cwd,
                stdout=stdout_fh,
                stderr=stderr_fh,
                env=merged_env,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            logger.error("Failed to start %s: %s", name, exc)
            proc.health_status = "ERR"
            if isinstance(stdout_fh, int):
                pass
            else:
                stdout_fh.close()
            if isinstance(stderr_fh, int):
                pass
            else:
                stderr_fh.close()
            return None
        except OSError as exc:
            logger.error("Failed to start %s: %s", name, exc)
            proc.health_status = "ERR"
            if not isinstance(stdout_fh, int):
                stdout_fh.close()
            if not isinstance(stderr_fh, int):
                stderr_fh.close()
            return None

        proc.subprocess = popen
        proc.pid = popen.pid
        proc.started_at = time.time()
        proc.health_status = "WARN"  # until first health-check succeeds
        proc.last_heartbeat = time.time()
        logger.info("Started %s (pid=%d) on port %d", name, proc.pid, proc.port)
        return proc.pid

    def stop_agent(self, name: str, timeout: float = 5.0) -> bool:
        """Gracefully stop an agent: SIGTERM → wait → SIGKILL."""
        proc = self._agents.get(name)
        if proc is None or proc.subprocess is None:
            logger.warning("Cannot stop %s — not tracked", name)
            return False

        pid = proc.pid
        logger.info("Stopping %s (pid=%d) …", name, pid)

        # Try SIGTERM first
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

        try:
            proc.subprocess.wait(timeout=timeout)
            logger.info("%s stopped gracefully", name)
        except subprocess.TimeoutExpired:
            logger.warning("%s did not stop in %.1fs — sending SIGKILL", name, timeout)
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                proc.subprocess.wait(timeout=3)
            except subprocess.TimeoutExpired:
                logger.error("%s refused to die", name)

        proc.health_status = "DOWN"
        proc.subprocess = None
        proc.pid = None
        proc.started_at = None
        return True

    def restart_agent(self, name: str) -> Optional[int]:
        """Stop then start an agent.  Returns new PID or *None*."""
        self.stop_agent(name, timeout=3)
        return self.start_agent(name)

    def check_health(self, name: str) -> str:
        """Return health status string: OK / WARN / ERR / DOWN.

        Strategy:
        1. If subprocess is gone → DOWN
        2. Try HTTP GET ``http://localhost:{port}/health`` → OK
        3. If subprocess alive but HTTP fails → WARN
        """
        proc = self._agents.get(name)
        if proc is None:
            return "DOWN"

        # Process still running?
        if proc.subprocess is not None and proc.subprocess.poll() is not None:
            proc.health_status = "DOWN"
            return "DOWN"

        # HTTP health check
        if proc.subprocess is not None:
            try:
                url = f"http://localhost:{proc.port}/health"
                with urlopen(url, timeout=3) as resp:
                    if resp.status < 400:
                        proc.health_status = "OK"
                        proc.last_heartbeat = time.time()
                        return "OK"
                    else:
                        proc.health_status = "ERR"
                        return "ERR"
            except (URLError, OSError, TimeoutError, ConnectionError):
                proc.health_status = "WARN"
                proc.last_heartbeat = time.time()
                return "WARN"
            except Exception:
                proc.health_status = "WARN"
                return "WARN"

        return "DOWN"

    def get_status(self) -> dict[str, dict[str, Any]]:
        """Snapshot of all agents' current status."""
        result: dict[str, dict[str, Any]] = {}
        for name, proc in self._agents.items():
            result[name] = {
                "name": proc.name,
                "port": proc.port,
                "pid": proc.pid,
                "status": proc.health_status,
                "uptime": round(proc.uptime, 1),
                "restart_count": proc.restart_count,
                "command": proc.command,
            }
        return result

    def stop_all(self) -> None:
        """Stop every agent in reverse registration order."""
        names = list(self._agents.keys())
        for name in reversed(names):
            self.stop_agent(name)
        logger.info("All agents stopped")

    def auto_restart(self, name: str) -> Optional[int]:
        """Attempt to restart a crashed agent with exponential back-off.

        Returns new PID if restarted, *None* if the agent has exceeded
        ``max_restart_attempts``.
        """
        proc = self._agents.get(name)
        if proc is None:
            return None

        if proc.restart_count >= self._max_restarts:
            logger.error(
                "%s has exceeded %d restart attempts — giving up",
                name, self._max_restarts,
            )
            return None

        backoff = min(2 ** proc.restart_count, self._backoff_max)
        logger.info("Restarting %s in %ds (attempt %d/%d)", name, backoff, proc.restart_count + 1, self._max_restarts)
        time.sleep(backoff)
        proc.restart_count += 1
        pid = self.start_agent(name)
        if pid is None:
            return self.auto_restart(name)
        return pid

    @property
    def agent_names(self) -> list[str]:
        return list(self._agents.keys())

    def get_agent(self, name: str) -> Optional[AgentProcess]:
        return self._agents.get(name)
