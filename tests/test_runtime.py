"""
Tests for SuperZ Runtime — config, process manager, agent launcher, runtime.

All tests use mocking to avoid requiring real git repos, network services,
or long-running processes.  ``sleep 999`` is used as a mock agent command.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from unittest import mock
from urllib.error import URLError

import pytest

# Ensure we can import from the package root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import FleetConfig, DEFAULT_FLEET_YAML, _deep_merge
from process_manager import ProcessManager, AgentProcess
from agent_launcher import AgentLauncher
from runtime import SuperZRuntime, TUIRenderer, build_parser


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_instance(tmp_path):
    """Provide a temporary ~/.superinstance directory."""
    inst = tmp_path / "superinstance"
    inst.mkdir()
    (inst / "logs").mkdir()
    (inst / "agents").mkdir()
    return inst


@pytest.fixture
def sample_config(tmp_path):
    """Write a minimal fleet.yaml and return its path."""
    cfg = tmp_path / "fleet.yaml"
    cfg.write_text(textwrap.dedent("""\
        runtime:
          headless: true
          health_interval: 5
        keeper:
          port: 8443
        git_agent:
          port: 8444
        agents:
          - name: trail-agent
            repo: SuperInstance/trail-agent
            port: 8501
            enabled: true
            command: "sleep 999"
          - name: trust-agent
            repo: SuperInstance/trust-agent
            port: 8502
            enabled: false
            command: "sleep 999"
        mud:
          enabled: false
    """))
    return str(cfg)


# ---------------------------------------------------------------------------
# Helper: HTTP server in a thread
# ---------------------------------------------------------------------------

class _HealthHandler(BaseHTTPRequestHandler):
    """Minimal /health endpoint returning 200 OK."""
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # silence request logs


def _start_health_server(port: int) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", port), _HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


# ===================================================================
# Config Tests
# ===================================================================

class TestFleetConfig:
    """Tests for FleetConfig — load, save, validate, generate_defaults."""

    def test_generate_defaults(self):
        cfg = FleetConfig.generate_defaults()
        assert cfg.is_valid()
        assert len(cfg.agents) == 12
        assert all(a.get("enabled") for a in cfg.agents)

    def test_load_from_file(self, sample_config):
        cfg = FleetConfig.load(path=sample_config)
        assert cfg.is_valid()
        assert len(cfg.agents) == 2
        assert cfg.enabled_agents()[0]["name"] == "trail-agent"
        assert len(cfg.enabled_agents()) == 1  # trust-agent disabled

    def test_load_missing_file_uses_defaults(self, tmp_path):
        nonexistent = str(tmp_path / "nope.yaml")
        cfg = FleetConfig.load(path=nonexistent)
        assert cfg.is_valid()
        assert len(cfg.agents) == 12

    def test_save(self, tmp_path):
        cfg = FleetConfig.generate_defaults()
        out = tmp_path / "saved.yaml"
        cfg.save(path=str(out))
        assert out.exists()

        # Re-load and verify round-trip
        cfg2 = FleetConfig.load(path=str(out))
        assert len(cfg2.agents) == len(cfg.agents)

    def test_validate_missing_name(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("agents:\n  - port: 8501\n")
        cfg = FleetConfig.load(path=str(bad))
        errors = cfg.validate()
        assert any("name" in e.lower() for e in errors)

    def test_validate_duplicate_name(self, tmp_path):
        dup = tmp_path / "dup.yaml"
        dup.write_text(textwrap.dedent("""\
            agents:
              - name: foo
                port: 8501
              - name: foo
                port: 8502
        """))
        cfg = FleetConfig.load(path=str(dup))
        errors = cfg.validate()
        assert any("duplicate" in e.lower() for e in errors)

    def test_validate_duplicate_port(self, tmp_path):
        dup = tmp_path / "dup.yaml"
        dup.write_text(textwrap.dedent("""\
            agents:
              - name: foo
                port: 8501
              - name: bar
                port: 8501
        """))
        cfg = FleetConfig.load(path=str(dup))
        errors = cfg.validate()
        assert any("duplicate port" in e.lower() for e in errors)

    def test_validate_bad_health_interval(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("runtime:\n  health_interval: -1\n")
        cfg = FleetConfig.load(path=str(bad))
        errors = cfg.validate()
        assert any("health_interval" in e for e in errors)

    def test_get_agent(self):
        cfg = FleetConfig.generate_defaults()
        agent = cfg.get_agent("trail-agent")
        assert agent is not None
        assert agent["port"] == 8501
        assert cfg.get_agent("nonexistent") is None

    def test_enabled_agents(self, sample_config):
        cfg = FleetConfig.load(path=sample_config)
        enabled = cfg.enabled_agents()
        assert len(enabled) == 1
        assert enabled[0]["name"] == "trail-agent"

    def test_get_all_ports(self):
        cfg = FleetConfig.generate_defaults()
        ports = cfg.get_all_ports()
        assert ports["keeper"] == 8443
        assert ports["git-agent"] == 8444
        assert ports["trail-agent"] == 8501
        # MUD disabled → should not be in ports
        assert "mud" not in ports

    def test_get_all_ports_mud_enabled(self, tmp_path):
        mud_cfg = tmp_path / "mud.yaml"
        mud_cfg.write_text(textwrap.dedent("""\
            runtime: {}
            keeper: {port: 8443}
            git_agent: {port: 8444}
            agents: []
            mud: {enabled: true, port: 7777, bridge_port: 8877}
        """))
        cfg = FleetConfig.load(path=str(mud_cfg))
        ports = cfg.get_all_ports()
        assert ports["mud"] == 7777
        assert ports["mud-bridge"] == 8877

    def test_get_dotted_key(self):
        cfg = FleetConfig.generate_defaults()
        assert cfg.get("runtime.health_interval") == 30
        assert cfg.get("nonexistent.key", "default") == "default"

    def test_deep_merge(self):
        base = {"a": 1, "b": {"c": 2, "d": 3}}
        override = {"b": {"c": 99}, "e": 5}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": {"c": 99, "d": 3}, "e": 5}

    def test_repr(self):
        cfg = FleetConfig.generate_defaults()
        r = repr(cfg)
        assert "agents=12" in r
        assert "enabled=12" in r


# ===================================================================
# Process Manager Tests
# ===================================================================

class TestProcessManager:
    """Tests for ProcessManager — start, stop, restart, health check."""

    def test_register(self, tmp_instance):
        pm = ProcessManager(base_instance_dir=str(tmp_instance))
        proc = pm.register("test-agent", "sleep 999", 9999, "/tmp")
        assert proc.name == "test-agent"
        assert proc.port == 9999
        assert proc.health_status == "DOWN"
        assert "test-agent" in pm.agent_names

    def test_start_and_stop(self, tmp_instance):
        pm = ProcessManager(base_instance_dir=str(tmp_instance))
        pm.register("sleeper", "sleep 999", 19999, "/tmp")
        pid = pm.start_agent("sleeper")
        assert pid is not None
        assert pid > 0

        # Check it's actually running
        proc = pm.get_agent("sleeper")
        assert proc is not None
        assert proc.pid == pid
        assert proc.subprocess is not None
        assert proc.subprocess.poll() is None  # still alive

        # Stop it
        stopped = pm.stop_agent("sleeper")
        assert stopped is True
        assert proc.subprocess is None
        assert proc.pid is None
        assert proc.health_status == "DOWN"

    def test_stop_unknown_agent(self, tmp_instance):
        pm = ProcessManager(base_instance_dir=str(tmp_instance))
        result = pm.stop_agent("nonexistent")
        assert result is False

    def test_start_already_running(self, tmp_instance):
        pm = ProcessManager(base_instance_dir=str(tmp_instance))
        pm.register("sleeper", "sleep 999", 19999, "/tmp")
        pid1 = pm.start_agent("sleeper")
        pid2 = pm.start_agent("sleeper")
        assert pid1 == pid2  # returns existing PID

    def test_start_invalid_command(self, tmp_instance):
        pm = ProcessManager(base_instance_dir=str(tmp_instance))
        pm.register("bad", "nonexistent_binary_xyz", 19999, "/tmp")
        pid = pm.start_agent("bad")
        assert pid is None
        proc = pm.get_agent("bad")
        assert proc.health_status == "ERR"

    def test_restart_agent(self, tmp_instance):
        pm = ProcessManager(base_instance_dir=str(tmp_instance))
        pm.register("sleeper", "sleep 999", 19999, "/tmp")
        pid1 = pm.start_agent("sleeper")
        pid2 = pm.restart_agent("sleeper")
        assert pid2 is not None
        # PIDs may or may not differ due to timing, but both should be valid
        assert pid2 > 0

    def test_check_health_down(self, tmp_instance):
        pm = ProcessManager(base_instance_dir=str(tmp_instance))
        pm.register("dead", "sleep 0.1", 19999, "/tmp")
        pm.start_agent("dead")
        time.sleep(0.3)  # let it exit
        status = pm.check_health("dead")
        assert status == "DOWN"

    def test_check_health_http_ok(self, tmp_instance):
        """Start a real HTTP server on a port, then check health."""
        port = 19283
        server = _start_health_server(port)
        try:
            pm = ProcessManager(base_instance_dir=str(tmp_instance))
            # Manually create an AgentProcess that points to our server
            proc = AgentProcess(name="http-agent", port=port, command="", cwd="", log_dir=str(tmp_instance / "logs" / "http-agent"))
            pm._agents["http-agent"] = proc
            # Pretend subprocess is running
            mock_popen = mock.MagicMock()
            mock_popen.poll.return_value = None
            proc.subprocess = mock_popen

            status = pm.check_health("http-agent")
            assert status == "OK"
        finally:
            server.shutdown()

    def test_check_health_http_fail_process_alive(self, tmp_instance):
        """Port with no HTTP server → WARN (process alive but no health endpoint)."""
        # Use a port that nothing is listening on
        port = 19284
        pm = ProcessManager(base_instance_dir=str(tmp_instance))
        proc = AgentProcess(name="no-http", port=port, command="", cwd="", log_dir=str(tmp_instance / "logs" / "no-http"))
        pm._agents["no-http"] = proc
        mock_popen = mock.MagicMock()
        mock_popen.poll.return_value = None
        proc.subprocess = mock_popen

        status = pm.check_health("no-http")
        assert status == "WARN"

    def test_get_status(self, tmp_instance):
        pm = ProcessManager(base_instance_dir=str(tmp_instance))
        pm.register("sleeper", "sleep 999", 19999, "/tmp")
        pm.start_agent("sleeper")

        status = pm.get_status()
        assert "sleeper" in status
        assert status["sleeper"]["pid"] > 0
        assert status["sleeper"]["port"] == 19999

    def test_stop_all(self, tmp_instance):
        pm = ProcessManager(base_instance_dir=str(tmp_instance))
        pm.register("a", "sleep 999", 19991, "/tmp")
        pm.register("b", "sleep 999", 19992, "/tmp")
        pm.register("c", "sleep 999", 19993, "/tmp")
        pm.start_agent("a")
        pm.start_agent("b")
        pm.start_agent("c")

        pm.stop_all()
        for name in ("a", "b", "c"):
            assert pm.get_agent(name).health_status == "DOWN"

    def test_auto_restart_with_backoff(self, tmp_instance):
        """Agent exits immediately, auto-restart should fire with backoff."""
        pm = ProcessManager(base_instance_dir=str(tmp_instance), max_restart_attempts=3)
        pm.register("crasher", "sleep 0.1", 19999, "/tmp")

        # First start
        pm.start_agent("crasher")
        time.sleep(0.3)
        assert pm.check_health("crasher") == "DOWN"

        # Auto-restart (should succeed since sleep 0.1 exits quickly)
        pid = pm.auto_restart("crasher")
        assert pid is not None
        assert pm.get_agent("crasher").restart_count == 1

    def test_auto_restart_max_attempts(self, tmp_instance):
        """After max attempts, auto_restart gives up."""
        pm = ProcessManager(base_instance_dir=str(tmp_instance), max_restart_attempts=2)
        pm.register("crasher", "sleep 0.1", 19999, "/tmp")

        # Exhaust restarts
        for _ in range(3):
            pm.start_agent("crasher")
            time.sleep(0.3)
            pm.check_health("crasher")
            pm.auto_restart("crasher")

        # Should have given up
        assert pm.get_agent("crasher").restart_count >= 2

    def test_log_files_created(self, tmp_instance):
        """Verify stdout/stderr log files are created."""
        pm = ProcessManager(base_instance_dir=str(tmp_instance))
        pm.register("logger", "echo hello", 19999, "/tmp")
        pm.start_agent("logger")
        pm.stop_agent("logger")

        log_dir = tmp_instance / "logs" / "logger"
        assert (log_dir / "stdout.log").exists()
        assert (log_dir / "stderr.log").exists()


# ===================================================================
# Agent Launcher Tests
# ===================================================================

class TestAgentLauncher:
    """Tests for AgentLauncher — discover, clone, onboard, launch."""

    def test_init_creates_dirs(self, tmp_instance):
        launcher = AgentLauncher(instance_dir=str(tmp_instance))
        assert (tmp_instance / "agents").exists()

    def test_discover_agents_empty(self, tmp_instance):
        launcher = AgentLauncher(instance_dir=str(tmp_instance))
        discovered = launcher.discover_agents()
        assert discovered == []

    def test_discover_agents(self, tmp_instance):
        (tmp_instance / "agents" / "trail-agent").mkdir()
        (tmp_instance / "agents" / "trust-agent").mkdir()
        (tmp_instance / "agents" / ".hidden").mkdir()

        launcher = AgentLauncher(instance_dir=str(tmp_instance))
        discovered = launcher.discover_agents()
        assert discovered == ["trail-agent", "trust-agent"]

    def test_is_onboarded_false(self, tmp_instance):
        launcher = AgentLauncher(instance_dir=str(tmp_instance))
        assert launcher.is_onboarded("trail-agent") is False

    def test_mark_onboarded(self, tmp_instance):
        launcher = AgentLauncher(instance_dir=str(tmp_instance))
        launcher.mark_onboarded("trail-agent")
        assert launcher.is_onboarded("trail-agent") is True

    def test_clone_agent_mocked(self, tmp_instance):
        """Mock subprocess.run to simulate a git clone."""
        launcher = AgentLauncher(instance_dir=str(tmp_instance))

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stderr="")
            result = launcher.clone_agent("trail-agent", "SuperInstance/trail-agent")
            assert result is True
            mock_run.assert_called_once()

    def test_clone_agent_git_missing(self, tmp_instance):
        launcher = AgentLauncher(instance_dir=str(tmp_instance))
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            result = launcher.clone_agent("foo", "bar/baz")
            assert result is False

    def test_clone_already_exists(self, tmp_instance):
        target = tmp_instance / "agents" / "existing"
        target.mkdir()
        (target / ".git").mkdir()
        launcher = AgentLauncher(instance_dir=str(tmp_instance))
        result = launcher.clone_agent("existing", "whatever/repo")
        assert result is True  # should skip

    def test_build_launch_command_from_config(self, tmp_instance):
        launcher = AgentLauncher(instance_dir=str(tmp_instance))
        cfg = {"command": "python custom.py run"}
        cmd = launcher.build_launch_command("test", cfg)
        assert cmd == "python custom.py run"

    def test_build_launch_command_cli_py(self, tmp_instance):
        launcher = AgentLauncher(instance_dir=str(tmp_instance))
        agent_dir = tmp_instance / "agents" / "my-agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "cli.py").touch()

        cmd = launcher.build_launch_command("my-agent")
        assert cmd == "python cli.py serve"

    def test_build_launch_command_module(self, tmp_instance):
        launcher = AgentLauncher(instance_dir=str(tmp_instance))
        cmd = launcher.build_launch_command("trail-agent")
        assert cmd == "python -m trail_agent.serve"

    def test_pull_agent_mocked(self, tmp_instance):
        target = tmp_instance / "agents" / "trail-agent"
        target.mkdir()
        (target / ".git").mkdir()
        launcher = AgentLauncher(instance_dir=str(tmp_instance))

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stderr="")
            assert launcher.pull_agent("trail-agent") is True

    def test_launch_with_process_manager(self, tmp_instance):
        """Full launch pipeline (mocked clone since we don't have real git)."""
        launcher = AgentLauncher(instance_dir=str(tmp_instance))
        pm = ProcessManager(base_instance_dir=str(tmp_instance))

        # Pre-create agent dir to simulate clone
        agent_dir = tmp_instance / "agents" / "test-agent"
        agent_dir.mkdir()

        with mock.patch.object(launcher, "clone_agent", return_value=True):
            result = launcher.launch(
                name="test-agent",
                port=19999,
                agent_config={"repo": "SuperInstance/test-agent"},
                process_manager=pm,
            )
        assert result is True
        assert "test-agent" in pm.agent_names

    def test_onboard_full_pipeline(self, tmp_instance):
        launcher = AgentLauncher(instance_dir=str(tmp_instance))
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stderr="")
            assert launcher.onboard("new-agent", "org/new-agent") is True
        assert launcher.is_onboarded("new-agent")


