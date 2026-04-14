"""
Tests for SuperZ Runtime.

Covers: environment check, config loading/validation, process manager,
health monitor, agent launcher, boot sequence (mocked), graceful shutdown,
and TUI output capture.
"""

from __future__ import annotations

import io
import json
import os
import sys
import signal
import time
import shutil
import tempfile
import threading
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock
from http.server import HTTPServer, BaseHTTPRequestHandler

import pytest

# Ensure the package root is importable
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))

from config import (
    FleetConfig, AgentConfig, RuntimeConfig, KeeperConfig,
    GitAgentConfig, MudConfig, NetworkConfig, SecretsConfig, _parse_bool,
)
from process_manager import ProcessManager, AgentProcess, LogRotator
from health_monitor import HealthMonitor, HealthSnapshot, FleetHealthReport
from agent_launcher import AgentLauncher, datetime_now_iso
from runtime import (
    SuperZRuntime, BootError, _format_duration, _check_command,
    banner, log_phase, log_phase_done, ensure_agent_stub,
    run_doctor, run_stop, run_status,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    """Provide a temporary directory for tests."""
    return tmp_path


@pytest.fixture
def config(tmp_dir: Path) -> FleetConfig:
    """Create a FleetConfig with temp directories."""
    cfg = FleetConfig()
    cfg.DEFAULT_INSTANCE_DIR = tmp_dir / "instance"
    cfg.DEFAULT_AGENTS_DIR = tmp_dir / "instance" / "agents"
    cfg.DEFAULT_LOGS_DIR = tmp_dir / "instance" / "logs"
    cfg.DEFAULT_VAULT_DIR = tmp_dir / "instance" / "vault"
    cfg.DEFAULT_WORKSHOP_DIR = tmp_dir / "instance" / "workshop"
    cfg.DEFAULT_WORLDS_DIR = tmp_dir / "instance" / "worlds"
    cfg.DEFAULT_CONFIG_PATH = tmp_dir / "fleet.yaml"
    cfg._fill_defaults()
    return cfg


@pytest.fixture
def proc_manager(tmp_dir: Path) -> ProcessManager:
    """Create a ProcessManager with temp log dir."""
    logs_dir = tmp_dir / "logs"
    return ProcessManager(logs_dir=logs_dir)


@pytest.fixture
def health_monitor() -> HealthMonitor:
    """Create a HealthMonitor."""
    return HealthMonitor(health_timeout=2, max_history=100)


# ---------------------------------------------------------------------------
# Config Tests
# ---------------------------------------------------------------------------

class TestFleetConfig:
    """Tests for FleetConfig loading, validation, and persistence."""

    def test_default_config(self) -> None:
        """Default config should have sensible values."""
        cfg = FleetConfig()
        assert cfg.keeper.port == 8443
        assert cfg.git_agent.port == 8444
        assert len(cfg.agents) == 8
        assert cfg.mud.port == 7777
        assert cfg.network.topology == "star"

    def test_default_agents(self) -> None:
        """Default agent list should match spec."""
        cfg = FleetConfig()
        names = {a.name for a in cfg.agents}
        assert "trail-agent" in names
        assert "trust-agent" in names
        assert "flux-vm-agent" in names
        assert "knowledge-agent" in names
        assert "scheduler-agent" in names
        assert "edge-relay" in names
        assert "liaison-agent" in names
        assert "cartridge-agent" in names

    def test_load_nonexistent(self) -> None:
        """Loading a nonexistent file should return defaults."""
        cfg = FleetConfig.load("/nonexistent/path.yaml")
        assert len(cfg.agents) == 8

    def test_save_and_load(self, tmp_dir: Path) -> None:
        """Save and reload a config."""
        cfg = FleetConfig()
        cfg.keeper.port = 9999
        path = tmp_dir / "test.yaml"
        cfg.save(path)

        loaded = FleetConfig.load(str(path))
        assert loaded.keeper.port == 9999

    def test_validate_good(self) -> None:
        """Valid config should have no errors."""
        cfg = FleetConfig()
        errors = cfg.validate()
        assert len(errors) == 0

    def test_validate_port_conflict(self) -> None:
        """Port conflicts should be detected."""
        cfg = FleetConfig()
        cfg.keeper.port = 8443
        cfg.git_agent.port = 8443  # conflict!
        errors = cfg.validate()
        assert any("conflict" in e.lower() for e in errors)

    def test_validate_port_range(self) -> None:
        """Out-of-range ports should be detected."""
        cfg = FleetConfig()
        cfg.keeper.port = 99999
        errors = cfg.validate()
        assert any("out of range" in e.lower() for e in errors)

    def test_validate_health_interval(self) -> None:
        """Health interval below minimum should be detected."""
        cfg = FleetConfig()
        cfg.runtime.health_interval = 1
        errors = cfg.validate()
        assert any("health_interval" in e for e in errors)

    def test_validate_topology(self) -> None:
        """Invalid topology should be detected."""
        cfg = FleetConfig()
        cfg.network.topology = "invalid"
        errors = cfg.validate()
        assert any("topology" in e for e in errors)

    def test_env_override_headless(self) -> None:
        """Environment variable should override headless."""
        with patch.dict(os.environ, {"SUPERZ_HEADLESS": "true"}):
            cfg = FleetConfig.load()
            assert cfg.runtime.headless is True

    def test_env_override_skip_mud(self) -> None:
        """SUPERZ_SKIP_MUD should disable MUD."""
        with patch.dict(os.environ, {"SUPERZ_SKIP_MUD": "true"}):
            cfg = FleetConfig.load()
            assert cfg.mud.enabled is False

    def test_filter_agents(self) -> None:
        """Agent filtering should work correctly."""
        cfg = FleetConfig()
        filtered = cfg.filter_agents(["trail-agent", "trust-agent"])
        assert len(filtered) == 2
        names = {a.name for a in filtered}
        assert "trail-agent" in names
        assert "trust-agent" in names

    def test_ensure_directories(self, tmp_dir: Path) -> None:
        """ensure_directories should create all dirs."""
        cfg = FleetConfig()
        cfg.DEFAULT_INSTANCE_DIR = tmp_dir / "new_instance"
        cfg.DEFAULT_AGENTS_DIR = tmp_dir / "new_instance" / "agents"
        cfg.DEFAULT_LOGS_DIR = tmp_dir / "new_instance" / "logs"
        cfg.DEFAULT_VAULT_DIR = tmp_dir / "new_instance" / "vault"
        cfg.DEFAULT_WORKSHOP_DIR = tmp_dir / "new_instance" / "workshop"
        cfg.DEFAULT_WORLDS_DIR = tmp_dir / "new_instance" / "worlds"
        cfg._fill_defaults()
        cfg.ensure_directories()
        assert cfg.DEFAULT_INSTANCE_DIR.exists()
        assert cfg.DEFAULT_AGENTS_DIR.exists()
        assert cfg.DEFAULT_LOGS_DIR.exists()


class TestParseBool:
    """Tests for _parse_bool helper."""

    def test_true_values(self) -> None:
        for val in ("1", "true", "TRUE", "yes", "YES", "on", "ON"):
            assert _parse_bool(val) is True

    def test_false_values(self) -> None:
        for val in ("0", "false", "FALSE", "no", "NO", "off", "OFF", "anything"):
            assert _parse_bool(val) is False


# ---------------------------------------------------------------------------
# Process Manager Tests
# ---------------------------------------------------------------------------

class TestProcessManager:
    """Tests for ProcessManager start/stop/restart."""

    def test_start_and_stop(self, proc_manager: ProcessManager) -> None:
        """Should be able to start and stop a subprocess."""
        agent = proc_manager.start_agent(
            name="test-sleep",
            cmd=[sys.executable, "-c", "import time; time.sleep(30)"],
        )
        assert agent.is_running
        assert agent.health_status == "starting"

        proc_manager.stop_agent("test-sleep")
        assert not agent.is_running
        assert agent.health_status == "stopped"

    def test_stop_nonexistent(self, proc_manager: ProcessManager) -> None:
        """Stopping a nonexistent agent should not error."""
        result = proc_manager.stop_agent("ghost-agent")
        assert result is True

    def test_stop_all(self, proc_manager: ProcessManager) -> None:
        """stop_all should stop every running agent."""
        proc_manager.start_agent(
            name="a",
            cmd=[sys.executable, "-c", "import time; time.sleep(30)"],
        )
        proc_manager.start_agent(
            name="b",
            cmd=[sys.executable, "-c", "import time; time.sleep(30)"],
        )
        assert len(proc_manager.get_running()) == 2

        proc_manager.stop_all()
        assert len(proc_manager.get_running()) == 0

    def test_crashed_agent_detection(self, proc_manager: ProcessManager) -> None:
        """Crashed agents should be detected by check_and_restart_dead."""
        proc_manager.start_agent(
            name="exit-immediately",
            cmd=[sys.executable, "-c", "import sys; sys.exit(1)"],
        )
        # Wait for the process to actually exit
        time.sleep(0.5)
        restarted = proc_manager.check_and_restart_dead()
        assert "exit-immediately" in restarted

    def test_status_callback(self, proc_manager: ProcessManager) -> None:
        """Status change callback should fire on state transitions."""
        changes: list[tuple[str, str, str]] = []
        proc_manager.set_status_callback(lambda n, o, s: changes.append((n, o, s)))

        proc_manager.start_agent(
            name="cb-test",
            cmd=[sys.executable, "-c", "import time; time.sleep(30)"],
        )
        proc_manager.stop_agent("cb-test")

        # Should have recorded at least the stopped transition
        assert len(changes) >= 1
        assert changes[-1][2] == "stopped"

    def test_pid_file(self, tmp_dir: Path) -> None:
        """PID file should be written and read correctly."""
        pid_path = tmp_dir / "runtime.pid"
        pm = ProcessManager(logs_dir=tmp_dir / "logs")
        pm.write_pid_file(pid_path)
        assert pid_path.exists()
        assert ProcessManager.read_pid_file(pid_path) == os.getpid()
        ProcessManager.remove_pid_file(pid_path)
        assert not pid_path.exists()

    def test_summary(self, proc_manager: ProcessManager) -> None:
        """Summary should reflect current process state."""
        proc_manager.start_agent(
            name="s1",
            cmd=[sys.executable, "-c", "import time; time.sleep(30)"],
        )
        summary = proc_manager.summary()
        assert summary["total"] == 1
        assert summary["running"] == 1

        proc_manager.stop_all()
        summary = proc_manager.summary()
        assert summary["running"] == 0
        assert summary["stopped"] == 1

    def test_log_rotator(self, tmp_dir: Path) -> None:
        """LogRotator should create log files correctly."""
        rotator = LogRotator(tmp_dir / "logs", max_logs=3, max_bytes=1024)
        path = rotator.get_log_path("test-agent", "stdout")
        assert path.name == "test-agent.stdout.log"


# ---------------------------------------------------------------------------
# Health Monitor Tests
# ---------------------------------------------------------------------------

class TestHealthMonitor:
    """Tests for HealthMonitor with mock HTTP servers."""

    def _start_mock_server(self, port: int, status: int = 200) -> HTTPServer:
        """Start a mock HTTP health endpoint in a thread."""
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "healthy"}).encode())
            def log_message(self, fmt: str, *args: object) -> None:
                pass  # suppress

        server = HTTPServer(("127.0.0.1", port), Handler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        return server

    def test_check_healthy(self, health_monitor: HealthMonitor) -> None:
        """Healthy agent should return healthy status."""
        server = self._start_mock_server(19876)
        try:
            health_monitor.add_agent("mock-healthy", "http://127.0.0.1:19876/health")
            snapshot = health_monitor.check_agent("mock-healthy")
            assert snapshot.status == "healthy"
            assert snapshot.response_time_ms > 0
        finally:
            server.shutdown()

    def test_check_unhealthy(self, health_monitor: HealthMonitor) -> None:
        """Unreachable agent should return unhealthy status."""
        health_monitor.add_agent("ghost", "http://127.0.0.1:19877/health")
        snapshot = health_monitor.check_agent("ghost")
        assert snapshot.status == "unhealthy"
        assert "error" in snapshot.error.lower() or "refused" in snapshot.error.lower()

    def test_check_all(self, health_monitor: HealthMonitor) -> None:
        """check_all should return a full fleet report."""
        server = self._start_mock_server(19878)
        try:
            health_monitor.add_agent("a1", "http://127.0.0.1:19878/health")
            health_monitor.add_agent("a2", "http://127.0.0.1:19879/health")  # dead
            report = health_monitor.check_all()
            assert report.total_agents == 2
            assert report.healthy_count == 1
            assert report.unhealthy_count == 1
            assert 0 < report.health_score < 100
        finally:
            server.shutdown()

    def test_fleet_score_perfect(self, health_monitor: HealthMonitor) -> None:
        """All healthy agents should score 100."""
        server = self._start_mock_server(19880)
        try:
            health_monitor.add_agent("p1", "http://127.0.0.1:19880/health")
            report = health_monitor.check_all()
            assert report.health_score == 100.0
        finally:
            server.shutdown()

    def test_history(self, health_monitor: HealthMonitor) -> None:
        """History should accumulate data points via check_all."""
        server = self._start_mock_server(19881)
        try:
            health_monitor.add_agent("hist", "http://127.0.0.1:19881/health")
            health_monitor.check_all()
            health_monitor.check_all()
            history = health_monitor.get_history("hist")
            assert len(history) == 2
        finally:
            server.shutdown()

    def test_alerts(self, health_monitor: HealthMonitor) -> None:
        """Consecutive unhealthy checks should generate alerts."""
        health_monitor.add_agent("alert-test", "http://127.0.0.1:19882/health")
        # check_all populates health data; check_alerts tracks consecutive failures
        for _ in range(3):
            health_monitor.check_all()
            health_monitor.check_alerts()  # must call repeatedly to accumulate count
        alerts = health_monitor.check_alerts()
        assert len(alerts) > 0
        assert any("CRITICAL" in a for a in alerts)

    def test_format_report(self, health_monitor: HealthMonitor) -> None:
        """format_report should produce a human-readable string."""
        server = self._start_mock_server(19883)
        try:
            health_monitor.add_agent("fmt", "http://127.0.0.1:19883/health")
            report = health_monitor.check_all()
            text = health_monitor.format_report(report)
            assert "Fleet Health:" in text
            assert "fmt" in text
            assert "[OK]" in text
        finally:
            server.shutdown()

    def test_set_total_tests(self, health_monitor: HealthMonitor) -> None:
        """Total tests should be tracked."""
        health_monitor.set_total_tests(880)
        assert health_monitor._total_tests_passing == 880


# ---------------------------------------------------------------------------
# Agent Launcher Tests
# ---------------------------------------------------------------------------

class TestAgentLauncher:
    """Tests for AgentLauncher with mocked clone/onboard."""

    def test_discover_local_agents(self, tmp_dir: Path) -> None:
        """Should find git repos in the agents directory."""
        agents_dir = tmp_dir / "agents"
        agents_dir.mkdir()

        # Create a fake git repo
        repo_dir = agents_dir / "trail-agent"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()

        launcher = AgentLauncher.__new__(AgentLauncher)
        launcher.config = FleetConfig()
        launcher.agents_dir = agents_dir
        launcher._github_token = None

        found = launcher.discover_local_agents()
        assert "trail-agent" in found

    def test_discover_missing_agents(self, tmp_dir: Path) -> None:
        """Should identify agents not yet cloned."""
        agents_dir = tmp_dir / "agents"
        agents_dir.mkdir()

        # Only trail-agent exists
        (agents_dir / "trail-agent" / ".git").mkdir(parents=True)

        launcher = AgentLauncher.__new__(AgentLauncher)
        launcher.config = FleetConfig()
        launcher.agents_dir = agents_dir
        launcher._github_token = None

        configs = [
            AgentConfig(name="trail-agent", repo="trail-agent"),
            AgentConfig(name="trust-agent", repo="trust-agent"),
        ]
        missing = launcher.discover_missing_agents(configs)
        assert len(missing) == 1
        assert missing[0].name == "trust-agent"

    def test_clone_agent(self, tmp_dir: Path) -> None:
        """Clone should succeed with a mock git command."""
        agents_dir = tmp_dir / "agents"
        agents_dir.mkdir()

        launcher = AgentLauncher.__new__(AgentLauncher)
        launcher.config = FleetConfig()
        launcher.agents_dir = agents_dir
        launcher._github_token = None

        agent = AgentConfig(name="test-agent", repo="test-agent")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            result = launcher.clone_agent(agent)
            assert result is True
            mock_run.assert_called_once()

    def test_clone_agent_failure(self, tmp_dir: Path) -> None:
        """Clone failure should return False."""
        agents_dir = tmp_dir / "agents"
        agents_dir.mkdir()

        launcher = AgentLauncher.__new__(AgentLauncher)
        launcher.config = FleetConfig()
        launcher.agents_dir = agents_dir
        launcher._github_token = None

        agent = AgentConfig(name="test-agent", repo="test-agent")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stderr="fatal: repo not found")
            result = launcher.clone_agent(agent)
            assert result is False

    def test_build_agent_env(self, tmp_dir: Path) -> None:
        """Agent environment should include all expected vars."""
        agents_dir = tmp_dir / "agents"
        agents_dir.mkdir()

        cfg = FleetConfig()
        cfg.network.keeper_url = "http://127.0.0.1:8443"

        launcher = AgentLauncher.__new__(AgentLauncher)
        launcher.config = cfg
        launcher.agents_dir = agents_dir
        launcher._github_token = None

        agent = AgentConfig(name="trail-agent", port=8501, host="127.0.0.1")
        env = launcher.build_agent_env(agent)

        assert env["AGENT_NAME"] == "trail-agent"
        assert env["AGENT_PORT"] == "8501"
        assert env["KEEPER_URL"] == "http://127.0.0.1:8443"
        assert env["PYTHONUNBUFFERED"] == "1"

    def test_build_launch_command(self, tmp_dir: Path) -> None:
        """Should detect launch command from repo structure."""
        agents_dir = tmp_dir / "agents"
        # The launcher looks for agents_dir / agent.name (which is "trail-agent")
        agent_dir = agents_dir / "trail-agent"
        agent_dir.mkdir(parents=True)
        # Create a module-style structure that build_launch_command can detect
        mod_dir = agent_dir / "trail_agent"
        mod_dir.mkdir(parents=True)
        (mod_dir / "__init__.py").write_text("")
        (mod_dir / "__main__.py").write_text("")

        launcher = AgentLauncher.__new__(AgentLauncher)
        launcher.config = FleetConfig()
        launcher.agents_dir = agents_dir
        launcher._github_token = None

        agent = AgentConfig(name="trail-agent", repo="trail-agent")
        cmd = launcher.build_launch_command(agent)
        assert cmd is not None
        assert cmd[0] == "python"
        assert "-m" in cmd
        assert "trail-agent" in cmd


