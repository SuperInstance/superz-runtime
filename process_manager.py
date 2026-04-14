"""
Fleet Process Manager for SuperZ Runtime.

Manages fleet agent subprocesses — start, stop, restart, health check,
auto-restart with exponential backoff, log rotation, and graceful shutdown.
"""

from __future__ import annotations

import os
import sys
import time
import signal
import logging
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent Process Model
# ---------------------------------------------------------------------------

@dataclass
class AgentProcess:
    """Tracks a single fleet agent subprocess."""
    name: str
    port: int = 0
    config: dict[str, Any] = field(default_factory=dict)
    process: Optional[subprocess.Popen] = None
    pid: Optional[int] = None
    health_status: str = "unknown"     # unknown | starting | healthy | degraded | crashed | stopped
    last_heartbeat: Optional[datetime] = None
    started_at: Optional[datetime] = None
    last_restart: Optional[datetime] = None
    restart_count: int = 0
    stdout_path: Optional[Path] = None
    stderr_path: Optional[Path] = None
    cwd: str = ""
    cmd: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

    @property
    def uptime(self) -> timedelta:
        """How long the agent has been running."""
        if self.started_at is None:
            return timedelta(0)
        end = datetime.now() if self.health_status in ("healthy", "starting", "degraded") else (self.last_restart or datetime.now())
        return max(end - self.started_at, timedelta(0))

    @property
    def is_running(self) -> bool:
        """Check if the underlying process is alive."""
        if self.process is None:
            return False
        return self.process.poll() is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "port": self.port,
            "pid": self.pid,
            "health_status": self.health_status,
            "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "restart_count": self.restart_count,
            "is_running": self.is_running,
            "uptime_seconds": self.uptime.total_seconds(),
        }


# ---------------------------------------------------------------------------
# Log Rotator
# ---------------------------------------------------------------------------

class LogRotator:
    """Simple log rotation: keep last N log files per agent."""

    MAX_LOGS = 5
    MAX_BYTES = 10 * 1024 * 1024  # 10 MB

    def __init__(self, logs_dir: Path, max_logs: int = MAX_LOGS, max_bytes: int = MAX_BYTES) -> None:
        self.logs_dir = logs_dir
        self.max_logs = max_logs
        self.max_bytes = max_bytes
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def get_log_path(self, agent_name: str, stream: str) -> Path:
        """Return the current log path for an agent's stream."""
        return self.logs_dir / f"{agent_name}.{stream}.log"

    def rotate_if_needed(self, agent_name: str, stream: str) -> Path:
        """Rotate log file if it exceeds max size."""
        log_path = self.get_log_path(agent_name, stream)
        if log_path.exists() and log_path.stat().st_size >= self.max_bytes:
            self._rotate(agent_name, stream)
        return log_path

    def _rotate(self, agent_name: str, stream: str) -> None:
        """Shift log files: .log.4 -> deleted, .log.3 -> .log.4, etc."""
        log_path = self.get_log_path(agent_name, stream)
        for i in range(self.max_logs - 1, 0, -1):
            older = self.logs_dir / f"{agent_name}.{stream}.log.{i}"
            newer = self.logs_dir / f"{agent_name}.{stream}.log.{i + 1}"
            if older.exists():
                older.rename(newer)
        # Move current to .log.1
        rotated = self.logs_dir / f"{agent_name}.{stream}.log.1"
        if log_path.exists():
            log_path.rename(rotated)

    def cleanup_old_logs(self) -> None:
        """Remove log files beyond the retention limit."""
        for log_file in self.logs_dir.glob("*.log.*"):
            try:
                parts = log_file.name.split(".log.")
                if len(parts) == 2:
                    idx = int(parts[1])
                    if idx > self.max_logs:
                        log_file.unlink()
                        logger.debug("Removed old log: %s", log_file)
            except (ValueError, IndexError):
                pass


# ---------------------------------------------------------------------------
# Process Manager
# ---------------------------------------------------------------------------