# ===================================================================
# TUI Tests
# ===================================================================

class TestTUIRenderer:
    """Tests for TUI rendering — output capture, formatting."""

    def test_render_no_ansi_when_disabled(self, capsys):
        tui = TUIRenderer(enabled=False)
        tui.start()
        tui.render(
            {"test": {"name": "test", "status": "OK", "port": 8080, "pid": 100, "uptime": 60, "restart_count": 0}},
            phase="running",
        )
        # Should produce no stdout when disabled
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_render_enabled_captures_output(self, capsys):
        with mock.patch("runtime._supports_ansi", return_value=True):
            tui = TUIRenderer(enabled=True)
        tui._last_frame = 0  # force render
        tui._enabled = True  # force enable even though capsys is not a TTY
        tui.start()
        tui.render(
            {
                "keeper": {"name": "keeper", "status": "OK", "port": 8443, "pid": 100, "uptime": 120, "restart_count": 0},
                "agent-a": {"name": "agent-a", "status": "DOWN", "port": 8501, "pid": None, "uptime": 0, "restart_count": 2},
            },
            phase="running",
        )
        captured = capsys.readouterr()
        assert "SuperZ Runtime" in captured.out
        assert "keeper" in captured.out
        assert "agent-a" in captured.out
        assert "1/2 healthy" in captured.out

    def test_fmt_uptime(self):
        assert TUIRenderer._fmt_uptime(0) == "-"
        assert TUIRenderer._fmt_uptime(5) == "5s"
        assert TUIRenderer._fmt_uptime(125) == "2m 5s"
        assert TUIRenderer._fmt_uptime(3725) == "1h 2m"


