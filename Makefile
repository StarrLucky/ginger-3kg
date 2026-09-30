.PHONY: lint fmt test check secrets hooks

lint:
	ruff check .
	ruff format --check .

fmt:
	ruff check --fix .
	ruff format .

test:
	pytest

check: lint test

secrets:
	./scripts/check_secrets.sh

hooks:
	git config core.hooksPath scripts/hooks
	@echo "pre-commit хук включён: scripts/hooks/pre-commit"