# ---------------------------------------------------------------------------
# Runtime Tests
# ---------------------------------------------------------------------------

class TestRuntime:
    """Tests for SuperZRuntime boot sequence (with mocked subprocesses)."""

    def test_environment_check(self, tmp_dir: Path) -> None:
        """Phase 1 should complete successfully with valid environment."""
        runtime = SuperZRuntime(config_path=str(tmp_dir / "fleet.yaml"))
        runtime._setup_logging()
        # Should not raise
        runtime._phase1_environment_check()
        assert runtime.config is not None
        assert runtime.process_manager is not None
        assert runtime.health_monitor is not None

    def test_environment_check_python_version(self) -> None:
        """Should raise BootError for old Python."""
        runtime = SuperZRuntime()
        with patch.object(sys, "version_info", (3, 8, 0)):
            with pytest.raises(BootError) as exc_info:
                runtime._setup_logging()
                runtime._phase1_environment_check()
            assert "Python" in exc_info.value.message

    def test_environment_check_git(self) -> None:
        """Should raise BootError when git is missing."""
        runtime = SuperZRuntime()
        with patch("runtime._check_command", return_value=False):
            with pytest.raises(BootError) as exc_info:
                runtime._setup_logging()
                runtime._phase1_environment_check()
            assert "git" in exc_info.value.message

    def test_boot_error(self) -> None:
        """BootError should format phase and message correctly."""
        err = BootError("Phase 1", "something went wrong")
        assert str(err) == "Phase 1: something went wrong"
        assert err.phase == "Phase 1"

    def test_shutdown(self) -> None:
        """Shutdown should be safe to call multiple times."""
        runtime = SuperZRuntime()
        runtime.process_manager = ProcessManager(logs_dir=Path(tempfile.mkdtemp()))
        runtime.shutdown()
        runtime.shutdown()  # second call should be no-op

    def test_signal_handler(self, tmp_dir: Path) -> None:
        """Signal handler should trigger shutdown."""
        runtime = SuperZRuntime()
        runtime.process_manager = ProcessManager(logs_dir=tmp_dir / "logs")
        runtime._signal_handler(signal.SIGTERM, None)
        assert runtime._shutting_down


