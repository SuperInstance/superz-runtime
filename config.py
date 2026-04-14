"""
Unified Fleet Configuration for SuperZ Runtime.

Bridges pelagic-bootstrap, standalone-agent-scaffold, and holodeck-studio configs
into a single fleet.yaml that the runtime consumes.
"""

from __future__ import annotations

import os
import sys
import copy
import shutil
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    """Configuration for a single fleet agent."""
    name: str = ""
    repo: str = ""
    port: int = 8500
    enabled: bool = True
    mode: str = "serve"            # serve | work | listen
    host: str = "127.0.0.1"
    branch: str = "main"
    onboarded: bool = False
    health_endpoint: str = "/health"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "repo": self.repo,
            "port": self.port,
            "enabled": self.enabled,
            "mode": self.mode,
            "host": self.host,
            "branch": self.branch,
            "onboarded": self.onboarded,
            "health_endpoint": self.health_endpoint,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AgentConfig:
        return cls(
            name=d.get("name", ""),
            repo=d.get("repo", ""),
            port=d.get("port", 8500),
            enabled=d.get("enabled", True),
            mode=d.get("mode", "serve"),
            host=d.get("host", "127.0.0.1"),
            branch=d.get("branch", "main"),
            onboarded=d.get("onboarded", False),
            health_endpoint=d.get("health_endpoint", "/health"),
        )


@dataclass
class RuntimeConfig:
    """Runtime behaviour settings."""
    headless: bool = False
    log_level: str = "INFO"
    health_interval: int = 30        # seconds
    git_sync_interval: int = 300     # seconds
    max_restart_backoff: int = 60    # seconds
    shutdown_timeout: int = 5        # seconds


@dataclass
class KeeperConfig:
    """Keeper Agent configuration."""
    host: str = "127.0.0.1"
    port: int = 8443
    vault_path: str = ""             # defaults to ~/.superinstance/vault/
    enabled: bool = True


@dataclass
class GitAgentConfig:
    """Git Agent (co-captain) configuration."""
    host: str = "127.0.0.1"
    port: int = 8444
    workshop_path: str = ""          # defaults to ~/.superinstance/workshop/
    enabled: bool = True


@dataclass
class MudConfig:
    """Holodeck MUD server configuration."""
    enabled: bool = True
    port: int = 7777
    host: str = "127.0.0.1"
    world_path: str = ""             # defaults to ~/.superinstance/worlds/


@dataclass
class NetworkConfig:
    """Fleet network topology settings."""
    topology: str = "star"           # star | mesh
    discovery: bool = True
    keeper_url: str = ""             # set at runtime


@dataclass
class SecretsConfig:
    """Secrets references (never actual secret values)."""
    keeper_url: str = ""
    github_token_env: str = "GITHUB_TOKEN"


# ---------------------------------------------------------------------------
# FleetConfig — the unified container
# ---------------------------------------------------------------------------

