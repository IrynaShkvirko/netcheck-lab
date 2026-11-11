BACKEND ?= local
# One virtualenv per OS: the Mac and the Linux VM share this folder, and a single .venv
# would be overwritten by whichever side ran uv last (.venv-darwin / .venv-linux).
export UV_PROJECT_ENVIRONMENT ?= .venv-$(shell uname -s | tr A-Z a-z)
NETCHECK = uv run python -m netcheck --backend $(BACKEND)
PYTEST   = uv run pytest --backend $(BACKEND) --junitxml=artifacts/junit.xml
CLAB_VERSION = $(shell grep containerlab_version backends.yaml | cut -d'"' -f2)

.PHONY: install install-clab render lab-up lab-fresh lab-down lab-reset fault-wrong-vlan \
        fault-missing-ospf fault-check record-fixtures test-unit test-lab test-lab-live smoke demo \
        lint typecheck check golden mutate mutation-floor mutate-lab pin-images

install:            ## Python deps (uv)
	uv sync

install-clab:       ## Containerlab at the pinned version
	scripts/install-clab.sh $(CLAB_VERSION)

render:             ## topology + configs + expected state from intent/lab.yaml
	$(NETCHECK) render

lab-up:             ## deploy once (reused if running) and take the baseline snapshot
	$(NETCHECK) deploy

lab-fresh:          ## destroy and redeploy (use if a container restarted and lost its links)
	$(NETCHECK) deploy --fresh

lab-down:
	$(NETCHECK) destroy

lab-reset:          ## restore the baseline snapshot (removes injected faults)
	$(NETCHECK) restore

fault-wrong-vlan:
	$(NETCHECK) fault wrong-vlan

fault-missing-ospf:
	$(NETCHECK) fault missing-ospf-network

fault-check:        ## inject each fault, prove the lab tests it declares catch it, reset
	$(NETCHECK) fault-check

record-fixtures:    ## record the lab's real FRR/iproute2 output as unit-test samples (lab up)
	$(NETCHECK) record-fixtures

test-unit:
	uv run pytest tests/unit

test-lab:           ## full lab suite
	$(PYTEST) tests/lab

test-lab-live:      ## full suite, printing each test step as it runs
	$(PYTEST) tests/lab -v -o log_cli=true --log-cli-level=INFO --log-cli-format='  %(message)s'

smoke:
	$(PYTEST) tests/lab -m smoke

demo: lab-up        ## 3-5 minute interview demo
	$(PYTEST) tests/lab -m smoke
	$(NETCHECK) fault wrong-vlan
	@# must fail: a pass here means the suite cannot see the fault, so the demo stops
	@if $(PYTEST) tests/lab/test_vlan.py -k "isolated and h1-h3"; then \
		$(MAKE) lab-reset; \
		echo "error: the isolation test passed with the wrong-vlan fault applied"; exit 1; \
	fi
	$(MAKE) lab-reset
	$(PYTEST) tests/lab -m resilience

lint:
	uv run ruff check . && uv run ruff format --check .

typecheck:
	uv run mypy

check: lint typecheck test-unit

golden:             ## rewrite tests/unit/golden from the current render; then review git diff
	NETCHECK_UPDATE_GOLDEN=1 uv run pytest tests/unit/test_golden.py -q -rs

mutate:             ## mutation testing over tests/unit, then the floor (slow; see .claude/skills/mutation-testing)
	uv run mutmut run
	uv run mutmut results
	$(MAKE) mutation-floor

mutation-floor:     ## hold the last mutmut results to the floors in pyproject.toml (scores, accepted survivors)
	uv run python scripts/mutation-floor.py check

# Lab mutation profile (mutation/lab): functions of the local backend that send real
# commands and no unit test reaches. Never deploy/destroy, LocalSnapshot or the lifecycle.
LOCAL := netcheck.backends.local
LAB_MUTANTS_LinuxShell   := '$(LOCAL).xǁLinuxShellǁ*'
LAB_MUTANTS_FrrOspf      := '$(LOCAL).xǁFrrOspfǁ*'
LAB_MUTANTS_links-bridge := '$(LOCAL).xǁLinuxLinksǁ*' '$(LOCAL).xǁLinuxBridgeǁ*'
LAB_MUTANTS_backend      := '$(LOCAL).xǁLocalBackendǁis_running*' '$(LOCAL).xǁLocalBackendǁenvironment*' \
                            '$(LOCAL).xǁLocalBackendǁdevices*' '$(LOCAL).xǁLocalBackendǁopen_transport*' \
                            '$(LOCAL).xǁLocalBackendǁcontainer*'
LAB_MUTANTS_all := $(LAB_MUTANTS_LinuxShell) $(LAB_MUTANTS_FrrOspf) $(LAB_MUTANTS_links-bridge) \
                   $(LAB_MUTANTS_backend)
LAB_MUTMUT = cd mutation/lab && NETCHECK_SLA_MODE=record NETCHECK_NO_DEPLOY=1 NETCHECK_RESET_ON_START=1 \
	UV_PROJECT_ENVIRONMENT=$(abspath $(UV_PROJECT_ENVIRONMENT)) uv run --project $(CURDIR) mutmut

mutate-lab:         ## mutation testing against the running lab; GROUP=LinuxShell|FrrOspf|links-bridge|backend|all
	@test -n "$(LAB_MUTANTS_$(GROUP))" || \
		{ echo "GROUP must be one of: LinuxShell FrrOspf links-bridge backend all"; exit 2; }
	$(MAKE) test-lab    # the unmutated lab suite must be green first
	$(LAB_MUTMUT) run --max-children 1 $(LAB_MUTANTS_$(GROUP))
	$(LAB_MUTMUT) results

pin-images:         ## print image digests to paste into backends.yaml
	@for img in $$(grep -E '^\s+(router|switch|host):' backends.yaml | awk '{print $$2}' | sort -u); do \
		docker pull -q $$img >/dev/null && docker inspect --format='{{index .RepoDigests 0}}' $$img; done
