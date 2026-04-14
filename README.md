# SuperZ Runtime

> **Clone this repo, run one command, everything starts.**

The SuperZ Runtime is a self-booting Pelagic fleet engine. It clones, configures, launches, and monitors the entire fleet of agents from a single entry point.

## Quick Start

### Local (Python 3.10+)

```bash
# Clone the runtime
git clone https://github.com/SuperInstance/superz-runtime.git
cd superz-runtime

# Install dependencies
pip install pyyaml

# Boot the fleet (with TUI dashboard)
python -m superz_runtime

# Or headless (daemon mode)
python -m superz_runtime --headless
```

### Docker

```bash
# Clone and build
git clone https://github.com/SuperInstance/superz-runtime.git
cd superz-runtime

# Start the full fleet
docker compose up -d

# View logs
docker compose logs -f

# Stop
docker compose down
```

## Commands

```bash
python -m superz_runtime                  # Boot with TUI
python -m superz_runtime --headless       # Daemon mode
python -m superz_runtime --skip-mud       # Skip MUD server
python -m superz_runtime --agents trail,trust  # Specific agents only
python -m superz_runtime --config my.yaml # Custom config
python -m superz_runtime --doctor         # Diagnose issues
python -m superz_runtime --status         # Fleet health status
python -m superz_runtime --stop           # Stop running fleet
```

Or via Make:

```bash
make boot            # Full fleet with TUI
make boot-headless   # Daemon mode
make stop            # Stop all agents
make status          # Show health
make doctor          # Diagnose issues
make test            # Run tests
```

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                  SUPERZ RUNTIME                       │
│  ┌────────────────────────────────────────────────┐  │
│  │              runtime.py                         │  │
│  │  Phase 1: Environment Check                    │  │
│  │  Phase 2: Fleet Bootstrap (clone/onboard)      │  │
│  │  Phase 3: Infrastructure (Keeper + Git)        │  │
│  │  Phase 4: Launch Agents (parallel)             │  │
│  │  Phase 5: MUD Server (holodeck-studio)         │  │
│  │  Phase 6: Health Monitoring + Git Sync         │  │
│  └──────────┬─────────────────┬───────────────────┘  │
│             │                 │                       │
│  ┌──────────▼──┐   ┌─────────▼──────┐               │
│  │ config.py   │   │ process_       │               │
│  │ fleet.yaml  │   │ manager.py     │               │
│  │ env vars    │   │ start/stop/    │               │
│  │ defaults    │   │ restart/backoff│               │
│  └─────────────┘   └───────┬────────┘               │
│                             │                        │
│  ┌─────────────────┐  ┌────▼──────────┐             │
│  │ health_         │  │ agent_        │             │
│  │ monitor.py      │  │ launcher.py   │             │
│  │ /health polling │  │ clone/onboard │             │
│  │ fleet score     │  │ build cmd     │             │
│  │ alerts          │  │ inject env    │             │
│  └─────────────────┘  └───────────────┘             │
└──────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────┐
│           FLEET AGENTS                  │
│  ┌──────────┐  ┌──────────┐            │
│  │ Keeper   │  │ Git      │  :8443/44  │
│  │ Agent    │  │ Agent    │            │
│  └──────────┘  └──────────┘            │
│  ┌──────────┐  ┌──────────┐            │
│  │ Trail    │  │ Trust    │  :8501/02  │
│  │ Agent    │  │ Agent    │            │
│  └──────────┘  └──────────┘            │
│  ┌──────────┐  ┌──────────┐            │
│  │ Flux VM  │  │Knowledge │  :8503/04  │
│  │ Agent    │  │ Agent    │            │
│  └──────────┘  └──────────┘            │
│  ┌──────────┐  ┌──────────┐            │
│  │Scheduler │  │ Edge     │  :8505/06  │
│  │ Agent    │  │ Relay    │            │
│  └──────────┘  └──────────┘            │
│  ┌──────────┐  ┌──────────┐            │
│  │ Liaison  │  │Cartridge │  :8507/08  │
│  │ Agent    │  │ Agent    │            │
│  └──────────┘  └──────────┘            │
│  ┌──────────────────────────┐          │
│  │ Holodeck MUD Server      │  :7777   │
│  └──────────────────────────┘          │
└─────────────────────────────────────────┘
```

## Configuration

The runtime reads `fleet.yaml` (or `~/.superinstance/fleet.yaml`). A default is auto-generated on first boot.

Key sections:
- **runtime** — headless mode, log level, health check intervals
- **keeper** — host, port, vault path
- **git_agent** — host, port, workshop path
- **agents** — list of fleet agents with ports, modes, branches
- **mud** — MUD server settings
- **network** — topology (star/mesh), discovery
- **secrets** — environment variable names (never raw values)

Environment variables override config:
- `SUPERZ_HEADLESS=true`
- `SUPERZ_LOG_LEVEL=DEBUG`
- `KEEPER_PORT=8443`
- `SUPERZ_SKIP_MUD=true`

## Ports

| Service | Port |
|---|---|
| Keeper Agent | 8443 |
| Git Agent | 8444 |
| Trail Agent | 8501 |
| Trust Agent | 8502 |
| Flux VM Agent | 8503 |
| Knowledge Agent | 8504 |
| Scheduler Agent | 8505 |
| Edge Relay | 8506 |
| Liaison Agent | 8507 |
| Cartridge Agent | 8508 |
| Holodeck MUD | 7777 |

## File Structure

```
superz-runtime/
├── __main__.py          # Package entry point
├── runtime.py           # Main runtime engine (~500 lines)
├── config.py            # Unified fleet config (~250 lines)
├── process_manager.py   # Fleet process manager (~300 lines)
├── health_monitor.py    # Fleet health aggregation (~250 lines)
├── agent_launcher.py    # Agent launch logic (~200 lines)
├── _agent_stub.py       # Lightweight placeholder for missing agents
├── fleet.yaml           # Default fleet configuration
├── pyproject.toml       # Package config
├── Dockerfile           # Docker container
├── docker-compose.yaml  # Full fleet stack
├── Makefile             # Quick commands
├── README.md            # This file
└── tests/
    └── test_runtime.py  # Comprehensive test suite
```

## Instance Directory

All runtime state lives in `~/.superinstance/`:

```
~/.superinstance/
├── fleet.yaml           # Active configuration
├── superz_runtime.pid   # Runtime PID file
├── agents/              # Cloned agent repos
│   ├── trail-agent/
│   ├── trust-agent/
│   └── ...
├── logs/                # Agent stdout/stderr logs
├── vault/               # Keeper vault
├── workshop/            # Git agent workshop
└── worlds/              # MUD world files
```

## Requirements

- **Python 3.10+**
- **git** (for cloning agents)
- **gh CLI** (optional, for GitHub API features)
- **PyYAML** (`pip install pyyaml`)

## License

MIT
