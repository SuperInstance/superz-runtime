"""
Unified Fleet Configuration — load, save, validate, generate defaults.

Handles fleet.yaml parsing with type-safe accessors and sensible defaults
so the runtime can always boot even without a config file.
"""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger("superz.config")

# ---------------------------------------------------------------------------
# Default configuration (merged into any user-provided fleet.yaml)
# ---------------------------------------------------------------------------

DEFAULT_FLEET_YAML = """\
runtime:
  headless: false
  log_level: INFO
  health_interval: 30
  git_sync_interval: 300
  auto_restart: true
  max_restart_attempts: 5
  restart_backoff_max: 60

keeper:
  port: 8443
  vault_path: ~/.superinstance/keeper_vault

git_agent:
  port: 8444

agents:
  - name: trail-agent
    repo: SuperInstance/trail-agent
    port: 8501
    enabled: true
    command: "python -m trail_agent.serve"
  - name: trust-agent
    repo: SuperInstance/trust-agent
    port: 8502
    enabled: true
    command: "python -m trust_agent.serve"
  - name: compass-agent
    repo: SuperInstance/compass-agent
    port: 8503
    enabled: true
    command: "python -m compass_agent.serve"
  - name: echo-agent
    repo: SuperInstance/echo-agent
    port: 8504
    enabled: true
    command: "python -m echo_agent.serve"
  - name: atlas-agent
    repo: SuperInstance/atlas-agent
    port: 8505
    enabled: true
    command: "python -m atlas_agent.serve"
  - name: beacon-agent
    repo: SuperInstance/beacon-agent
    port: 8506
    enabled: true
    command: "python -m beacon_agent.serve"
  - name: scope-agent
    repo: SuperInstance/scope-agent
    port: 8507
    enabled: true
    command: "python -m scope_agent.serve"
  - name: forge-agent
    repo: SuperInstance/forge-agent
    port: 8508
    enabled: true
    command: "python -m forge_agent.serve"
  - name: vault-agent
    repo: SuperInstance/vault-agent
    port: 8509
    enabled: true
    command: "python -m vault_agent.serve"
  - name: tide-agent
    repo: SuperInstance/tide-agent
    port: 8510
    enabled: true
    command: "python -m tide_agent.serve"
  - name: helm-agent
    repo: SuperInstance/helm-agent
    port: 8511
    enabled: true
    command: "python -m helm_agent.serve"
  - name: crest-agent
    repo: SuperInstance/crest-agent
    port: 8512
    enabled: true
    command: "python -m crest_agent.serve"

mud:
  enabled: false
  port: 7777
  bridge_port: 8877
  holodeck_path: holodeck-studio/server.py
"""