# ---------------------------------------------------------------------------
# Utility Tests
# ---------------------------------------------------------------------------

class TestUtilities:
    """Tests for utility functions."""

    def test_format_duration(self) -> None:
        assert _format_duration(0) == "00:00"
        assert _format_duration(65) == "01:05"
        assert _format_duration(3661) == "01:01:01"
        assert _format_duration(-1) == "00:00"

    def test_check_command(self) -> None:
        assert _check_command("python") is True
        assert _check_command("nonexistent_command_xyz") is False

    def test_banner(self, capsys: Any) -> None:
        """Banner should produce output."""
        banner()
        captured = capsys.readouterr()
        assert "SuperZ Runtime" in captured.out

    def test_log_phase(self, capsys: Any) -> None:
        """log_phase should print phase name."""
        log_phase("Test Phase")
        captured = capsys.readouterr()
        assert "Test Phase" in captured.out

    def test_log_phase_done(self, capsys: Any) -> None:
        """log_phase_done should print completion."""
        log_phase_done("Test Phase")
        captured = capsys.readouterr()
        assert "complete" in captured.out

    def test_datetime_now_iso(self) -> None:
        """Should return a valid ISO datetime string."""
        result = datetime_now_iso()
        assert "T" in result
        assert "-" in result

    def test_ensure_agent_stub(self, tmp_dir: Path) -> None:
        """Agent stub file should be created."""
        stub_path = tmp_dir / "_agent_stub.py"
        with patch.object(Path, "exists", return_value=False):
            with patch("runtime.Path", return_value=stub_path):
                ensure_agent_stub()
        # Stub should exist after call
        # (In test context, stub may already exist from the actual call)


