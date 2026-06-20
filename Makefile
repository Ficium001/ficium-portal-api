.PHONY: test test-integration lint typecheck

# Run the full unit test suite (no DB needed)
test:
	pytest tests/test_health.py -v

# Run the integration suite — supply passwords, DSNs are constructed.
# Usage: make test-integration PORTAL_PW=<pw> APP_PW=<pw>
PORTAL_REF := egwobcajdlragubtkpqp
PORTAL_HOST := aws-0-ap-southeast-1.pooler.supabase.com
APP_REF := wixfhjlsjkiwfvqewvmt
APP_HOST := aws-0-ap-south-1.pooler.supabase.com

test-integration:
	@if [ -z "$(PORTAL_PW)" ] || [ -z "$(APP_PW)" ]; then \
		echo "Usage: make test-integration PORTAL_PW=<pw> APP_PW=<pw>"; \
		exit 1; \
	fi
	PORTAL_DB_DSN="postgresql://postgres.$(PORTAL_REF):$(PORTAL_PW)@$(PORTAL_HOST):5432/postgres" \
	APP_DB_DSN="postgresql://postgres.$(APP_REF):$(APP_PW)@$(APP_HOST):5432/postgres" \
	pytest tests/test_integration.py -v

lint:
	ruff check app tests

typecheck:
	mypy app
