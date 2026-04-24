.PHONY: quickstart quickstart-status test-quickstart

quickstart:
	bash scripts/quickstart.sh

quickstart-status:
	bash scripts/deploy_runtime.sh status

test-quickstart:
	bash -n scripts/quickstart.sh
	.venv/bin/python -m py_compile src/quickstart_smoke.py src/main.py
	.venv/bin/pytest tests/test_quickstart_smoke.py -q