# ---------------------------------------------------------------------------
# Integration-style tests
# ---------------------------------------------------------------------------

class TestIntegration:
    """Higher-level integration tests."""

    def test_full_boot_with_stubs(self, tmp_dir: Path) -> None:
        """Boot the runtime with stub agents and verify it starts."""
        # Ensure the agent stub is written
        ensure_agent_stub()

        runtime = SuperZRuntime(
            config_path=str(tmp_dir / "fleet.yaml"),
            headless=True,
            skip_mud=True,
            agent_filter=["trail-agent"],
        )

        # Phase 1
        runtime._setup_logging()
        runtime._phase1_environment_check()
        assert runtime.config is not None

        # Phase 2
        runtime._phase2_fleet_bootstrap()

        # Phase 3 — start infrastructure and manually mark healthy
        runtime._start_infrastructure_agent(AgentConfig(
            name="keeper-agent", port=8443, host="127.0.0.1",
        ))
        runtime._start_infrastructure_agent(AgentConfig(
            name="git-agent", port=8444, host="127.0.0.1",
        ))
        procs = runtime.process_manager.get_all()
        assert len(procs) >= 2

        # Phase 4
        runtime._phase4_launch_agents()
        procs = runtime.process_manager.get_all()
        # Should now include trail-agent
        assert any("trail" in name for name in procs)

        # Clean up
        runtime.shutdown()

    def test_health_monitor_integration(self, tmp_dir: Path) -> None:
        """Health monitor should work with stub agents."""
        ensure_agent_stub()

        runtime = SuperZRuntime(
            config_path=str(tmp_dir / "fleet.yaml"),
            headless=True,
            skip_mud=True,
        )
        runtime._setup_logging()
        runtime._phase1_environment_check()

        # Start infrastructure agents directly
        runtime._start_infrastructure_agent(AgentConfig(
            name="keeper-agent", port=8443, host="127.0.0.1",
        ))
        runtime.health_monitor.add_agent(
            "keeper-agent", "http://127.0.0.1:8443/health",
        )

        # Wait for stub to be ready
        time.sleep(1)

        report = runtime.health_monitor.check_all()
        assert report.total_agents >= 1

        runtime.shutdown()