class FleetConfig:
    """Unified configuration for the entire Pelagic fleet.

    Loads from ``fleet.yaml`` (YAML), environment variables, or built-in
    defaults.  Validates ports, paths, and agent lists before the runtime
    can use them.

    Usage::

        cfg = FleetConfig.load("/path/to/fleet.yaml")
        print(cfg.runtime.headless)
    """

    DEFAULT_INSTANCE_DIR = Path.home() / ".superinstance"
    DEFAULT_AGENTS_DIR = DEFAULT_INSTANCE_DIR / "agents"
    DEFAULT_LOGS_DIR = DEFAULT_INSTANCE_DIR / "logs"
    DEFAULT_VAULT_DIR = DEFAULT_INSTANCE_DIR / "vault"
    DEFAULT_WORKSHOP_DIR = DEFAULT_INSTANCE_DIR / "workshop"
    DEFAULT_WORLDS_DIR = DEFAULT_INSTANCE_DIR / "worlds"
    DEFAULT_CONFIG_PATH = DEFAULT_INSTANCE_DIR / "fleet.yaml"

    # The canonical SuperInstance GitHub org for fleet agents
    GITHUB_ORG = "SuperInstance"

    # Default fleet agents
    DEFAULT_AGENTS: list[dict[str, Any]] = [
        {"name": "trail-agent",      "repo": "trail-agent",      "port": 8501, "enabled": True,  "mode": "serve"},
        {"name": "trust-agent",      "repo": "trust-agent",      "port": 8502, "enabled": True,  "mode": "serve"},
        {"name": "flux-vm-agent",    "repo": "flux-vm-agent",    "port": 8503, "enabled": True,  "mode": "serve"},
        {"name": "knowledge-agent",  "repo": "knowledge-agent",  "port": 8504, "enabled": True,  "mode": "serve"},
        {"name": "scheduler-agent",  "repo": "scheduler-agent",  "port": 8505, "enabled": True,  "mode": "serve"},
        {"name": "edge-relay",       "repo": "edge-relay",       "port": 8506, "enabled": True,  "mode": "serve"},
        {"name": "liaison-agent",    "repo": "liaison-agent",    "port": 8507, "enabled": True,  "mode": "serve"},
        {"name": "cartridge-agent",  "repo": "cartridge-agent",  "port": 8508, "enabled": True,  "mode": "serve"},
    ]

    def __init__(self) -> None:
        self.runtime: RuntimeConfig = RuntimeConfig()
        self.keeper: KeeperConfig = KeeperConfig()
        self.git_agent: GitAgentConfig = GitAgentConfig()
        self.agents: list[AgentConfig] = [
            AgentConfig.from_dict(a) for a in self.DEFAULT_AGENTS
        ]
        self.mud: MudConfig = MudConfig()
        self.network: NetworkConfig = NetworkConfig()
        self.secrets: SecretsConfig = SecretsConfig()
        self.config_path: Optional[Path] = None

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: Optional[str] = None) -> FleetConfig:
        """Load configuration from a YAML file, falling back to defaults.

        Search order:
        1. Explicit *path* argument
        2. ``FLEET_CONFIG`` environment variable
        3. ``~/.superinstance/fleet.yaml``
        4. Built-in defaults
        """
        cfg = cls()

        resolve_path = path or os.environ.get("FLEET_CONFIG")
        if resolve_path:
            cfg.config_path = Path(resolve_path).expanduser().resolve()
        else:
            cfg.config_path = cls.DEFAULT_CONFIG_PATH

        if cfg.config_path.exists():
            cfg._load_from_yaml(cfg.config_path)
            logger.info("Loaded config from %s", cfg.config_path)
        else:
            logger.info("No config file found — using defaults")

        cfg._apply_env_overrides()
        cfg._fill_defaults()
        return cfg

    def _load_from_yaml(self, path: Path) -> None:
        """Parse a YAML config file and populate fields."""
        if yaml is None:
            logger.warning("PyYAML not installed — cannot read %s", path)
            return

        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw: dict[str, Any] = yaml.safe_load(fh) or {}
        except Exception as exc:
            logger.error("Failed to parse %s: %s", path, exc)
            return

        # Runtime
        rt = raw.get("runtime", {})
        self.runtime.headless = rt.get("headless", self.runtime.headless)
        self.runtime.log_level = rt.get("log_level", self.runtime.log_level)
        self.runtime.health_interval = rt.get("health_interval", self.runtime.health_interval)
        self.runtime.git_sync_interval = rt.get("git_sync_interval", self.runtime.git_sync_interval)
        self.runtime.max_restart_backoff = rt.get("max_restart_backoff", self.runtime.max_restart_backoff)
        self.runtime.shutdown_timeout = rt.get("shutdown_timeout", self.runtime.shutdown_timeout)

        # Keeper
        kp = raw.get("keeper", {})
        self.keeper.host = kp.get("host", self.keeper.host)
        self.keeper.port = kp.get("port", self.keeper.port)
        self.keeper.vault_path = kp.get("vault_path", self.keeper.vault_path)
        self.keeper.enabled = kp.get("enabled", self.keeper.enabled)

        # Git agent
        ga = raw.get("git_agent", {})
        self.git_agent.host = ga.get("host", self.git_agent.host)
        self.git_agent.port = ga.get("port", self.git_agent.port)
        self.git_agent.workshop_path = ga.get("workshop_path", self.git_agent.workshop_path)
        self.git_agent.enabled = ga.get("enabled", self.git_agent.enabled)

        # Agents
        agents_raw = raw.get("agents", [])
        if agents_raw:
            self.agents = [AgentConfig.from_dict(a) for a in agents_raw]

        # MUD
        mud = raw.get("mud", {})
        self.mud.enabled = mud.get("enabled", self.mud.enabled)
        self.mud.port = mud.get("port", self.mud.port)
        self.mud.host = mud.get("host", self.mud.host)
        self.mud.world_path = mud.get("world_path", self.mud.world_path)

        # Network
        net = raw.get("network", {})
        self.network.topology = net.get("topology", self.network.topology)
        self.network.discovery = net.get("discovery", self.network.discovery)
        self.network.keeper_url = net.get("keeper_url", self.network.keeper_url)

        # Secrets
        sec = raw.get("secrets", {})
        self.secrets.keeper_url = sec.get("keeper_url", self.secrets.keeper_url)
        self.secrets.github_token_env = sec.get("github_token_env", self.secrets.github_token_env)

    def _apply_env_overrides(self) -> None:
        """Let environment variables override specific settings."""
        env_map: dict[str, Any] = {
            "SUPERZ_HEADLESS": ("runtime", "headless", _parse_bool),
            "SUPERZ_LOG_LEVEL": ("runtime", "log_level", str),
            "KEEPER_PORT": ("keeper", "port", int),
            "KEEPER_HOST": ("keeper", "host", str),
            "GIT_AGENT_PORT": ("git_agent", "port", int),
            "MUD_PORT": ("mud", "port", int),
            "SUPERZ_SKIP_MUD": ("mud", "enabled", lambda v: not _parse_bool(v)),
        }
        for env_var, (section, attr, parser) in env_map.items():
            val = os.environ.get(env_var)
            if val is not None:
                try:
                    setattr(getattr(self, section), attr, parser(val))
                    logger.debug("Env override: %s=%s -> %s.%s", env_var, val, section, attr)
                except (ValueError, TypeError) as exc:
                    logger.warning("Bad env var %s=%s: %s", env_var, val, exc)

    def _fill_defaults(self) -> None:
        """Resolve any still-empty paths to their defaults."""
        if not self.keeper.vault_path:
            self.keeper.vault_path = str(self.DEFAULT_VAULT_DIR)
        if not self.git_agent.workshop_path:
            self.git_agent.workshop_path = str(self.DEFAULT_WORKSHOP_DIR)
        if not self.mud.world_path:
            self.mud.world_path = str(self.DEFAULT_WORLDS_DIR)
        if not self.network.keeper_url:
            self.network.keeper_url = f"http://{self.keeper.host}:{self.keeper.port}"

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> list[str]:
        """Validate the configuration.  Returns a list of error strings
        (empty means the config is good)."""
        errors: list[str] = []
        ports_seen: dict[int, str] = {}

        def _check_port(name: str, port: int) -> None:
            if not (1 <= port <= 65535):
                errors.append(f"{name}: port {port} out of range")
                return
            if port in ports_seen:
                errors.append(f"Port conflict: {name} and {ports_seen[port]} both use port {port}")
            ports_seen[port] = name

        _check_port("keeper", self.keeper.port)
        _check_port("git_agent", self.git_agent.port)
        if self.mud.enabled:
            _check_port("mud", self.mud.port)
        for agent in self.agents:
            if agent.enabled:
                _check_port(f"agent/{agent.name}", agent.port)

        if self.runtime.health_interval < 5:
            errors.append("runtime.health_interval must be >= 5 seconds")
        if self.runtime.git_sync_interval < 30:
            errors.append("runtime.git_sync_interval must be >= 30 seconds")

        valid_topologies = {"star", "mesh"}
        if self.network.topology not in valid_topologies:
            errors.append(f"network.topology must be one of {valid_topologies}")

        return errors

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Optional[Path] = None) -> Path:
        """Write current configuration to a YAML file."""
        target = path or self.config_path or self.DEFAULT_CONFIG_PATH
        target.parent.mkdir(parents=True, exist_ok=True)

        if yaml is None:
            logger.error("PyYAML not installed — cannot save config")
            return target

        data: dict[str, Any] = {
            "runtime": {
                "headless": self.runtime.headless,
                "log_level": self.runtime.log_level,
                "health_interval": self.runtime.health_interval,
                "git_sync_interval": self.runtime.git_sync_interval,
                "max_restart_backoff": self.runtime.max_restart_backoff,
                "shutdown_timeout": self.runtime.shutdown_timeout,
            },
            "keeper": {
                "host": self.keeper.host,
                "port": self.keeper.port,
                "vault_path": self.keeper.vault_path,
                "enabled": self.keeper.enabled,
            },
            "git_agent": {
                "host": self.git_agent.host,
                "port": self.git_agent.port,
                "workshop_path": self.git_agent.workshop_path,
                "enabled": self.git_agent.enabled,
            },
            "agents": [a.to_dict() for a in self.agents],
            "mud": {
                "enabled": self.mud.enabled,
                "port": self.mud.port,
                "host": self.mud.host,
                "world_path": self.mud.world_path,
            },
            "network": {
                "topology": self.network.topology,
                "discovery": self.network.discovery,
                "keeper_url": self.network.keeper_url,
            },
            "secrets": {
                "keeper_url": self.secrets.keeper_url,
                "github_token_env": self.secrets.github_token_env,
            },
        }

        with open(target, "w", encoding="utf-8") as fh:
            yaml.dump(data, fh, default_flow_style=False, sort_keys=False)

        logger.info("Config saved to %s", target)
        return target

    def save_default(self, path: Optional[Path] = None) -> Path:
        """Generate and save a default fleet.yaml."""
        return self.save(path)

    # ------------------------------------------------------------------
    # Directory helpers
    # ------------------------------------------------------------------

    def ensure_directories(self) -> None:
        """Create all required instance directories."""
        dirs = [
            self.DEFAULT_INSTANCE_DIR,
            self.DEFAULT_AGENTS_DIR,
            self.DEFAULT_LOGS_DIR,
            Path(self.keeper.vault_path),
            Path(self.git_agent.workshop_path),
            Path(self.mud.world_path),
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
        logger.info("Instance directories ensured under %s", self.DEFAULT_INSTANCE_DIR)

    # ------------------------------------------------------------------
    # Agent filtering
    # ------------------------------------------------------------------

    def get_enabled_agents(self) -> list[AgentConfig]:
        """Return only enabled agents."""
        return [a for a in self.agents if a.enabled]

    def filter_agents(self, names: Optional[list[str]] = None) -> list[AgentConfig]:
        """Filter agents by name list.  ``None`` returns all enabled."""
        if names is None:
            return self.get_enabled_agents()
        name_set = {n.strip().lower() for n in names}
        return [a for a in self.agents if a.enabled and a.name.lower() in name_set]

    # ------------------------------------------------------------------
    # Bridge legacy paths
    # ------------------------------------------------------------------

    @classmethod
    def bridge_legacy_paths(cls) -> None:
        """Migrate ``~/.pelagic/`` content to ``~/.superinstance/`` if it
        exists and the new directory doesn't."""
        old = Path.home() / ".pelagic"
        new = cls.DEFAULT_INSTANCE_DIR
        if old.exists() and not new.exists():
            logger.info("Migrating legacy %s → %s", old, new)
            shutil.copytree(str(old), str(new))
        # Also ensure symlinks or references are noted
        pelagic_agents = old / "agents"
        if pelagic_agents.exists():
            superz_agents = cls.DEFAULT_AGENTS_DIR
            if not superz_agents.exists():
                shutil.copytree(str(pelagic_agents), str(superz_agents))
                logger.info("Copied legacy agents from %s", pelagic_agents)

    def __repr__(self) -> str:
        enabled = sum(1 for a in self.agents if a.enabled)
        return (
            f"FleetConfig(agents={len(self.agents)}, enabled={enabled}, "
            f"keeper=:{self.keeper.port}, git=:{self.git_agent.port})"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_bool(value: str) -> bool:
    """Parse common boolean representations."""
    return value.lower() in {"1", "true", "yes", "on"}