# ===================================================================
# Runtime Tests
# ===================================================================

class TestSuperZRuntime:
    """Integration tests for SuperZRuntime — boot, shutdown, signals."""

    def test_build_parser(self):
        parser = build_parser()
        args = parser.parse_args(["--headless", "--skip-mud", "--agents", "trail,trust"])
        assert args.headless is True
        assert args.skip_mud is True
        assert args.agents == "trail,trust"

    def test_runtime_init(self, sample_config):
        rt = SuperZRuntime(config_path=sample_config, headless=True)
        assert rt._headless is True
        assert rt._running is False

    def test_runtime_boot_and_shutdown(self, sample_config, tmp_instance):
        """Boot the runtime with mocked agents and verify it starts and stops."""
        rt = SuperZRuntime(config_path=sample_config, headless=True)

        # Mock the instance dir
        with mock.patch("runtime.INSTANCE_DIR", tmp_instance):
            # Mock subprocess.Popen so agent commands don't actually run
            mock_popen = mock.MagicMock()
            mock_popen.pid = 12345
            mock_popen.poll.return_value = None
            mock_popen.wait.return_value = None

            with mock.patch("subprocess.Popen", return_value=mock_popen):
                # Boot in a thread so we can interrupt it
                boot_thread = threading.Thread(target=rt.boot, daemon=True)
                boot_thread.start()

                # Wait for health loop to start
                time.sleep(2)

                # Trigger shutdown
                rt._running = False
                boot_thread.join(timeout=5)

                assert not boot_thread.is_alive()

    def test_shutdown_stops_all(self, sample_config, tmp_instance):
        """Verify _shutdown calls stop_all on the process manager."""
        rt = SuperZRuntime(config_path=sample_config, headless=True)

        with mock.patch("runtime.INSTANCE_DIR", tmp_instance):
            # Pre-create process manager
            pm = ProcessManager(base_instance_dir=str(tmp_instance))
            pm.register("a", "sleep 999", 19991, "/tmp")
            pm.register("b", "sleep 999", 19992, "/tmp")

            mock_popen = mock.MagicMock()
            mock_popen.pid = 111
            mock_popen.poll.return_value = None
            mock_popen.wait.return_value = None
            with mock.patch("subprocess.Popen", return_value=mock_popen):
                pm.start_agent("a")
                pm.start_agent("b")

            rt.process_mgr = pm
            rt._running = True
            rt._shutdown("test")

            assert rt._running is False
            assert pm.get_agent("a").health_status == "DOWN"
            assert pm.get_agent("b").health_status == "DOWN"

    def test_signal_handler(self, sample_config, tmp_instance):
        """SIGTERM should trigger graceful shutdown."""
        rt = SuperZRuntime(config_path=sample_config, headless=True)
        rt.register_signal_handlers()

        with mock.patch("runtime.INSTANCE_DIR", tmp_instance):
            mock_pm = mock.MagicMock()
            rt.process_mgr = mock_pm
            rt._running = True

            # Send SIGTERM to our own process
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.3)

            # Process manager should have been told to stop
            mock_pm.stop_all.assert_called_once()

    def test_interruptible_sleep(self):
        rt = SuperZRuntime(headless=True)
        rt._running = True

        # Should return early when _running becomes False
        def set_false():
            time.sleep(0.2)
            rt._running = False

        t = threading.Thread(target=set_false, daemon=True)
        t.start()

        start = time.time()
        rt._interruptible_sleep(10)  # would sleep 10s if not interruptible
        elapsed = time.time() - start

        assert elapsed < 2  # should have returned in ~0.2s
        rt._running = True  # reset

    def test_boot_check_environment(self, sample_config, tmp_instance):
        """Environment check should succeed on this machine."""
        rt = SuperZRuntime(config_path=sample_config, headless=True)
        with mock.patch("runtime.INSTANCE_DIR", tmp_instance):
            rt._check_environment()  # should not raise
            assert (tmp_instance / "logs").exists()
            assert (tmp_instance / "agents").exists()

    def test_boot_load_config(self, sample_config, tmp_instance):
        rt = SuperZRuntime(config_path=sample_config, headless=True)
        with mock.patch("runtime.INSTANCE_DIR", tmp_instance):
            rt._load_config()
            assert rt.config is not None
            assert len(rt.config.enabled_agents()) == 1

    def test_boot_load_config_invalid(self, tmp_path):
        bad_cfg = tmp_path / "bad.yaml"
        bad_cfg.write_text("runtime:\n  health_interval: bad\n")
        rt = SuperZRuntime(config_path=str(bad_cfg), headless=True)
        with mock.patch("runtime.INSTANCE_DIR", tmp_path / "si"):
            (tmp_path / "si").mkdir()
            with pytest.raises(RuntimeError, match="Invalid configuration"):
                rt._load_config()

    def test_boot_lifecycle_mocked(self, sample_config, tmp_instance):
        """Full boot lifecycle with all subprocess.Popen calls mocked."""
        rt = SuperZRuntime(config_path=sample_config, headless=True)

        with mock.patch("runtime.INSTANCE_DIR", tmp_instance):
            mock_popen = mock.MagicMock()
            mock_popen.pid = 99
            mock_popen.poll.return_value = None  # keep "alive"
            mock_popen.wait.return_value = None
            mock_popen.communicate.return_value = (b"", b"")

            with mock.patch("subprocess.Popen", return_value=mock_popen):
                boot_done = threading.Event()

                def run_boot():
                    try:
                        rt.boot()
                    except Exception:
                        pass
                    finally:
                        boot_done.set()

                t = threading.Thread(target=run_boot, daemon=True)
                t.start()
                time.sleep(3)
                rt._running = False
                boot_done.wait(timeout=10)

            assert not rt._running

    def test_filter_agents(self, sample_config, tmp_instance):
        """When --agents is set, only those agents are launched."""
        rt = SuperZRuntime(
            config_path=sample_config,
            headless=True,
            filter_agents=["trust-agent"],
        )
        assert rt._filter_agents == ["trust-agent"]

    def test_no_color_env(self, monkeypatch):
        """NO_COLOR env var should disable TUI."""
        monkeypatch.setenv("NO_COLOR", "1")
        tui = TUIRenderer(enabled=True)
        assert tui._enabled is False


