# SuperZ Runtime

**Self-booting Pelagic fleet runtime.** Clone, configure, and launch the entire agent fleet with a single command.

## Quick Start

```bash
# 1. Clone the runtime
git clone <repo-url> superz-runtime && cd superz-runtime

# 2. Boot the fleet (interactive TUI)
python runtime.py

# Or headless mode
python runtime.py --headless

# Only specific agents
python runtime.py --agents trail-agent,trust-agent
```

## Docker

```bash
# Build and run
docker compose up --build

# Headless mode (recommended for containers)
docker compose up -d
```

## Architecture

```
┌──────────────────────────────────────────────────┐
│                  SuperZ Runtime                   │
│              (runtime.py / TUI)                   │
├──────────┬──────────┬──────────┬─────────────────┤
│  Keeper  │ Git Agent│  Fleet   │  MUD (optional) │
│  :8443   │  :8444   │ :8501-12 │  :7777          │
├──────────┴──────────┴──────────┴─────────────────┤
│            Process Manager                       │
│     (start / stop / health / restart)            │
├──────────────────────────────────────────────────┤
│           Agent Launcher                         │
│     (clone / onboard / build command)            │
├──────────────────────────────────────────────────┤
│           Fleet Config (fleet.yaml)              │
└──────────────────────────────────────────────────┘
```

### Boot Phases

1. **Environment Check** — Python 3.10+, git, create `~/.superinstance/`
2. **Load Config** — parse `fleet.yaml` or generate defaults
3. **Start Keeper** — port 8443
4. **Start Git Agent** — port 8444
5. **Launch Fleet** — clone & start each enabled agent
6. **Start MUD** — optional holodeck server on port 7777
7. **Health Loop** — poll every 30s, auto-restart crashed agents
8. **Shutdown** — SIGTERM/SIGINT → stop all in reverse order

## Fleet Agents

| Agent       | Port | Description          |
|-------------|------|----------------------|
| trail-agent | 8501 | Path & trail tracking |
| trust-agent | 8502 | Trust scoring        |
| compass-agent| 8503 | Direction & routing  |
| echo-agent  | 8504 | Event echo           |
| atlas-agent | 8505 | Mapping & geography  |
| beacon-agent| 8506 | Signal broadcasting   |
| scope-agent | 8507 | Observation & monitoring |
| forge-agent | 8508 | Build & compilation   |
| vault-agent | 8509 | Secret management     |
| tide-agent  | 8510 | Temporal scheduling   |
| helm-agent  | 8511 | Orchestration         |
| crest-agent | 8512 | Wave analysis         |

## Configuration

Edit `fleet.yaml` to customize ports, enable/disable agents, and tune health check intervals. The runtime generates sensible defaults if no config file is found.

## Make Targets

```bash
make boot            # Start with TUI
make boot-headless   # Start in daemon mode
make stop            # Kill all fleet processes
make status          # Check which ports are listening
make doctor          # Verify environment
make clean           # Remove ~/.superinstance
make test            # Run test suite
```

## Requirements

- Python 3.10+
- git
- PyYAML (`pip install pyyaml`)

## License

MIT