class ProcessManager:
    """Manages fleet agent subprocesses — start, stop, restart, health check.

    Each agent runs as a child process with stdout/stderr captured to log files.
    Crashed agents are automatically restarted with exponential backoff.
    """

    def __init__(
        self,
        logs_dir: Path,
        max_backoff: int = 60,
        shutdown_timeout: int = 5,
    ) -> None:
        self.logs_dir = logs_dir
        self.max_backoff = max_backoff
        self.shutdown_timeout = shutdown_timeout
        self.processes: dict[str, AgentProcess] = {}
        self.rotator = LogRotator(logs_dir)
        self._lock = threading.Lock()
        self._restart_timers: dict[str, threading.Timer] = {}
        self._on_status_change: Optional[Callable[[str, str, str], None]] = None
        self._shutting_down = False

    def set_status_callback(self, callback: Callable[[str, str, str], None]) -> None:
        """Register a callback for status changes: (name, old_status, new_status)."""
        self._on_status_change = callback

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    def start_agent(
        self,
        name: str,
        cmd: list[str],
        cwd: str = "",
        env: Optional[dict[str, str]] = None,
        port: int = 0,
        config: Optional[dict[str, Any]] = None,
    ) -> AgentProcess:
        """Start an agent as a subprocess and track it."""
        with self._lock:
            # Stop existing if running
            if name in self.processes and self.processes[name].is_running:
                self.stop_agent(name)

            stdout_path = self.rotator.rotate_if_needed(name, "stdout")
            stderr_path = self.rotator.rotate_if_needed(name, "stderr")

            process_env = os.environ.copy()
            if env:
                process_env.update(env)

            agent_proc = AgentProcess(
                name=name,
                port=port,
                config=config or {},
                cwd=cwd or os.getcwd(),
                cmd=cmd,
                env=process_env,
                health_status="starting",
                started_at=datetime.now(),
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                restart_count=0,
            )

            try:
                stdout_fh = open(stdout_path, "a")  # noqa: SIM115
                stderr_fh = open(stderr_path, "a")  # noqa: SIM115
                agent_proc.process = subprocess.Popen(
                    cmd,
                    cwd=agent_proc.cwd,
                    env=process_env,
                    stdout=stdout_fh,
                    stderr=stderr_fh,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
                agent_proc.pid = agent_proc.process.pid
                logger.info("Started %s (PID %d) on port %d", name, agent_proc.pid, port)
            except (OSError, subprocess.SubprocessError) as exc:
                logger.error("Failed to start %s: %s", name, exc)
                agent_proc.health_status = "crashed"
                # Close file handles on failure
                for fh in (stdout_fh, stderr_fh):
                    try:
                        fh.close()
                    except Exception:
                        pass

            self.processes[name] = agent_proc
            return agent_proc

    def stop_agent(self, name: str, timeout: Optional[int] = None) -> bool:
        """Gracefully stop an agent.  Returns True if it stopped cleanly."""
        with self._lock:
            agent = self.processes.get(name)
            if agent is None or not agent.is_running:
                return True

            # Cancel pending restart timer
            if name in self._restart_timers:
                self._restart_timers[name].cancel()
                del self._restart_timers[name]

            old_status = agent.health_status
            timeout = timeout or self.shutdown_timeout
            pid = agent.process.pid if agent.process else None

            logger.info("Stopping %s (PID %d, timeout %ds)", name, pid, timeout)

            try:
                if agent.process:
                    # Send SIGTERM
                    agent.process.terminate()
                    try:
                        agent.process.wait(timeout=timeout)
                        logger.info("%s stopped cleanly", name)
                    except subprocess.TimeoutExpired:
                        # Force SIGKILL
                        logger.warning("%s did not stop in %ds — sending SIGKILL", name, timeout)
                        agent.process.kill()
                        agent.process.wait(timeout=3)
                        logger.info("%s killed", name)
            except (OSError, ProcessLookupError):
                pass

            self._set_status(agent, "stopped")
            return True

    def stop_all(self) -> None:
        """Gracefully stop all tracked agents."""
        self._shutting_down = True
        logger.info("Stopping all %d agents...", len(self.processes))

        # Cancel all restart timers
        for name, timer in self._restart_timers.items():
            timer.cancel()
        self._restart_timers.clear()

        # Stop each agent
        for name in list(self.processes.keys()):
            self.stop_agent(name)

        self.rotator.cleanup_old_logs()
        logger.info("All agents stopped")

    # ------------------------------------------------------------------
    # Restart with backoff
    # ------------------------------------------------------------------

    def schedule_restart(self, name: str) -> None:
        """Schedule an automatic restart with exponential backoff."""
        if self._shutting_down:
            return

        agent = self.processes.get(name)
        if agent is None:
            return

        backoff = min(2 ** agent.restart_count, self.max_backoff)
        agent.restart_count += 1
        agent.last_restart = datetime.now()

        logger.info(
            "Scheduling restart of %s in %ds (attempt #%d)",
            name, backoff, agent.restart_count,
        )

        timer = threading.Timer(backoff, self._do_restart, args=(name,))
        self._restart_timers[name] = timer
        timer.daemon = True
        timer.start()

    def _do_restart(self, name: str) -> None:
        """Execute a restart for a crashed agent."""
        if self._shutting_down:
            return
        agent = self.processes.get(name)
        if agent is None:
            return

        logger.info("Restarting %s...", name)

        # Close old file handles
        if agent.process:
            try:
                agent.process.stdout.close()  # type: ignore[union-attr]
                agent.process.stderr.close()  # type: ignore[union-attr]
            except Exception:
                pass

        new_agent = self.start_agent(
            name=name,
            cmd=agent.cmd,
            cwd=agent.cwd,
            env=agent.env,
            port=agent.port,
            config=agent.config,
        )
        new_agent.restart_count = agent.restart_count

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def _set_status(self, agent: AgentProcess, new_status: str) -> None:
        """Update agent status and notify callback."""
        old = agent.health_status
        agent.health_status = new_status
        if new_status == "healthy":
            agent.last_heartbeat = datetime.now()
        if self._on_status_change and old != new_status:
            try:
                self._on_status_change(agent.name, old, new_status)
            except Exception as exc:
                logger.warning("Status callback error: %s", exc)

    def set_healthy(self, name: str) -> None:
        agent = self.processes.get(name)
        if agent:
            self._set_status(agent, "healthy")

    def set_degraded(self, name: str) -> None:
        agent = self.processes.get(name)
        if agent:
            self._set_status(agent, "degraded")

    def set_crashed(self, name: str) -> None:
        agent = self.processes.get(name)
        if agent:
            self._set_status(agent, "crashed")
            if not self._shutting_down:
                self.schedule_restart(name)

    def get_all(self) -> dict[str, AgentProcess]:
        """Return a snapshot of all tracked processes."""
        return dict(self.processes)

    def get_running(self) -> dict[str, AgentProcess]:
        """Return only currently-running processes."""
        return {n: p for n, p in self.processes.items() if p.is_running}

    def check_and_restart_dead(self) -> list[str]:
        """Poll all processes and auto-restart any that have died.
        Returns list of agent names that were restarted."""
        restarted: list[str] = []
        for name, agent in self.processes.items():
            if agent.health_status in ("healthy", "starting", "degraded") and not agent.is_running:
                if agent.process:
                    code = agent.process.poll()
                    logger.warning(
                        "%s died with exit code %d",
                        name, code if code is not None else "unknown",
                    )
                self.set_crashed(name)
                restarted.append(name)
        return restarted

    # ------------------------------------------------------------------
    # PID file management
    # ------------------------------------------------------------------

    def write_pid_file(self, path: Path) -> None:
        """Write the runtime's own PID to a file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(os.getpid()))
        logger.debug("PID file written: %s", path)

    @staticmethod
    def read_pid_file(path: Path) -> Optional[int]:
        """Read a PID from a file.  Returns None if missing or invalid."""
        try:
            return int(path.read_text().strip())
        except (FileNotFoundError, ValueError):
            return None

    @staticmethod
    def remove_pid_file(path: Path) -> None:
        """Remove a PID file."""
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        """Return a summary dict of all processes."""
        total = len(self.processes)
        running = sum(1 for p in self.processes.values() if p.is_running)
        healthy = sum(1 for p in self.processes.values() if p.health_status == "healthy")
        crashed = sum(1 for p in self.processes.values() if p.health_status == "crashed")

        return {
            "total": total,
            "running": running,
            "healthy": healthy,
            "crashed": crashed,
            "stopped": total - running,
            "processes": {n: p.to_dict() for n, p in self.processes.items()},
        }