# Reserved port ranges
_KEEPER_PORT = 8443
_GIT_AGENT_PORT = 8444


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base* (base values are defaults)."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class FleetConfig:
    """Load, save, validate, and query fleet configuration.

    Usage::

        cfg = FleetConfig.load(path="fleet.yaml")
        agents = cfg.enabled_agents()
        port = cfg.get_agent("trail-agent")["port"]
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self._data: dict[str, Any] = data

    # ---- factory helpers ---------------------------------------------------

    @classmethod
    def load(cls, path: Optional[str | Path] = None) -> "FleetConfig":
        """Load from *path*, or generate built-in defaults when missing."""
        if path is None:
            path = Path.cwd() / "fleet.yaml"
        path = Path(path).expanduser().resolve()

        if path.exists():
            logger.info("Loading config from %s", path)
            with open(path, "r", encoding="utf-8") as fh:
                user_data: dict[str, Any] = yaml.safe_load(fh) or {}
        else:
            logger.warning("No config at %s — using defaults", path)
            user_data = {}

        defaults: dict[str, Any] = yaml.safe_load(DEFAULT_FLEET_YAML)
        merged = _deep_merge(defaults, user_data)
        return cls(merged)

    @classmethod
    def generate_defaults(cls) -> "FleetConfig":
        """Return a config with only built-in defaults."""
        return cls(yaml.safe_load(DEFAULT_FLEET_YAML))

    # ---- persistence -------------------------------------------------------

    def save(self, path: Optional[str | Path] = None) -> Path:
        """Write current config to *path* (default ``fleet.yaml`` in cwd)."""
        if path is None:
            path = Path.cwd() / "fleet.yaml"
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            yaml.dump(self._data, fh, default_flow_style=False, sort_keys=False)
        logger.info("Config saved to %s", path)
        return path

    # ---- validation --------------------------------------------------------

    def validate(self) -> list[str]:
        """Return a list of validation error strings (empty = valid)."""
        errors: list[str] = []

        # runtime section
        rt = self._data.get("runtime", {})
        if not isinstance(rt, dict):
            errors.append("'runtime' section must be a mapping")
        else:
            hi = rt.get("health_interval")
            if hi is not None and (not isinstance(hi, int) or hi < 1):
                errors.append("runtime.health_interval must be a positive integer")

        # keeper
        keeper = self._data.get("keeper", {})
        if not isinstance(keeper.get("port"), int):
            errors.append("keeper.port must be an integer")

        # git_agent
        ga = self._data.get("git_agent", {})
        if not isinstance(ga.get("port"), int):
            errors.append("git_agent.port must be an integer")

        # agents
        agents = self._data.get("agents", [])
        if not isinstance(agents, list):
            errors.append("'agents' must be a list")
        else:
            seen_names: set[str] = set()
            seen_ports: set[int] = set()
            for i, agent in enumerate(agents):
                if not isinstance(agent, dict):
                    errors.append(f"agents[{i}] must be a mapping")
                    continue
                name = agent.get("name")
                if not name or not isinstance(name, str):
                    errors.append(f"agents[{i}].name is missing or not a string")
                    continue
                if name in seen_names:
                    errors.append(f"Duplicate agent name: {name}")
                seen_names.add(name)

                port = agent.get("port")
                if port is None or not isinstance(port, int):
                    errors.append(f"{name}: port must be an integer")
                elif port in seen_ports:
                    errors.append(f"{name}: duplicate port {port}")
                seen_ports.add(port)

        # mud
        mud = self._data.get("mud", {})
        if not isinstance(mud, dict):
            errors.append("'mud' section must be a mapping")

        return errors

    def is_valid(self) -> bool:
        return len(self.validate()) == 0

    # ---- accessors ---------------------------------------------------------

    def get(self, dotted_key: str, default: Any = None) -> Any:
        """Retrieve a nested value via ``dotted.key.path``."""
        keys = dotted_key.split(".")
        node: Any = self._data
        for k in keys:
            if isinstance(node, dict):
                node = node.get(k)
                if node is None:
                    return default
            else:
                return default
        return node

    @property
    def runtime(self) -> dict[str, Any]:
        return self._data.get("runtime", {})

    @property
    def keeper(self) -> dict[str, Any]:
        return self._data.get("keeper", {})

    @property
    def git_agent(self) -> dict[str, Any]:
        return self._data.get("git_agent", {})

    @property
    def agents(self) -> list[dict[str, Any]]:
        return self._data.get("agents", [])

    @property
    def mud(self) -> dict[str, Any]:
        return self._data.get("mud", {})

    def enabled_agents(self) -> list[dict[str, Any]]:
        """Return only agents with ``enabled: true``."""
        return [a for a in self.agents if a.get("enabled", False)]

    def get_agent(self, name: str) -> Optional[dict[str, Any]]:
        """Look up an agent by name (case-sensitive)."""
        for agent in self.agents:
            if agent.get("name") == name:
                return agent
        return None

    def get_all_ports(self) -> dict[str, int]:
        """Return ``{service_name: port}`` for every known service."""
        ports: dict[str, int] = {}
        ports["keeper"] = self.keeper.get("port", _KEEPER_PORT)
        ports["git-agent"] = self.git_agent.get("port", _GIT_AGENT_PORT)
        for agent in self.agents:
            name = agent.get("name", "unknown")
            port = agent.get("port")
            if port is not None:
                ports[name] = port
        if self.mud.get("enabled", False):
            ports["mud"] = self.mud.get("port", 7777)
            ports["mud-bridge"] = self.mud.get("bridge_port", 8877)
        return ports

    # ---- repr --------------------------------------------------------------

    def __repr__(self) -> str:
        agent_count = len(self.agents)
        enabled_count = len(self.enabled_agents())
        return (
            f"FleetConfig(agents={agent_count}, "
            f"enabled={enabled_count}, "
            f"mud={'on' if self.mud.get('enabled') else 'off'})"
        )