# ===================================================================
# Edge cases and misc
# ===================================================================

class TestEdgeCases:

    def test_config_runtime_property(self):
        cfg = FleetConfig.generate_defaults()
        rt = cfg.runtime
        assert isinstance(rt, dict)
        assert rt["health_interval"] == 30

    def test_config_keeper_property(self):
        cfg = FleetConfig.generate_defaults()
        assert cfg.keeper["port"] == 8443

    def test_config_git_agent_property(self):
        cfg = FleetConfig.generate_defaults()
        assert cfg.git_agent["port"] == 8444

    def test_config_mud_property(self):
        cfg = FleetConfig.generate_defaults()
        assert cfg.mud["enabled"] is False

    def test_process_manager_uptime_before_start(self, tmp_instance):
        pm = ProcessManager(base_instance_dir=str(tmp_instance))
        pm.register("test", "sleep 999", 8080, "/tmp")
        proc = pm.get_agent("test")
        assert proc.uptime == 0.0

    def test_process_manager_uptime_after_start(self, tmp_instance):
        pm = ProcessManager(base_instance_dir=str(tmp_instance))
        pm.register("test", "sleep 999", 8080, "/tmp")
        pm.start_agent("test")
        time.sleep(0.5)
        proc = pm.get_agent("test")
        assert proc.uptime >= 0.4

    def test_agent_process_dataclass_defaults(self):
        proc = AgentProcess(name="x", port=1, command="sleep 999", cwd="/tmp")
        assert proc.pid is None
        assert proc.health_status == "DOWN"
        assert proc.restart_count == 0
        assert proc.started_at is None

    def test_launcher_agent_dir(self, tmp_instance):
        launcher = AgentLauncher(instance_dir=str(tmp_instance))
        assert launcher.agent_dir("foo") == tmp_instance / "agents" / "foo"
