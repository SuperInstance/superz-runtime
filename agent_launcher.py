"""
Agent Launcher — clone, onboard, and launch fleet agents.

The launcher works with the filesystem layout under ``~/.superinstance/``::

    ~/.superinstance/
    ├── agents/
    │   ├── trail-agent/
    │   ├── trust-agent/
    │   └── ...
    ├── logs/
    │   ├── trail-agent/
    │   └── ...
    ├── keeper_vault/
    └── onboard_state.json
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("superz.launcher")


class AgentLauncher:
    """Clone, onboard, and launch fleet agents.

    Parameters
    ----------
    instance_dir :
        Root of the SuperInstance data directory (``~/.superinstance``).
    github_base :
        Base URL for cloning agent repos (default GitHub HTTPS).
    """

    def __init__(
        self,
        instance_dir: str | Path = "~/.superinstance",
        github_base: str = "https://github.com",
    ) -> None:
        self._base = Path(instance_dir).expanduser().resolve()
        self._agents_dir = self._base / "agents"
        self._onboard_file = self._base / "onboard_state.json"
        self._github_base = github_base.rstrip("/")
        self._ensure_dirs()

    # ---- directory helpers -------------------------------------------------

    def _ensure_dirs(self) -> None:
        """Create base directories if they don't exist."""
        self._agents_dir.mkdir(parents=True, exist_ok=True)
        self._onboard_file.parent.mkdir(parents=True, exist_ok=True)

    # ---- discovery ---------------------------------------------------------

    def discover_agents(self) -> list[str]:
        """Return names of agent directories found in ``agents/``."""
        if not self._agents_dir.exists():
            return []
        return sorted(
            d.name
            for d in self._agents_dir.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )

    def agent_dir(self, name: str) -> Path:
        """Return the filesystem path for an agent's clone."""
        return self._agents_dir / name

    # ---- git operations ----------------------------------------------------

    def clone_agent(self, name: str, repo: str) -> bool:
        """Clone a git repo into ``~/.superinstance/agents/{name}/``.

        Parameters
        ----------
        name :
            Short name (e.g. ``trail-agent``).
        repo :
            Repo path without leading slash (e.g. ``SuperInstance/trail-agent``).

        Returns
        -------
        bool
            *True* if the clone succeeded or the dir already exists.
        """
        target = self.agent_dir(name)

        # Already cloned?
        if target.exists() and (target / ".git").is_dir():
            logger.info("%s already cloned — skipping", name)
            return True

        # Clean up partial clone
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)

        url = f"{self._github_base}/{repo}.git"
        logger.info("Cloning %s from %s …", name, url)

        try:
            result = subprocess.run(
                ["git", "clone", "--depth", "1", url, str(target)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                logger.error("git clone failed for %s: %s", name, result.stderr.strip())
                return False
        except FileNotFoundError:
            logger.error("git is not installed — cannot clone %s", name)
            return False
        except subprocess.TimeoutExpired:
            logger.error("git clone timed out for %s", name)
            return False

        logger.info("Cloned %s → %s", name, target)
        return True

    def pull_agent(self, name: str) -> bool:
        """Run ``git pull`` inside an already-cloned agent directory."""
        target = self.agent_dir(name)
        if not (target / ".git").is_dir():
            logger.warning("Cannot pull %s — not a git repo", name)
            return False

        try:
            result = subprocess.run(
                ["git", "pull"],
                cwd=str(target),
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                logger.error("git pull failed for %s: %s", name, result.stderr.strip())
                return False
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

        logger.info("Pulled latest for %s", name)
        return True

    # ---- onboarding --------------------------------------------------------

    def _load_onboard_state(self) -> dict[str, Any]:
        if self._onboard_file.exists():
            with open(self._onboard_file, "r", encoding="utf-8") as fh:
                return json.load(fh)
        return {}

    def _save_onboard_state(self, state: dict[str, Any]) -> None:
        with open(self._onboard_file, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)

    def is_onboarded(self, name: str) -> bool:
        """Check whether an agent has been onboarded."""
        state = self._load_onboard_state()
        return state.get(name, {}).get("onboarded", False)

    def mark_onboarded(self, name: str) -> None:
        """Record that an agent has been onboarded."""
        state = self._load_onboard_state()
        state.setdefault(name, {})["onboarded"] = True
        state[name]["timestamp"] = __import__("time").time()
        self._save_onboard_state(state)
        logger.info("Marked %s as onboarded", name)

    def onboard(self, name: str, repo: str) -> bool:
        """Clone (if needed) and mark as onboarded."""
        if not self.clone_agent(name, repo):
            return False
        self.mark_onboarded(name)
        return True

    # ---- launch command construction ---------------------------------------

    def build_launch_command(self, name: str, agent_config: Optional[dict[str, Any]] = None) -> str:
        """Construct a launch command for an agent.

        Order of precedence:
        1. ``agent_config["command"]`` from fleet.yaml
        2. ``python cli.py serve``
        3. ``python -m {module_name}``
        """
        if agent_config and agent_config.get("command"):
            return agent_config["command"]

        target = self.agent_dir(name)

        # Try cli.py
        if (target / "cli.py").exists():
            return "python cli.py serve"

        # Derive module name from agent dir
        module_name = name.replace("-", "_")
        return f"python -m {module_name}.serve"

    # ---- launch via ProcessManager -----------------------------------------

    def launch(
        self,
        name: str,
        port: int,
        agent_config: Optional[dict[str, Any]] = None,
        process_manager: Any = None,
    ) -> bool:
        """Full launch pipeline: discover → clone → build command → start.

        If *process_manager* is provided, the agent is started through it.
        Otherwise this only ensures the agent is cloned and onboarded.
        """
        target = self.agent_dir(name)

        # Clone if not present
        if agent_config and agent_config.get("repo"):
            if not self.clone_agent(name, agent_config["repo"]):
                return False

        if not target.exists():
            logger.error("Agent directory %s does not exist", target)
            return False

        # Build command
        cmd = self.build_launch_command(name, agent_config)
        logger.info("Launch command for %s: %s", name, cmd)

        if process_manager is not None:
            env = {"PORT": str(port), "AGENT_NAME": name}
            process_manager.register(
                name=name,
                command=cmd,
                port=port,
                cwd=str(target),
                env=env,
            )
            pid = process_manager.start_agent(name)
            if pid is None:
                return False

        self.mark_onboarded(name)
        return True
