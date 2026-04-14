#!/usr/bin/env python3
"""
SuperZ Runtime — self-booting Pelagic fleet runtime engine.

Usage::

    python runtime.py                    # boot everything with TUI
    python runtime.py --headless         # no TUI, daemon mode
    python runtime.py --skip-mud         # skip MUD server
    python runtime.py --agents trail,trust  # only specific agents
    python runtime.py --config /path/to/fleet.yaml

Boot phases:
    1. Environment Check
    2. Load Config
    3. Start Keeper
    4. Start Git Agent
    5. Launch Fleet Agents
    6. Optional: Start MUD
    7. Health Loop
    8. Graceful Shutdown
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config import FleetConfig
from process_manager import ProcessManager
from agent_launcher import AgentLauncher

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INSTANCE_DIR = Path.home() / ".superinstance"
KEEPER_CMD = "python -m keeper_agent.serve"
GIT_AGENT_CMD = "python -m git_agent.serve"
MUD_CMD_TEMPLATE = "python {holodeck_path}"


# ---------------------------------------------------------------------------
# ANSI helpers (stdlib-only, no curses)
# ---------------------------------------------------------------------------

class _ANSI:
    """Minimal ANSI escape sequences for the TUI."""
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    RED     = "\033[31m"
    GREEN   = "\033[32m"
    YELLOW  = "\033[33m"
    BLUE    = "\033[34m"
    CYAN    = "\033[36m"
    WHITE   = "\033[37m"
    BG_BLUE = "\033[44m"
    CLEAR   = "\033[2J\033[H"
    LINE_UP = "\033[A"
    LINE_CLR = "\033[2K"


def _supports_ansi() -> bool:
    """Heuristic: does the terminal support ANSI escape codes?"""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


# ---------------------------------------------------------------------------
# TUI Renderer
# ---------------------------------------------------------------------------

class TUIRenderer:
    """Lightweight terminal UI — redraw status table every tick."""

    REFRESH_INTERVAL = 2.0  # seconds

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled and _supports_ansi()
        self._boot_start: Optional[float] = None
        self._last_frame: float = 0.0

    def start(self) -> None:
        self._boot_start = time.time()

    def render(
        self,
        statuses: dict[str, dict[str, Any]],
        phase: str = "",
        message: str = "",
    ) -> None:
        if not self._enabled:
            return

        now = time.time()
        if now - self._last_frame < self.REFRESH_INTERVAL and phase != "shutdown":
            return
        self._last_frame = now

        lines: list[str] = []
        a = _ANSI

        # Header
        uptime_str = self._fmt_uptime(now - self._boot_start) if self._boot_start else "0s"
        lines.append(f"{a.CLEAR}")
        lines.append(f"{a.BG_BLUE}{a.WHITE}{a.BOLD}  SuperZ Runtime  {a.RESET}")
        lines.append(f"  Uptime: {uptime_str}   Phase: {a.BOLD}{phase}{a.RESET}")
        lines.append("")

        # Status table header
        lines.append(
            f"  {a.BOLD}{'AGENT':<20} {'STATUS':<8} {'PORT':>6} {'PID':>8} {'UPTIME':>10} {'RESTARTS':>10}{a.RESET}"
        )
        lines.append(f"  {'─' * 20} {'─' * 8} {'─' * 6} {'─' * 8} {'─' * 10} {'─' * 10}")

        total = 0
        healthy = 0
        for name, info in statuses.items():
            total += 1
            status = info.get("status", "DOWN")
            if status == "OK":
                healthy += 1

            color_map = {"OK": a.GREEN, "WARN": a.YELLOW, "ERR": a.RED, "DOWN": a.DIM}
            color = color_map.get(status, a.WHITE)

            lines.append(
                f"  {name:<20} {color}{status:<8}{a.RESET} {info.get('port', '-'):>6} "
                f"{str(info.get('pid', '-')):>8} {self._fmt_uptime(info.get('uptime', 0)):>10} "
                f"{info.get('restart_count', 0):>10}"
            )

        lines.append("")
        summary_color = a.GREEN if healthy == total and total > 0 else a.YELLOW if healthy > 0 else a.RED
        lines.append(f"  {summary_color}{a.BOLD}Fleet: {healthy}/{total} healthy{a.RESET}")
        if message:
            lines.append(f"  {a.DIM}{message}{a.RESET}")
        lines.append(f"  {a.DIM}{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}{a.RESET}")
        lines.append("")

        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()

    @staticmethod
    def _fmt_uptime(seconds: float) -> str:
        if seconds <= 0:
            return "-"
        s = int(seconds)
        h, s = divmod(s, 3600)
        m, s = divmod(s, 60)
        if h:
            return f"{h}h {m}m"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"

    def log(self, msg: str) -> None:
        """Print a single-line message below the table."""
        if self._enabled:
            sys.stdout.write(f"  {_ANSI.DIM}› {msg}{_ANSI.RESET}\n")
            sys.stdout.flush()


# ---------------------------------------------------------------------------
# SuperZ Runtime
# ---------------------------------------------------------------------------

class SuperZRuntime:
    """Self-booting Pelagic fleet runtime.

    Parameters
    ----------
    config_path :
        Path to fleet.yaml (``None`` → auto-discover in cwd).
    headless :
        If *True*, suppress the TUI and run in daemon mode.
    skip_mud :
        If *True*, skip the holodeck MUD server.
    filter_agents :
        If set, only launch agents whose names are in this list.
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        headless: bool = False,
        skip_mud: bool = False,
        filter_agents: Optional[list[str]] = None,
    ) -> None:
        self._headless = headless
        self._skip_mud = skip_mud
        self._filter_agents = filter_agents
        self._running = False
        self._phase = "init"

        # Core components (set up during boot)
        self.config: Optional[FleetConfig] = None
        self.process_mgr: Optional[ProcessManager] = None
        self.launcher: Optional[AgentLauncher] = None
        self.tui = TUIRenderer(enabled=not headless)

        # Config path
        self._config_path = config_path

        # Setup logging
        self._setup_logging()

    # ---- logging -----------------------------------------------------------

    def _setup_logging(self) -> None:
        level = getattr(logging, (self.config.runtime.get("log_level", "INFO") if self.config else "INFO"), logging.INFO)
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%H:%M:%S",
            stream=sys.stderr,
        )

    # ---- boot phases -------------------------------------------------------

    def boot(self) -> None:
        """Execute the full boot sequence."""
        self._running = True
        self.tui.start()

        try:
            self._phase_phase("Environment Check", self._check_environment)
            self._phase_phase("Load Config", self._load_config)
            self._phase_phase("Start Keeper", self._start_keeper)
            self._phase_phase("Start Git Agent", self._start_git_agent)
            self._phase_phase("Launch Fleet Agents", self._launch_fleet_agents)
            if not self._skip_mud:
                self._phase_phase("Start MUD (optional)", self._start_mud)
            self._phase_phase("Health Loop", self._health_loop)
        except KeyboardInterrupt:
            self._shutdown("SIGINT received")
        except SystemExit:
            raise
        except Exception as exc:
            logging.getLogger("superz").error("Fatal error during boot: %s", exc, exc_info=True)
            self._shutdown(f"Error: {exc}")

    def _phase_phase(self, name: str, fn) -> None:
        """Run a boot phase, updating the TUI."""
        self._phase = name
        self.tui.log(f"▶ {name}")
        logging.getLogger("superz").info("=== %s ===", name)
        fn()

    # -- Phase 1: Environment Check ------------------------------------------

    def _check_environment(self) -> None:
        """Verify Python version, git availability, create instance dir."""
        # Python version
        py_major, py_minor = sys.version_info[:2]
        if py_major < 3 or (py_major == 3 and py_minor < 10):
            raise RuntimeError(f"Python 3.10+ required (found {py_major}.{py_minor})")
        logging.getLogger("superz").info("Python %s.%s OK", py_major, py_minor)

        # git
        git_path = shutil.which("git")
        if git_path is None:
            raise RuntimeError("git is not installed or not on PATH")
        logging.getLogger("superz").info("git found: %s", git_path)

        # Instance directory
        INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
        (INSTANCE_DIR / "logs").mkdir(exist_ok=True)
        (INSTANCE_DIR / "agents").mkdir(exist_ok=True)
        logging.getLogger("superz").info("Instance dir: %s", INSTANCE_DIR)

        # Platform info
        logging.getLogger("superz").info("Platform: %s %s", platform.system(), platform.machine())

    # -- Phase 2: Load Config ------------------------------------------------

    def _load_config(self) -> None:
        """Load fleet.yaml or generate defaults."""
        self.config = FleetConfig.load(path=self._config_path)
        errors = self.config.validate()
        if errors:
            for err in errors:
                logging.getLogger("superz").error("Config error: %s", err)
            raise RuntimeError(f"Invalid configuration: {len(errors)} error(s)")

        # Override headless from config if CLI flag not set
        if not self._headless and self.config.runtime.get("headless"):
            self._headless = True
            self.tui = TUIRenderer(enabled=False)

        self._setup_logging()

        logging.getLogger("superz").info("Config loaded: %s", self.config)

    # -- Phase 3: Start Keeper -----------------------------------------------

    def _start_keeper(self) -> None:
        """Register and start the keeper agent."""
        assert self.config is not None
        pm = self._get_process_manager()
        keeper_port = self.config.keeper.get("port", 8443)

        pm.register(
            name="keeper",
            command=KEEPER_CMD,
            port=keeper_port,
            cwd=str(INSTANCE_DIR),
        )

        pid = pm.start_agent("keeper")
        if pid is None:
            logging.getLogger("superz").warning("Keeper failed to start (will retry in health loop)")

    # -- Phase 4: Start Git Agent --------------------------------------------

    def _start_git_agent(self) -> None:
        """Register and start the git agent."""
        assert self.config is not None
        pm = self._get_process_manager()
        ga_port = self.config.git_agent.get("port", 8444)

        pm.register(
            name="git-agent",
            command=GIT_AGENT_CMD,
            port=ga_port,
            cwd=str(INSTANCE_DIR),
        )

        pid = pm.start_agent("git-agent")
        if pid is None:
            logging.getLogger("superz").warning("Git agent failed to start (will retry in health loop)")

    # -- Phase 5: Launch Fleet Agents ----------------------------------------

    def _launch_fleet_agents(self) -> None:
        """Clone (if needed) and launch all enabled fleet agents."""
        assert self.config is not None
        pm = self._get_process_manager()
        self.launcher = AgentLauncher(instance_dir=str(INSTANCE_DIR))

        agents = self.config.enabled_agents()

        # Apply filter
        if self._filter_agents:
            filter_set = {a.strip() for a in self._filter_agents}
            agents = [a for a in agents if a.get("name") in filter_set]
            logging.getLogger("superz").info("Filtered to %d agent(s): %s", len(agents), filter_set)

        for agent_cfg in agents:
            name = agent_cfg["name"]
            port = agent_cfg["port"]

            # Skip if already cloned (launcher handles this)
            if self.launcher.launch(
                name=name,
                port=port,
                agent_config=agent_cfg,
                process_manager=pm,
            ):
                logging.getLogger("superz").info("Launched %s on port %d", name, port)
            else:
                logging.getLogger("superz").warning("Failed to launch %s", name)

    # -- Phase 6: Start MUD --------------------------------------------------

    def _start_mud(self) -> None:
        """Optionally start the holodeck MUD server."""
        assert self.config is not None
        if not self.config.mud.get("enabled", False):
            logging.getLogger("superz").info("MUD disabled — skipping")
            return

        pm = self._get_process_manager()
        mud_port = self.config.mud.get("port", 7777)
        holodeck_path = self.config.mud.get("holodeck_path", "holodeck-studio/server.py")

        mud_cwd = INSTANCE_DIR / "holodeck-studio"
        if not mud_cwd.exists():
            logging.getLogger("superz").warning("Holodeck not found at %s — skipping MUD", mud_cwd)
            return

        cmd = MUD_CMD_TEMPLATE.format(holodeck_path=holodeck_path)
        pm.register(
            name="mud",
            command=cmd,
            port=mud_port,
            cwd=str(mud_cwd),
        )

        pid = pm.start_agent("mud")
        if pid:
            logging.getLogger("superz").info("MUD started on port %d", mud_port)
        else:
            logging.getLogger("superz").warning("MUD failed to start")

    # -- Phase 7: Health Loop ------------------------------------------------

    def _health_loop(self) -> None:
        """Poll all agents and display TUI status until shutdown."""
        assert self.config is not None
        assert self.process_mgr is not None

        interval = self.config.runtime.get("health_interval", 30)
        auto_restart = self.config.runtime.get("auto_restart", True)

        logging.getLogger("superz").info(
            "Health loop: interval=%ds, auto_restart=%s", interval, auto_restart
        )

        while self._running:
            # Check health of each agent
            for name in self.process_mgr.agent_names:
                status = self.process_mgr.check_health(name)

                # Auto-restart crashed agents
                if status == "DOWN" and auto_restart:
                    proc = self.process_mgr.get_agent(name)
                    if proc and proc.restart_count < self.config.runtime.get("max_restart_attempts", 5):
                        logging.getLogger("superz").warning("%s is DOWN — attempting restart", name)
                        self.process_mgr.auto_restart(name)

            # Render TUI
            statuses = self.process_mgr.get_status()
            self.tui.render(statuses, phase="running")

            # Sleep in small increments so we can respond to signals
            self._interruptible_sleep(interval)

    # ---- shutdown ----------------------------------------------------------

    def _shutdown(self, reason: str = "shutdown") -> None:
        """Gracefully stop all agents in reverse order."""
        if not self._running:
            return
        self._running = False
        self._phase = "shutdown"

        logging.getLogger("superz").info("Initiating shutdown: %s", reason)
        self.tui.log(f"⏹ Shutting down ({reason})")

        if self.process_mgr is not None:
            statuses = self.process_mgr.get_status()
            self.tui.render(statuses, phase="shutdown", message=f"Stopping all agents: {reason}")
            self.process_mgr.stop_all()

        logging.getLogger("superz").info("Shutdown complete")
        print(f"\n  {_ANSI.DIM}SuperZ Runtime stopped.{_ANSI.RESET}\n")

    # ---- helpers -----------------------------------------------------------

    def _get_process_manager(self) -> ProcessManager:
        """Lazy-init the process manager."""
        if self.process_mgr is None:
            assert self.config is not None
            self.process_mgr = ProcessManager(
                base_instance_dir=str(INSTANCE_DIR),
                max_restart_attempts=self.config.runtime.get("max_restart_attempts", 5),
                backoff_max=self.config.runtime.get("restart_backoff_max", 60),
            )
        return self.process_mgr

    def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep in small increments, checking ``_running`` each tick."""
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(0.5, end - time.time()))

    def register_signal_handlers(self) -> None:
        """Wire up SIGTERM and SIGINT for graceful shutdown."""
        def _handler(signum, _frame):
            sig_name = signal.Signals(signum).name
            self._shutdown(f"{sig_name} received")

        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="superz-runtime",
        description="Self-booting Pelagic fleet runtime",
    )
    p.add_argument("--headless", action="store_true", help="No TUI, daemon mode")
    p.add_argument("--skip-mud", action="store_true", help="Skip MUD server")
    p.add_argument("--config", type=str, default=None, help="Path to fleet.yaml")
    p.add_argument(
        "--agents",
        type=str,
        default=None,
        help="Comma-separated list of agents to launch (e.g. trail,trust)",
    )
    p.add_argument(
        "--version",
        action="version",
        version="SuperZ Runtime 1.0.0",
    )
    return p


def main(argv: Optional[list[str]] = None) -> None:
    """Entry point for ``python runtime.py``."""
    parser = build_parser()
    args = parser.parse_args(argv)

    filter_agents = None
    if args.agents:
        filter_agents = [a.strip() for a in args.agents.split(",")]

    runtime = SuperZRuntime(
        config_path=args.config,
        headless=args.headless,
        skip_mud=args.skip_mud,
        filter_agents=filter_agents,
    )
    runtime.register_signal_handlers()
    runtime.boot()


if __name__ == "__main__":
    main()
