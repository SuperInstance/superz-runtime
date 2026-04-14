.PHONY: boot boot-headless stop status doctor clean test

# ── Run the runtime with TUI ────────────────────────────────────────
boot:
	python runtime.py

# ── Run in headless/daemon mode ─────────────────────────────────────
boot-headless:
	python runtime.py --headless

# ── Boot with specific agents only ──────────────────────────────────
boot-agents:
	python runtime.py --agents trail-agent,trust-agent

# ── Stop all running agents (kill superz processes) ─────────────────
stop:
	pkill -f "superz_runtime" 2>/dev/null || true
	@echo "Fleet stopped."

# ── Show fleet status (check which ports are listening) ─────────────
status:
	@echo "=== SuperZ Fleet Status ==="
	@for port in 8443 8444 8501 8502 8503 8504 8505 8506 8507 8508 8509 8510 8511 8512; do \
		if command -v lsof > /dev/null 2>&1; then \
			if lsof -i :$$port -sTCP:LISTEN > /dev/null 2>&1; then \
				echo "  ✓ Port $$port — LISTENING"; \
			else \
				echo "  ✗ Port $$port — closed"; \
			fi; \
		elif command -v ss > /dev/null 2>&1; then \
			if ss -tlnp | grep -q ":$$port "; then \
				echo "  ✓ Port $$port — LISTENING"; \
			else \
				echo "  ✗ Port $$port — closed"; \
			fi; \
		else \
			echo "  ? Port $$port — cannot check (no lsof/ss)"; \
		fi; \
	done

# ── Environment doctor ──────────────────────────────────────────────
doctor:
	@echo "=== SuperZ Doctor ==="
	@echo "Python: $$(python3 --version 2>&1)"
	@echo "Git:    $$(git --version 2>&1)"
	@echo "PyYAML: $$(python3 -c 'import yaml; print(yaml.__version__)' 2>&1)"
	@echo "Instance dir: $${HOME}/.superinstance"
	@test -d "$${HOME}/.superinstance" && echo "  ✓ exists" || echo "  ✗ missing"
	@test -f fleet.yaml && echo "Config:  fleet.yaml ✓" || echo "Config:  fleet.yaml ✗ (will use defaults)"

# ── Clean instance data ─────────────────────────────────────────────
clean:
	rm -rf ~/.superinstance
	@echo "Instance data cleaned."

# ── Run tests ────────────────────────────────────────────────────────
test:
	python3 -m pytest tests/ -v
