# OSS Radar — convenience entry points. See scripts/demo_local.sh for details.
.PHONY: demo dashboard test help
.DEFAULT_GOAL := help

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

demo: ## Fresh clone -> local pipeline run -> view instructions (no cloud needed)
	@bash scripts/demo_local.sh $(ARGS)

dashboard: ## Serve the dashboard against the local DuckDB warehouse
	@.venv/bin/uvicorn dashboard.app.main:app --port 8099

test: ## Run pipeline + dashboard test suites
	@.venv/bin/python -m pytest pipeline/tests dashboard/tests -q
