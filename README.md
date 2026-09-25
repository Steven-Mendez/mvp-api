# mvp-api

The serverless backend of the MVP web and mobile apps: FastAPI in a container on AWS
Lambda. With no traffic it costs cents a month: nothing is billed per hour.

## Architecture

### Code: Clean Architecture, four layers

```
app/
  domain/          Entities and their rules (User, Workspace, Role, Member, Invitation,
                   Product). Plain dataclasses: no framework, no database.
  application/     Use cases (accounts, workspaces, products, purge) and the ports
                   (Protocols) they need: repositories + unit of work, media storage,
                   identity provider, mailer, event publisher.
  infrastructure/  Adapters implementing the ports: Aurora DSQL (SQLAlchemy, mapping the
                   domain dataclasses imperatively), S3 + Pillow, Cognito, SES,
                   AppSync Events, DynamoDB, configuration.
  presentation/    FastAPI: routers, request/response schemas, auth, HTTP errors.
  main.py          Composition root of the HTTP API.
  jobs.py          Composition root of the scheduled jobs: a plain Lambda handler.
```

Dependencies point inward only, and `domain` and `application` import no framework.
`make arch` (import-linter) enforces both rules in CI.

### Runtime: AWS, cents a month at zero traffic

```
web / mobile ──HTTPS──► CloudFront (API) ──► Lambda function URL ──► FastAPI (uvicorn
      │                                        + x-origin-verify       via Lambda Web Adapter)
      │                                                                  │  │  │  │  │
      ├──sign in──────────► Cognito ◄──────── admin create/delete ───────┘  │  │  │  │
      ├──WebSocket────────► AppSync Events ◄─ publish (SigV4) ──────────────┘  │  │  │
      ├──presigned POST──► S3 (uploads/) ◄─── re-encode into media/ ───────────┘  │  │
      └──images──────────► CloudFront (media) ──► S3 (media/)                     │  │
                                                   Aurora DSQL (IAM token) ◄──────┘  │
                                                   DynamoDB (rate limits), SES ◄─────┘
EventBridge Scheduler ──daily──► Lambda "jobs" (plain handler) ──► purge of deleted workspaces
                                        └─ failed after 2 retries ──► SQS failed-jobs ──► alarm ──► email
```

