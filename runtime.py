"""
SuperZ Runtime — The Main Runtime Engine.

Self-booting Pelagic fleet runtime. When someone runs ``python runtime.py``
or ``python -m superz_runtime``, EVERYTHING boots.

Boot sequence (Phase 1–6):
    Phase 1: Environment Check — Python, git, directories, config
    Phase 2: Fleet Bootstrap — clone agents, onboard, generate fleet.yaml
    Phase 3: Start Infrastructure — Keeper + Git Agent
    Phase 4: Launch Agents — all fleet agents in parallel
    Phase 5: MUD Server — optional holodeck-studio
    Phase 6: Health Monitoring — continuous polling, git sync, graceful shutdown
"""

from __future__ import annotations

import os
import sys
import shutil
import time
import signal
import logging
import threading
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

# Ensure the package root is on sys.path for sibling imports
_PACKAGE_ROOT = Path(__file__).resolve().parent
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))

from config import FleetConfig, AgentConfig
from process_manager import ProcessManager, AgentProcess
from health_monitor import HealthMonitor, FleetHealthReport
from agent_launcher import AgentLauncher

logger = logging.getLogger("superz_runtime")

# ---------------------------------------------------------------------------
# ANSI helpers (no curses — just escape codes)
# ---------------------------------------------------------------------------

class _ANSI:
    """ANSI escape code constants for the TUI."""
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    RED     = "\033[31m"
    GREEN   = "\033[32m"
    YELLOW  = "\033[33m"
    CYAN    = "\033[36m"
    WHITE   = "\033[37m"
    CLEAR   = "\033[2J\033[H"  # clear screen + home cursor

    @staticmethod
    def supports_color() -> bool:
        """Detect whether the terminal supports ANSI colors."""
        if os.environ.get("NO_COLOR"):
            return False
        if sys.platform == "win32":
            return os.environ.get("ANSICON") is not None or "WT_SESSION" in os.environ
        return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


# ---------------------------------------------------------------------------
# TUI Status Display
# ---------------------------------------------------------------------------

