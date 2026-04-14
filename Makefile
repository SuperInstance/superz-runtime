.PHONY: boot boot-headless stop status doctor clean test help install

# Default target
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies
	pip install -e ".[dev]"

boot: ## Boot the full fleet with TUI
	python -m superz_runtime

boot-headless: ## Boot without TUI (daemon mode)
	python -m superz_runtime --headless

boot-skip-mud: ## Boot without MUD server
	python -m superz_runtime --skip-mud

boot-agents: ## Boot specific agents (usage: make boot-agents AGENTS=trail,trust)
	python -m superz_runtime --agents $(AGENTS)

stop: ## Stop all running agents
	python -m superz_runtime --stop

status: ## Show fleet health status
	python -m superz_runtime --status

doctor: ## Diagnose runtime issues
	python -m superz_runtime --doctor

clean: ## Clean all runtime state (logs, PID files, etc.)
	rm -rf ~/.superinstance/logs/*.log*
	rm -f ~/.superinstance/superz_runtime.pid
	@echo "Cleaned logs and PID files"

clean-all: ## Remove entire ~/.superinstance/ directory
	rm -rf ~/.superinstance
	@echo "Removed ~/.superinstance/"

test: ## Run all tests
	python -m pytest tests/ -v

test-coverage: ## Run tests with coverage
	python -m pytest tests/ -v --cov=. --cov-report=term-missing

lint: ## Run linter
	python -m ruff check .

docker-build: ## Build Docker image
	docker compose build

docker-up: ## Start fleet with Docker Compose
	docker compose up -d

docker-down: ## Stop Docker Compose stack
	docker compose down

docker-logs: ## Follow Docker Compose logs
	docker compose logs -f
