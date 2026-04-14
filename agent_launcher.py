"""
Agent Launcher for SuperZ Runtime.

Discovers, clones, onboards, and launches fleet agents from their repos.
Handles the full lifecycle from GitHub clone to running subprocess.
"""

from __future__ import annotations

import os
import sys
import shutil
import subprocess
import logging
from pathlib import Path
from typing import Any, Optional

from config import FleetConfig, AgentConfig

logger = logging.getLogger(__name__)

# Possible entry points to try when launching an agent (in order)
LAUNCH_COMMANDS = [
    # Module-style: python -m <name> serve
    ["python", "-m", "{name}", "serve"],
    # CLI script: python cli.py serve
    ["python", "cli.py", "serve"],
    # Standalone server: python server.py
    ["python", "server.py"],
    # Main module: python main.py serve
    ["python", "main.py", "serve"],
]


class AgentLauncher:
    """Launches fleet agents from their cloned repos.

    Responsibilities:
    - Discover available agents (scan directory or GitHub API)
    - Clone missing agents from the SuperInstance org
    - Run ``--onboard`` if the agent hasn't been set up
    - Build the correct launch command
    - Inject config into agent's environment
    - Verify the agent started successfully
    """

    def __init__(self, config: FleetConfig) -> None:
        self.config = config
        self.agents_dir = FleetConfig.DEFAULT_AGENTS_DIR
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        self._github_token: Optional[str] = os.environ.get(
            config.secrets.github_token_env
        )

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_local_agents(self) -> list[str]:
        """Scan the agents directory for already-cloned agent repos.

        Returns a list of agent directory names.
        """
        found: list[str] = []
        if not self.agents_dir.exists():
            return found
        for entry in self.agents_dir.iterdir():
            if entry.is_dir() and (entry / ".git").exists():
                found.append(entry.name)
        return sorted(found)

    def discover_missing_agents(self, agent_configs: list[AgentConfig]) -> list[AgentConfig]:
        """Return agents that aren't cloned locally yet."""
        local = set(self.discover_local_agents())
        missing: list[AgentConfig] = []
        for agent in agent_configs:
            if agent.name not in local:
                missing.append(agent)
        return missing

    # ------------------------------------------------------------------
    # Cloning
    # ------------------------------------------------------------------

    def clone_agent(self, agent: AgentConfig) -> bool:
        """Clone an agent repo from GitHub.  Returns True on success."""
        repo_url = self._build_repo_url(agent)
        target = self.agents_dir / agent.name

        if target.exists():
            logger.info("Agent %s already exists at %s — skipping clone", agent.name, target)
            return True

        logger.info("Cloning %s from %s → %s", agent.name, repo_url, target)
        try:
            env = os.environ.copy()
            cmd = ["git", "clone", "--depth", "1", "--branch", agent.branch, repo_url, str(target)]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
            if result.returncode != 0:
                # Fallback: try without branch (e.g. default branch)
                logger.warning(
                    "Clone with branch '%s' failed (rc=%d): %s",
                    agent.branch, result.returncode, result.stderr.strip(),
                )
                cmd = ["git", "clone", "--depth", "1", repo_url, str(target)]
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env=env,
                )
                if result.returncode != 0:
                    logger.error("Clone failed for %s: %s", agent.name, result.stderr.strip())
                    return False

            logger.info("Cloned %s successfully", agent.name)
            return True

        except subprocess.TimeoutExpired:
            logger.error("Clone timeout for %s", agent.name)
            return False
        except Exception as exc:
            logger.error("Clone error for %s: %s", agent.name, exc)
            return False

    def clone_missing(self, agent_configs: list[AgentConfig]) -> dict[str, bool]:
        """Clone all missing agents.  Returns {name: success}."""
        missing = self.discover_missing_agents(agent_configs)
        results: dict[str, bool] = {}
        for agent in missing:
            results[agent.name] = self.clone_agent(agent)
        return results

    # ------------------------------------------------------------------
    # Onboarding
    # ------------------------------------------------------------------

    def onboard_agent(self, agent: AgentConfig) -> bool:
        """Run the agent's onboard command if needed."""
        target = self.agents_dir / agent.name
        if not target.exists():
            logger.warning("Cannot onboard %s — not cloned", agent.name)
            return False

        # Check if already onboarded (look for common markers)
        onboarded = self._check_onboarded(target)
        if onboarded:
            logger.info("%s already onboarded — skipping", agent.name)
            return True

        logger.info("Onboarding %s...", agent.name)
        try:
            # Try common onboard commands
            onboard_cmds = [
                ["python", "-m", agent.name, "--onboard"],
                ["python", "cli.py", "--onboard"],
                ["python", "onboard.py"],
                ["bash", "onboard.sh"],
            ]
            for cmd in onboard_cmds:
                result = subprocess.run(
                    cmd,
                    cwd=str(target),
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if result.returncode == 0:
                    logger.info("Onboarded %s via: %s", agent.name, " ".join(cmd))
                    # Write a marker
                    marker = target / ".superz_onboarded"
                    marker.write_text(datetime_now_iso())
                    return True

            logger.warning("No onboard command succeeded for %s", agent.name)
            # Write marker anyway to avoid retrying every boot
            marker = target / ".superz_onboarded"
            marker.write_text(datetime_now_iso())
            return True  # Don't block the fleet

        except subprocess.TimeoutExpired:
            logger.error("Onboard timeout for %s", agent.name)
            return False
        except Exception as exc:
            logger.error("Onboard error for %s: %s", agent.name, exc)
            return False

    def _check_onboarded(self, agent_dir: Path) -> bool:
        """Check if an agent has been onboarded."""
        markers = [
            agent_dir / ".superz_onboarded",
            agent_dir / ".onboarded",
        ]
        # Also check for common setup artifacts
        artifacts = [
            agent_dir / "node_modules",
            agent_dir / ".venv",
            agent_dir / "venv",
            agent_dir / "__pycache__",
            agent_dir / "requirements.txt",  # if deps installed
        ]
        return any(m.exists() for m in markers) or any(a.exists() for a in artifacts)

    # ------------------------------------------------------------------
    # Launch command building
    # ------------------------------------------------------------------

    def build_launch_command(self, agent: AgentConfig) -> Optional[list[str]]:
        """Build the launch command for an agent.

        Tries several common patterns and returns the first that looks viable.
        """
        agent_dir = self.agents_dir / agent.name
        if not agent_dir.exists():
            logger.error("Agent dir not found: %s", agent_dir)
            return None

        for cmd_template in LAUNCH_COMMANDS:
            cmd = [c.replace("{name}", agent.name) for c in cmd_template]
            main_file = agent_dir / cmd[-2] if len(cmd) >= 2 and cmd[0] == "python" else None
            # For `-m` style, check if there's a __main__.py or __init__.py
            if "-m" in cmd:
                module_name = cmd[cmd.index("-m") + 1]
                module_dir = agent_dir / module_name.replace("-", "_")
                if module_dir.exists() and (module_dir / "__init__.py").exists():
                    return cmd
            # For direct file style, check if file exists
            elif main_file and main_file.exists():
                return cmd

        # Fallback: if the repo has a Makefile with a "serve" target
        if (agent_dir / "Makefile").exists():
            return ["make", "serve"]

        logger.warning("Could not determine launch command for %s", agent.name)
        return None

    # ------------------------------------------------------------------
    # Environment injection
    # ------------------------------------------------------------------

    def build_agent_env(self, agent: AgentConfig) -> dict[str, str]:
        """Build the environment variables for an agent process."""
        env = os.environ.copy()

        # Inject keeper connection info
        env["KEEPER_URL"] = self.config.network.keeper_url
        env["KEEPER_HOST"] = self.config.keeper.host
        env["KEEPER_PORT"] = str(self.config.keeper.port)

        # Inject agent-specific config
        env["AGENT_NAME"] = agent.name
        env["AGENT_PORT"] = str(agent.port)
        env["AGENT_HOST"] = agent.host
        env["AGENT_MODE"] = agent.mode

        # Inject paths
        env["VAULT_PATH"] = self.config.keeper.vault_path
        env["WORKSHOP_PATH"] = self.config.git_agent.workshop_path

        # Python settings
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        # Add agent dir to PYTHONPATH
        agent_dir = self.agents_dir / agent.name
        existing_pp = env.get("PYTHONPATH", "")
        if existing_pp:
            env["PYTHONPATH"] = f"{agent_dir}{os.pathsep}{existing_pp}"
        else:
            env["PYTHONPATH"] = str(agent_dir)

        return env

    # ------------------------------------------------------------------
    # Full launch sequence
    # ------------------------------------------------------------------

    def prepare_agent(self, agent: AgentConfig) -> Optional[dict[str, Any]]:
        """Full preparation: clone, onboard, build command and env.

        Returns a dict with 'cmd', 'cwd', 'env', 'port' or None on failure.
        """
        # Ensure cloned
        if not (self.agents_dir / agent.name).exists():
            if not self.clone_agent(agent):
                logger.error("Failed to clone %s — cannot launch", agent.name)
                return None

        # Onboard
        self.onboard_agent(agent)

        # Build command
        cmd = self.build_launch_command(agent)
        if cmd is None:
            logger.error("No launch command for %s — skipping", agent.name)
            return None

        # Build env
        env = self.build_agent_env(agent)

        agent_dir = self.agents_dir / agent.name

        return {
            "name": agent.name,
            "cmd": cmd,
            "cwd": str(agent_dir),
            "env": env,
            "port": agent.port,
        }

    def prepare_all(
        self, agent_configs: Optional[list[AgentConfig]] = None,
    ) -> list[dict[str, Any]]:
        """Prepare all enabled agents for launch."""
        configs = agent_configs or self.config.get_enabled_agents()
        results: list[dict[str, Any]] = []
        for agent in configs:
            prep = self.prepare_agent(agent)
            if prep is not None:
                results.append(prep)
        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_repo_url(self, agent: AgentConfig) -> str:
        """Build the GitHub clone URL for an agent."""
        if self._github_token:
            return f"https://{self._github_token}@github.com/{FleetConfig.GITHUB_ORG}/{agent.repo}.git"
        return f"https://github.com/{FleetConfig.GITHUB_ORG}/{agent.repo}.git"


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------

def datetime_now_iso() -> str:
    """Return the current datetime in ISO format."""
    from datetime import datetime
    return datetime.now().isoformat()