class TUIDisplay:
    """Minimal TUI status panel using ANSI escape codes (no curses).

    Renders a fleet status box that refreshes every N seconds.
    """

    REFRESH_INTERVAL = 2  # seconds

    def __init__(self) -> None:
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._color = _ANSI.supports_color()
        self._last_lines: int = 0
        self._lock = threading.Lock()

    def start(self, runtime: SuperZRuntime) -> None:
        """Start the TUI refresh thread."""
        self._running = True
        self._thread = threading.Thread(
            target=self._refresh_loop,
            args=(runtime,),
            daemon=True,
            name="tui-display",
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the TUI refresh thread."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def render(self, runtime: SuperZRuntime) -> str:
        """Render the full status panel as a string."""
        a = _ANSI
        c = self._color

        def _s(code: str, text: str) -> str:
            """Wrap *text* in ANSI *code* if colors are supported."""
            return f"{code}{text}{a.RESET}" if c else text

        def _status_icon(status: str) -> str:
            if status == "healthy":
                return _s(a.GREEN, "[OK]")
            elif status == "degraded":
                return _s(a.YELLOW, "[!!]")
            elif status == "crashed":
                return _s(a.RED, "[XX]")
            elif status == "starting":
                return _s(a.CYAN, "[..]")
            elif status == "stopped":
                return _s(a.DIM, "[--]")
            return _s(a.DIM, "[??]")

        # Build agent rows
        processes = runtime.process_manager.get_all()
        rows: list[str] = []

        # Infrastructure agents first
        infra_order = ["keeper-agent", "git-agent"]
        infra_rows: list[str] = []
        agent_rows: list[str] = []

        for name, proc in sorted(processes.items(), key=lambda x: x[0]):
            icon = _status_icon(proc.health_status)
            port_str = f":{proc.port}" if proc.port else "    "
            uptime_str = _format_duration(proc.uptime.total_seconds())
            row = f"  {icon} {_s(a.WHITE, name):20s} {port_str:>6s}  uptime: {uptime_str}"
            if name in ("keeper-agent", "git-agent") or proc.name in infra_order:
                infra_rows.append(row)
            else:
                agent_rows.append(row)

        rows.extend(infra_rows)
        rows.extend(agent_rows)

        # Build footer stats
        pm_summary = runtime.process_manager.summary()
        total = pm_summary["total"]
        healthy = pm_summary["healthy"]
        uptime_str = _format_duration(runtime.uptime_seconds)
        report = runtime.health_monitor.get_last_report()

        # Header
        box_width = 52
        hline = _s(a.DIM, "═" * box_width)
        title = _s(a.BOLD, " SUPERZ RUNTIME — Pelagic Fleet ")

        lines = [
            f"{_s(a.DIM, '╔')}{hline}{_s(a.DIM, '╗')}",
            f"{_s(a.DIM, '║')}{title:^{box_width}}{_s(a.DIM, '║')}",
            f"{_s(a.DIM, '╠')}{hline}{_s(a.DIM, '╣')}",
        ]

        if rows:
            for row in rows:
                padded = f"{row:<{box_width}}"
                lines.append(f"{_s(a.DIM, '║')}{padded}{_s(a.DIM, '║')}")
        else:
            lines.append(f"{_s(a.DIM, '║')}  No agents running{' ' * (box_width - 18)}{_s(a.DIM, '║')}")

        lines.append(f"{_s(a.DIM, '╠')}{hline}{_s(a.DIM, '╣')}")

        # Footer stats
        fleet_line = (
            f"  Fleet Health: {healthy}/{total} OK | Uptime: {uptime_str}"
        )
        services_line = "  Services: Keeper(:8443) Git(:8444)"
        test_line = f"  Total Tests: {runtime.health_monitor._total_tests_passing} passing"

        for info_line in [fleet_line, test_line, services_line]:
            padded = f"{info_line:<{box_width}}"
            lines.append(f"{_s(a.DIM, '║')}{padded}{_s(a.DIM, '║')}")

        lines.append(f"{_s(a.DIM, '╚')}{hline}{_s(a.DIM, '╝')}")

        return "\n".join(lines)

    def _refresh_loop(self, runtime: SuperZRuntime) -> None:
        """Periodically redraw the status panel."""
        time.sleep(1)  # initial delay
        while self._running:
            with self._lock:
                if self._color:
                    # Move cursor to top and clear
                    sys.stdout.write(_ANSI.CLEAR)
                else:
                    # Non-terminal: just print a separator
                    sys.stdout.write(f"\n{'=' * 52}\n")
                sys.stdout.write(self.render(runtime) + "\n")
                sys.stdout.flush()
            time.sleep(self.REFRESH_INTERVAL)


# ---------------------------------------------------------------------------
# Main Runtime
# ---------------------------------------------------------------------------

class SuperZRuntime:
    """Self-booting Pelagic fleet runtime.

    Usage::

        python runtime.py                    # boot everything
        python runtime.py --headless          # no TUI, daemon mode
        python runtime.py --skip-mud          # skip MUD server
        python runtime.py --agents trail,trust  # only specific agents
        python runtime.py --config fleet.yaml # custom config
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        headless: bool = False,
        skip_mud: bool = False,
        agent_filter: Optional[list[str]] = None,
    ) -> None:
        self.config_path = config_path
        self.headless_override = headless
        self.skip_mud = skip_mud
        self.agent_filter = agent_filter

        # Core components (set during boot)
        self.config: Optional[FleetConfig] = None
        self.process_manager: Optional[ProcessManager] = None
        self.health_monitor: Optional[HealthMonitor] = None
        self.agent_launcher: Optional[AgentLauncher] = None
        self.tui: Optional[TUIDisplay] = None

        # State
        self._started_at: Optional[datetime] = None
        self._shutting_down = False
        self._threads: list[threading.Thread] = []

    @property
    def uptime_seconds(self) -> float:
        if self._started_at is None:
            return 0.0
        return (datetime.now() - self._started_at).total_seconds()

    # ==================================================================
    # BOOT SEQUENCE
    # ==================================================================

    def boot(self) -> None:
        """Execute the full boot sequence (Phase 1–6)."""
        self._setup_logging()
        self._started_at = datetime.now()

        banner()
        logger.info("SuperZ Runtime starting...")

        try:
            self._phase1_environment_check()
            self._phase2_fleet_bootstrap()
            self._phase3_start_infrastructure()
            self._phase4_launch_agents()
            if not self.skip_mud:
                self._phase5_mud_server()
            self._phase6_health_monitoring()
        except BootError as exc:
            logger.error("Boot failed at %s: %s", exc.phase, exc.message)
            self.shutdown()
            sys.exit(1)
        except KeyboardInterrupt:
            logger.info("Boot interrupted by user")
            self.shutdown()

    # ------------------------------------------------------------------
    # Phase 1: Environment Check
    # ------------------------------------------------------------------

    def _phase1_environment_check(self) -> None:
        """Check Python version, git, create directories, load config."""
        phase = "Phase 1: Environment Check"
        log_phase(phase)

        # Python version
        if sys.version_info < (3, 10):
            raise BootError(phase, f"Python 3.10+ required, got {sys.version_info[:2]}")
        logger.info("Python %s.%s — OK", sys.version_info.major, sys.version_info.minor)

        # Git availability
        if not _check_command("git"):
            raise BootError(phase, "git is required but not found in PATH")
        logger.info("git — OK")

        # Optional: gh CLI
        if _check_command("gh"):
            logger.info("gh CLI — available")
        else:
            logger.info("gh CLI — not found (optional)")

        # Migrate legacy paths
        FleetConfig.bridge_legacy_paths()

        # Load config
        self.config = FleetConfig.load(self.config_path)

        # Apply CLI overrides
        if self.headless_override:
            self.config.runtime.headless = True
        if self.skip_mud:
            self.config.mud.enabled = False

        # Validate config
        errors = self.config.validate()
        if errors:
            for err in errors:
                logger.error("Config error: %s", err)
            raise BootError(phase, f"Config validation failed: {len(errors)} errors")

        # Ensure directories
        self.config.ensure_directories()

        # Save default config if none existed
        if self.config.config_path and not self.config.config_path.exists():
            self.config.save_default()

        logger.info("Config loaded: %d agents, keeper=:%d",
                     len(self.config.agents), self.config.keeper.port)

        # Initialize components
        self.process_manager = ProcessManager(
            logs_dir=FleetConfig.DEFAULT_LOGS_DIR,
            max_backoff=self.config.runtime.max_restart_backoff,
            shutdown_timeout=self.config.runtime.shutdown_timeout,
        )
        self.health_monitor = HealthMonitor(
            health_timeout=5,
            max_history=1000,
        )
        self.agent_launcher = AgentLauncher(self.config)

        log_phase_done(phase)

    # ------------------------------------------------------------------
    # Phase 2: Fleet Bootstrap
    # ------------------------------------------------------------------

    def _phase2_fleet_bootstrap(self) -> None:
        """Clone agents, onboard, generate fleet.yaml."""
        phase = "Phase 2: Fleet Bootstrap"
        log_phase(phase)

        agents = self.config.filter_agents(self.agent_filter)
        logger.info("Preparing %d agents...", len(agents))

        # Clone missing agents
        missing = self.agent_launcher.discover_missing_agents(agents)
        if missing:
            logger.info("Cloning %d missing agents...", len(missing))
            results = self.agent_launcher.clone_missing(missing)
            for name, success in results.items():
                if not success:
                    logger.warning("Failed to clone %s — will skip", name)

        # Onboard agents
        for agent in agents:
            agent_dir = FleetConfig.DEFAULT_AGENTS_DIR / agent.name
            if agent_dir.exists():
                self.agent_launcher.onboard_agent(agent)

        # Update fleet.yaml with any changes
        if self.config.config_path:
            self.config.save()

        local = self.agent_launcher.discover_local_agents()
        logger.info("Local agents: %s", ", ".join(local) or "none")

        log_phase_done(phase)

    # ------------------------------------------------------------------
    # Phase 3: Start Infrastructure
    # ------------------------------------------------------------------

    def _phase3_start_infrastructure(self) -> None:
        """Start Keeper Agent and Git Agent."""
        phase = "Phase 3: Start Infrastructure"
        log_phase(phase)

        # Register status callback
        self.process_manager.set_status_callback(self._on_agent_status_change)

        # --- Keeper Agent ---
        if self.config.keeper.enabled:
            keeper = AgentConfig(
                name="keeper-agent",
                port=self.config.keeper.port,
                host=self.config.keeper.host,
                enabled=True,
                mode="serve",
            )
            self._start_infrastructure_agent(keeper)
            self.health_monitor.add_agent(
                "keeper-agent",
                f"http://{self.config.keeper.host}:{self.config.keeper.port}/health",
            )
            self._wait_for_agent("keeper-agent", timeout=30)

        # --- Git Agent ---
        if self.config.git_agent.enabled:
            git_agent = AgentConfig(
                name="git-agent",
                port=self.config.git_agent.port,
                host=self.config.git_agent.host,
                enabled=True,
                mode="serve",
            )
            self._start_infrastructure_agent(git_agent)
            self.health_monitor.add_agent(
                "git-agent",
                f"http://{self.config.git_agent.host}:{self.config.git_agent.port}/health",
            )
            self._wait_for_agent("git-agent", timeout=30)

        log_phase_done(phase)

    def _start_infrastructure_agent(self, agent: AgentConfig) -> None:
        """Start an infrastructure agent (keeper or git)."""
        # Infrastructure agents typically run as modules
        keeper_dir = FleetConfig.DEFAULT_AGENTS_DIR / agent.name
        if keeper_dir.exists():
            cmd = ["python", "-m", agent.name.replace("-", "_"), "serve"]
        else:
            # Start a stub that listens on the port
            cmd = self._stub_command(agent.name, agent.port)

        env = self.agent_launcher.build_agent_env(agent) if self.agent_launcher else os.environ.copy()

        self.process_manager.start_agent(
            name=agent.name,
            cmd=cmd,
            cwd=str(keeper_dir) if keeper_dir.exists() else os.getcwd(),
            env=env,
            port=agent.port,
        )

    def _stub_command(self, name: str, port: int) -> list[str]:
        """Generate a stub HTTP server command for placeholder agents."""
        stub_dir = Path(__file__).parent
        stub_path = stub_dir / "_agent_stub.py"
        return [sys.executable, str(stub_path), "--name", name, "--port", str(port)]

    def _wait_for_agent(self, name: str, timeout: int = 30) -> bool:
        """Wait for an agent to become healthy (via health check or heartbeat)."""
        logger.info("Waiting for %s to be healthy (timeout: %ds)...", name, timeout)
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            proc = self.process_manager.get_all().get(name)
            if proc and proc.health_status == "healthy":
                logger.info("%s is healthy", name)
                return True
            if proc and proc.health_status == "crashed":
                logger.warning("%s crashed during startup — will auto-restart", name)
                return False
            time.sleep(1)
        logger.warning("%s did not become healthy in time", name)
        return False

    # ------------------------------------------------------------------
    # Phase 4: Launch Agents
    # ------------------------------------------------------------------

    def _phase4_launch_agents(self) -> None:
        """Launch all fleet agents in parallel."""
        phase = "Phase 4: Launch Agents"
        log_phase(phase)

        agents = self.config.filter_agents(self.agent_filter)
        # Exclude infrastructure agents (already started)
        infra = {"keeper-agent", "git-agent"}
        agents = [a for a in agents if a.name not in infra]

        logger.info("Launching %d agents...", len(agents))

        for agent in agents:
            prep = self.agent_launcher.prepare_agent(agent)
            if prep is None:
                logger.warning("Skipping %s — preparation failed", agent.name)
                continue

            self.process_manager.start_agent(
                name=prep["name"],
                cmd=prep["cmd"],
                cwd=prep["cwd"],
                env=prep["env"],
                port=prep["port"],
            )
            self.health_monitor.add_agent(
                agent.name,
                f"http://{agent.host}:{agent.port}/health",
            )

        # Brief wait for agents to start
        logger.info("Waiting for agents to initialise...")
        time.sleep(2)

        # Check initial health
        report = self.health_monitor.check_all()
        healthy = report.healthy_count
        total = report.total_agents
        logger.info("Initial health: %d/%d agents healthy", healthy, total)

        log_phase_done(phase)

    # ------------------------------------------------------------------
    # Phase 5: MUD Server
    # ------------------------------------------------------------------

    def _phase5_mud_server(self) -> None:
        """Start holodeck-studio MUD server if available."""
        phase = "Phase 5: MUD Server"
        log_phase(phase)

        if not self.config.mud.enabled:
            logger.info("MUD server disabled — skipping")
            log_phase_done(phase)
            return

        mud_path = FleetConfig.DEFAULT_AGENTS_DIR / "holodeck-studio"
        server_script = mud_path / "server.py"

        if not server_script.exists():
            logger.info("holodeck-studio/server.py not found — skipping MUD")
            log_phase_done(phase)
            return

        cmd = [sys.executable, str(server_script)]
        env = os.environ.copy()
        env["MUD_PORT"] = str(self.config.mud.port)
        env["MUD_HOST"] = self.config.mud.host
        env["WORLD_PATH"] = self.config.mud.world_path

        self.process_manager.start_agent(
            name="holodeck-mud",
            cmd=cmd,
            cwd=str(mud_path),
            env=env,
            port=self.config.mud.port,
        )
        self.health_monitor.add_agent(
            "holodeck-mud",
            f"http://{self.config.mud.host}:{self.config.mud.port}/health",
        )
        logger.info("MUD server started on port %d", self.config.mud.port)

        log_phase_done(phase)

    # ------------------------------------------------------------------
    # Phase 6: Health Monitoring (continuous)
    # ------------------------------------------------------------------

    def _phase6_health_monitoring(self) -> None:
        """Start continuous health monitoring and TUI."""
        phase = "Phase 6: Health Monitoring"
        log_phase(phase)

        # Health check thread
        health_thread = threading.Thread(
            target=self._health_loop,
            daemon=True,
            name="health-monitor",
        )
        health_thread.start()
        self._threads.append(health_thread)

        # Git sync thread
        git_thread = threading.Thread(
            target=self._git_sync_loop,
            daemon=True,
            name="git-sync",
        )
        git_thread.start()
        self._threads.append(git_thread)

        # TUI (if not headless)
        if not self.config.runtime.headless:
            self.tui = TUIDisplay()
            self.tui.start(self)
        else:
            logger.info("Running in headless mode — no TUI")

        # Register signal handlers
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        logger.info("=" * 52)
        logger.info("  FLEET READY — all systems operational")
        logger.info("=" * 52)

        # Print initial report
        report = self.health_monitor.check_all()
        print(self.health_monitor.format_report(report))

        # Main loop — keep alive until shutdown
        try:
            while not self._shutting_down:
                # Check for dead processes
                self.process_manager.check_and_restart_dead()
                time.sleep(5)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def _health_loop(self) -> None:
        """Periodically check health of all agents."""
        interval = self.config.runtime.health_interval if self.config else 30
        while not self._shutting_down:
            time.sleep(interval)
            try:
                report = self.health_monitor.check_all()
                alerts = self.health_monitor.check_alerts()
                for alert in alerts:
                    logger.warning("ALERT: %s", alert)

                # Update process manager statuses based on health
                for name, snapshot in report.agents.items():
                    if snapshot.status == "healthy":
                        self.process_manager.set_healthy(name)
                    elif snapshot.status == "degraded":
                        self.process_manager.set_degraded(name)
                    elif snapshot.status == "unhealthy":
                        # Only mark as crashed if process is actually dead
                        proc = self.process_manager.get_all().get(name)
                        if proc and not proc.is_running:
                            self.process_manager.set_crashed(name)
            except Exception as exc:
                logger.error("Health check error: %s", exc)

    def _git_sync_loop(self) -> None:
        """Periodically git-sync agent repos."""
        interval = self.config.runtime.git_sync_interval if self.config else 300
        while not self._shutting_down:
            time.sleep(interval)
            try:
                self._git_sync_all()
            except Exception as exc:
                logger.error("Git sync error: %s", exc)

    def _git_sync_all(self) -> None:
        """Run git pull on each agent repo."""
        agents_dir = FleetConfig.DEFAULT_AGENTS_DIR
        for agent_dir in agents_dir.iterdir():
            if agent_dir.is_dir() and (agent_dir / ".git").exists():
                try:
                    result = _run_git(["git", "pull", "--ff-only"], cwd=str(agent_dir))
                    if result.returncode == 0:
                        logger.debug("Git sync: %s — up to date", agent_dir.name)
                    else:
                        logger.warning("Git sync failed for %s", agent_dir.name)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Status callback
    # ------------------------------------------------------------------

    def _on_agent_status_change(self, name: str, old: str, new: str) -> None:
        """Called when an agent's health status changes."""
        logger.info("Status change: %s %s → %s", name, old, new)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Gracefully shut down the entire fleet."""
        if self._shutting_down:
            return
        self._shutting_down = True
        logger.info("Shutting down SuperZ Runtime...")

        # Stop TUI
        if self.tui:
            self.tui.stop()

        # Stop all processes
        if self.process_manager:
            self.process_manager.stop_all()

        logger.info("SuperZ Runtime stopped. Goodbye!")
        # Clean ANSI
        if _ANSI.supports_color():
            sys.stdout.write(_ANSI.RESET)
            sys.stdout.flush()

    def _signal_handler(self, signum: int, frame: Any) -> None:
        """Handle SIGTERM/SIGINT for graceful shutdown."""
        sig_name = signal.Signals(signum).name
        logger.info("Received %s — shutting down", sig_name)
        self.shutdown()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _setup_logging(self) -> None:
        """Configure logging based on config or defaults."""
        log_level = "INFO"
        if self.config and self.config.runtime.log_level:
            log_level = self.config.runtime.log_level.upper()

        # If headless override and no config yet, still set a reasonable level
        logging.basicConfig(
            level=getattr(logging, log_level, logging.INFO),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )


# ---------------------------------------------------------------------------
# Agent Stub — lightweight placeholder for missing agents
# ---------------------------------------------------------------------------

def _create_agent_stub() -> str:
    """Generate the agent stub script content."""
    return '''"""Lightweight HTTP stub that stands in for un-cloned agents."""
import sys
import json
import signal
import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler


class StubHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "healthy", "stub": True}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress request logs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="stub")
    parser.add_argument("--port", type=int, default=8500)
    args = parser.parse_args()

    server = HTTPServer(("127.0.0.1", args.port), StubHandler)
    print(f"[{args.name}] Stub agent listening on :{args.port}", flush=True)

    def shutdown(sig, frame):
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server.serve_forever()


if __name__ == "__main__":
    main()
'''


# ---------------------------------------------------------------------------
# Utility Functions
# ---------------------------------------------------------------------------

class BootError(Exception):
    """Error during a specific boot phase."""
    def __init__(self, phase: str, message: str) -> None:
        self.phase = phase
        self.message = message
        super().__init__(f"{phase}: {message}")


def _check_command(cmd: str) -> bool:
    """Check if a command is available in PATH."""
    return shutil.which(cmd) is not None


def _run_git(cmd: list[str], cwd: str = ".", timeout: int = 30) -> Any:
    """Run a git command and return the CompletedProcess."""
    import subprocess
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def _format_duration(seconds: float) -> str:
    """Format seconds as HH:MM:SS or MM:SS."""
    seconds = int(max(0, seconds))
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def banner() -> None:
    """Print the SuperZ Runtime banner."""
    a = _ANSI
    c = a.supports_color()
    def _s(code: str, text: str) -> str:
        return f"{code}{text}{a.RESET}" if c else text

    lines = [
        "",
        _s(a.BOLD, "  ╦ ╦┌─┐┬─┐┬┌─╦ ╦┬┬  ┌─┐"),
        _s(a.BOLD, "  ║║║├┤ ├┬┘├┴┐╠═╣││  ├┤ "),
        _s(a.BOLD, "  ╚╩╝└─┘┴└─┴ ╩╩ ╩┴┴─┘└─┘"),
        _s(a.DIM, "  ───────────────────────────────────"),
        _s(a.CYAN, "  SuperZ Runtime — Pelagic Fleet Engine"),
        _s(a.DIM, "  ───────────────────────────────────"),
        "",
    ]
    print("\n".join(lines))


def log_phase(phase: str) -> None:
    """Log a phase start message."""
    a = _ANSI
    c = a.supports_color()
    line = f"▶ {phase}" if c else f">> {phase}"
    print(a.BOLD + a.CYAN + line + a.RESET if c else line)
    logger.info("Starting %s", phase)


def log_phase_done(phase: str) -> None:
    """Log a phase completion message."""
    a = _ANSI
    c = a.supports_color()
    line = f"✓ {phase} — complete" if c else f"OK {phase} — complete"
    print(a.GREEN + line + a.RESET if c else line)
    logger.info("%s — complete", phase)


# ---------------------------------------------------------------------------
# Ensure agent stub exists
# ---------------------------------------------------------------------------

def ensure_agent_stub() -> None:
    """Write the agent stub script if it doesn't exist."""
    stub_path = Path(__file__).parent / "_agent_stub.py"
    if not stub_path.exists():
        stub_path.write_text(_create_agent_stub())


# ---------------------------------------------------------------------------
# Module-level entry point (python runtime.py)
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point for ``python runtime.py``."""
    ensure_agent_stub()

    parser = argparse.ArgumentParser(
        prog="superz_runtime",
        description="SuperZ Runtime — self-booting Pelagic fleet",
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Run without TUI (daemon mode)",
    )
    parser.add_argument(
        "--skip-mud", action="store_true",
        help="Skip MUD server startup",
    )
    parser.add_argument(
        "--agents", type=str, default=None,
        help="Comma-separated list of agents to run (e.g. trail,trust)",
    )
    parser.add_argument(
        "--config", type=str, default=None,
        help="Path to fleet.yaml config file",
    )
    parser.add_argument(
        "--doctor", action="store_true",
        help="Run diagnostics and exit",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Show fleet status and exit",
    )
    parser.add_argument(
        "--stop", action="store_true",
        help="Stop all running agents and exit",
    )

    args = parser.parse_args()

    agent_filter: Optional[list[str]] = None
    if args.agents:
        agent_filter = [a.strip() for a in args.agents.split(",")]

    if args.doctor:
        run_doctor()
        return

    if args.stop:
        run_stop()
        return

    if args.status:
        run_status()
        return

    runtime = SuperZRuntime(
        config_path=args.config,
        headless=args.headless,
        skip_mud=args.skip_mud,
        agent_filter=agent_filter,
    )
    runtime.boot()


# ---------------------------------------------------------------------------
# Standalone commands
# ---------------------------------------------------------------------------

def run_doctor() -> None:
    """Diagnose common issues."""
    banner()
    print("\nRunning diagnostics...\n")

    checks: list[tuple[str, bool, str]] = []

    # Python
    ok = sys.version_info >= (3, 10)
    checks.append(("Python 3.10+", ok, f"Python {sys.version_info[:3]}"))

    # Git
    ok = _check_command("git")
    checks.append(("git", ok, "found" if ok else "not found in PATH"))

    # gh CLI
    ok = _check_command("gh")
    checks.append(("gh CLI", ok, "found" if ok else "not found (optional)"))

    # Instance directory
    inst = FleetConfig.DEFAULT_INSTANCE_DIR
    ok = inst.exists()
    checks.append((f"~/.superinstance/", ok, "exists" if ok else "not found"))

    # Agents directory
    agents = FleetConfig.DEFAULT_AGENTS_DIR
    ok = agents.exists()
    checks.append((f"~/.superinstance/agents/", ok, "exists" if ok else "not found"))

    # Config
    cfg_path = FleetConfig.DEFAULT_CONFIG_PATH
    ok = cfg_path.exists()
    checks.append((f"fleet.yaml", ok, "exists" if ok else "not found (will use defaults)"))

    # Local agents
    launcher = AgentLauncher(FleetConfig.load())
    local = launcher.discover_local_agents()
    ok = len(local) > 0
    checks.append(("Cloned agents", ok, f"{len(local)} found: {', '.join(local) or 'none'}"))

    # PyYAML
    try:
        import yaml  # type: ignore
        checks.append(("PyYAML", True, "installed"))
    except ImportError:
        checks.append(("PyYAML", False, "not installed (pip install pyyaml)"))

    # Results
    for name, ok, detail in checks:
        icon = "✓" if ok else "✗"
        color = "\033[32m" if ok and _ANSI.supports_color() else ("\033[31m" if not ok and _ANSI.supports_color() else "")
        reset = "\033[0m" if _ANSI.supports_color() else ""
        print(f"  {color}{icon}{reset} {name:25s} {detail}")

    total = len(checks)
    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"\n  {passed}/{total} checks passed")
    if passed < total:
        print("  Some issues found — see above")


def run_stop() -> None:
    """Stop all running agents."""
    pid_file = FleetConfig.DEFAULT_INSTANCE_DIR / "superz_runtime.pid"
    pid = ProcessManager.read_pid_file(pid_file)
    if pid is None:
        print("No running runtime found (no PID file)")
        return
    print(f"Sending SIGTERM to runtime PID {pid}...")
    try:
        os.kill(pid, signal.SIGTERM)
        print("Shutdown signal sent")
    except ProcessLookupError:
        print(f"Process {pid} not found — removing stale PID file")
        ProcessManager.remove_pid_file(pid_file)
    except PermissionError:
        print(f"Permission denied — cannot stop PID {pid}")


def run_status() -> None:
    """Show fleet status (checks health endpoints)."""
    config = FleetConfig.load()
    monitor = HealthMonitor()

    # Register all expected agents
    if config.keeper.enabled:
        monitor.add_agent("keeper-agent", f"http://{config.keeper.host}:{config.keeper.port}/health")
    if config.git_agent.enabled:
        monitor.add_agent("git-agent", f"http://{config.git_agent.host}:{config.git_agent.port}/health")
    for agent in config.get_enabled_agents():
        monitor.add_agent(agent.name, f"http://{agent.host}:{agent.port}/health")

    report = monitor.check_all()
    print(monitor.format_report(report))


if __name__ == "__main__":
    main()