| Piece               | Service                                                                                                                    | Idle cost                               |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------- | --------------------------------------- |
| API                 | Lambda container image (arm64) + [Lambda Web Adapter](https://github.com/aws/aws-lambda-web-adapter) behind a function URL | 0: billed per request, always-free tier |
| HTTPS entry, images | Two CloudFront distributions                                                                                               | 0: always-free tier                     |
| Database            | Aurora DSQL: serverless Postgres, reached over IAM, **no VPC and no NAT**                                                  | 0: free tier covers an MVP              |
| Accounts            | Cognito (Lite)                                                                                                             | 0 up to the free MAU tier               |
| Realtime            | AppSync Events                                                                                                             | 0: billed per operation                 |
| Invitations         | SES                                                                                                                        | 0: billed per email                     |
| Rate limits         | DynamoDB on demand, with TTL                                                                                               | 0: billed per request                   |
| Images              | S3                                                                                                                         | cents per GB stored                     |
| API image           | ECR, last 20 images kept                                                                                                   | cents (free the first year)             |
| Logs                | CloudWatch, 14-day retention                                                                                               | 0 within the free tier                  |
| Monitoring          | AWS Budgets (email at $10), 5 CloudWatch alarms by email (SNS), failed-jobs queue (SQS)                                    | ~$0.80                                  |

Nothing is billed per hour. No NAT gateway, load balancer, provisioned database,
Secrets Manager secret or customer-managed KMS key.

Only CloudFront can call the function URL. CloudFront adds a secret `x-origin-verify`
header to every request, and the app refuses requests without it. A CloudFront function
stamps `x-viewer-ip` on each request, which the per-address rate limit on registration
uses.

## The contract with the clients

The web and mobile apps generate their API client from the OpenAPI spec, served at
`/openapi.json` when `docs_enabled = true`. Keep the operation ids (`<tag>_<function>`)
and schema names stable, because the generated clients are named after them.

Realtime events carry `{resource, action, id, owner_id, at}`, published to each member's
AppSync channel `/users/<cognito sub>`. `tests/test_realtime_contract.py` pins that shape.

## Working on it

```sh
make install    # uv sync + git hooks (ruff, pyright, uv lock, terraform fmt)
make check      # lint, strict types, architecture, tests
make image      # build both Lambda images (linux/arm64): api and jobs
make fmt        # format Python and Terraform
make help       # every target
```

### Running it locally

Needs Docker, and no AWS account. Postgres stands in for Aurora DSQL, and
[moto](https://github.com/getmoto/moto) stands in for Cognito, S3 and DynamoDB:

```sh
make up     # start Postgres + moto (local/compose.yaml), create the pool, bucket and table, migrate
make run    # the API on http://localhost:8000, reloading on changes; docs at /docs
make down   # stop and throw everything away
```

`local/api.env` holds the whole configuration (`ENVIRONMENT=local`). What changes from AWS:
- **Sign in** against moto's Cognito at `http://localhost:5055` with the pool and client
  ids that `make up` prints. A demo account exists: `demo@example.com` / `password`.
- **Invitation emails and realtime events** go to the log, not to SES and AppSync. The
  invitation link is printed there.
- **No origin header:** there is no CloudFront, so `x-origin-verify` is not checked.
- **Images** upload to and are served from moto's S3, under `http://localhost:5055`.
- **Data lives in memory.** moto cannot persist, so Postgres does not either: `make down`
  or a Docker restart starts both from empty, with the same ids.

Local mode refuses to start on Lambda, and `DATABASE_URL` / `AWS_ENDPOINT_URL` are refused
outside it.

Tooling:
- **uv** for dependencies and Python 3.14.
- **ruff** for lint and format.
- **pyright** in strict mode.
- **import-linter** for the layers.
- **pytest** for tests.
- **pre-commit** for the git hooks.

## Deploying

Pushing to `main` runs CI and then deploys to production from GitHub Actions. The deploy
job assumes an IAM role over OIDC, so there are no stored AWS keys. It then:
1. Builds the two arm64 images (Dockerfile targets `api` and `jobs`) and pushes them to
   ECR, tagged with the commit.
2. Runs `terraform apply`, pinning each function to its image digest.
3. Runs `alembic upgrade head` against DSQL.
4. Checks that `/health/ready` answers.

### Setting up a new project

This repository is a template and holds no account-specific values. Each project brings
its own through seven variables of a GitHub environment named `production`:
- `AWS_DEPLOY_ROLE_ARN`
- `ECR_REPOSITORY`
- `TF_STATE_BUCKET`
- `AWS_REGION`
- `WEB_ORIGINS`
- `MAIL_FROM`
- `ALERT_EMAIL`

The first three come from a one-time bootstrap stack. **[docs/setup.md](docs/setup.md)**
walks through all of it, including what to do after the first deploy.

## Database notes (Aurora DSQL)

- **No foreign keys are enforced.** The use cases keep references consistent.
- **Transactions are optimistic.** A transaction that loses a race fails with SQLSTATE
  40001, and `UnitOfWork.transaction()` reruns it. Keep only database work inside it;
  storage, email and realtime go after the commit.
- **Migrations run in autocommit.** DSQL allows one DDL per transaction, and allows no
  DDL next to DML. Write each migration as a list of single statements, and build indexes
  with `CREATE INDEX ASYNC`.
- **A transaction modifies at most 3,000 rows.** Purges work in batches.
- **Migrations must be backward compatible.** They run after the new code is live (first
  expand, later contract).
