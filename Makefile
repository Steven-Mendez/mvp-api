# Production runs in AWS; these targets check, build and deploy it, or run it locally
# against Postgres and moto (local/). `make help` lists them.
.DEFAULT_GOAL := help
TF_ENV ?= prod
TF := terraform -chdir=infra

.PHONY: help install fmt lint typecheck arch test test-integration test-e2e test-all \
	mutate mutate-diff mutate-semantic check up run down image tf-init plan apply migrate

help: ## List the targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-16s %s\n", $$1, $$2}'

install: ## Install dependencies and the git hooks
	uv sync --frozen
	uv run pre-commit install

fmt: ## Format Python and Terraform
	uv run ruff format .
	uv run ruff check --fix .
	terraform fmt -recursive infra

lint: ## Lint (ruff, formatting, Terraform formatting)
	uv run ruff check .
	uv run ruff format --check .
	terraform fmt -check -recursive infra

typecheck: ## pyright, strict
	uv run pyright

arch: ## Enforce the Clean Architecture layers
	uv run lint-imports

# The testing triad and mutation testing: docs/testing.md.
test: ## Unit tests: in-memory fakes, no Docker, about a second
	uv run pytest -m "not integration and not e2e"

test-integration: ## Repositories, migrations and locks on a throwaway Postgres (Docker)
	uv run pytest -m integration

test-e2e: ## The HTTP API end to end on a throwaway Postgres (Docker)
	uv run pytest -m e2e

test-all: ## The whole triad with branch coverage (Docker)
	uv run pytest --cov --cov-report=term

mutate: ## Mutation score of the core with mutmut (minimum 95%)
	uv run python mutation/gate.py

mutate-diff: ## Mutation score of the core modules this branch changed
	uv run python mutation/gate.py --changed-since origin/main

mutate-semantic: ## The realistic bugs of mutation/semantic/ the suite must catch (Docker)
	uv run python mutation/semantic.py

check: lint typecheck arch test-all ## Everything CI runs on every pull request

# `uv run --env-file` leaves variables the shell already exports alone, so they are
# dropped first: local/api.env alone decides, and no real AWS credentials come along.
LOCAL_VARS := $(shell sed -n 's/^\([A-Z_]*\)=.*/\1/p' local/api.env) \
	AWS_PROFILE AWS_SESSION_TOKEN AWS_DEFAULT_REGION
LOCAL := env $(addprefix -u ,$(LOCAL_VARS)) uv run --env-file local/api.env
COMPOSE := docker compose -f local/compose.yaml

up: ## Start the local stack (Postgres + moto), create its resources and migrate
	$(COMPOSE) up -d --wait
	$(LOCAL) python local/bootstrap.py
	$(LOCAL) alembic upgrade head

run: ## Serve the API against the local stack on http://localhost:8000 (after `make up`)
	$(LOCAL) uvicorn app.main:app --reload --port 8000

down: ## Stop the local stack; its data goes with it
	$(COMPOSE) down

image: ## Build both Lambda images (linux/arm64): mvp-api:api and mvp-api:jobs
	docker buildx build --platform linux/arm64 --target api --tag mvp-api:api --load .
	docker buildx build --platform linux/arm64 --target jobs --tag mvp-api:jobs --load .

tf-init: ## terraform init against the remote state (needs TF_STATE_BUCKET and AWS_REGION)
	@test -n "$(TF_STATE_BUCKET)" -a -n "$(AWS_REGION)" || { echo "Set TF_STATE_BUCKET and AWS_REGION (docs/setup.md)"; exit 1; }
	$(TF) init -input=false \
		-backend-config="bucket=$(TF_STATE_BUCKET)" \
		-backend-config="key=mvp-api/$(TF_ENV).tfstate" \
		-backend-config="region=$(AWS_REGION)"

TF_IMAGES = -var api_image_uri=$(API_IMAGE_URI) -var jobs_image_uri=$(JOBS_IMAGE_URI)

plan: ## terraform plan (API_IMAGE_URI=... JOBS_IMAGE_URI=..., as <repository>@sha256:...)
	$(TF) plan -var-file=envs/$(TF_ENV)/$(TF_ENV).tfvars $(TF_IMAGES)

apply: ## terraform apply (API_IMAGE_URI=... JOBS_IMAGE_URI=...)
	$(TF) apply -var-file=envs/$(TF_ENV)/$(TF_ENV).tfvars $(TF_IMAGES)

migrate: ## Apply database migrations to the deployed cluster
	env -u ENVIRONMENT -u DATABASE_URL \
		DSQL_ENDPOINT=$$($(TF) output -raw dsql_endpoint) AWS_REGION=$$($(TF) output -raw region) \
		uv run alembic upgrade head
