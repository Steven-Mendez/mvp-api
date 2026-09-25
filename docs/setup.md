# Setting up a new project from this template

Everything this repository needs to deploy, in order. Do it once per project (per AWS
account). After that, every push to `main` deploys on its own.

The repository holds no account-specific values: no account ID, bucket name, email
address or origin. Each project provides its own through the variables of a GitHub
environment, so you can use the template without editing any file.

## Before you start

You need:
- An AWS account.
- Local administrator credentials for it (`aws sts get-caller-identity` works).
- Terraform ≥ 1.10.
- The repository on GitHub.

Pick the **region** where the whole stack lives (default `us-east-1`). It must offer
Aurora DSQL, AppSync Events and Cognito. Use the same one in the bootstrap and in the
`AWS_REGION` variable.

The **project name** (default `mvp`) prefixes every resource. If you change it, change it
in two places:
- The bootstrap, with `-var project=...`.
- `infra/envs/prod/prod.tfvars`, with `project = "..."`.

## 1. Bootstrap (once, locally)

This creates what the deploy depends on:
- The S3 bucket for the Terraform state.
- The **ECR repository** for the images.
- GitHub's OIDC provider.
- The role the CD workflow assumes.

```sh
terraform -chdir=infra/bootstrap init
terraform -chdir=infra/bootstrap apply \
  -var github_repository=<owner>/<repo> \
  -var region=us-east-1
```

Its outputs feed the next step:

```sh
terraform -chdir=infra/bootstrap output
```

The bootstrap keeps its own state in `infra/bootstrap/terraform.tfstate`. That file is
git-ignored, so keep it somewhere safe; you only need it to change or destroy the
bootstrap.

## 2. Configure GitHub

Go to **Settings → Environments** and create an environment named **`production`**. The
deploy role only trusts that environment. Add a required reviewer if you want a manual
approval before each deploy.

Then add these **environment variables**. They are variables, not secrets, because none
of them is sensitive:

| Variable              | Value                                                                                                                            |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `AWS_DEPLOY_ROLE_ARN` | The `deploy_role_arn` bootstrap output                                                                                           |
| `ECR_REPOSITORY`      | The `ecr_repository_url` bootstrap output                                                                                        |
| `TF_STATE_BUCKET`     | The `state_bucket` bootstrap output                                                                                              |
| `AWS_REGION`          | The region of the stack, e.g. `us-east-1`                                                                                        |
| `WEB_ORIGINS`         | Origins of the web and mobile (Expo web) apps, comma-separated, e.g. `https://app.example.com`. The first one builds invitation links. |
| `MAIL_FROM`           | The address invitation emails come from                                                                                          |
| `ALERT_EMAIL`         | Where budget alerts and alarms are emailed                                                                                       |

With the GitHub CLI:

```sh
gh api -X PUT repos/<owner>/<repo>/environments/production
gh variable set AWS_DEPLOY_ROLE_ARN --env production --body "$(terraform -chdir=infra/bootstrap output -raw deploy_role_arn)"
gh variable set ECR_REPOSITORY      --env production --body "$(terraform -chdir=infra/bootstrap output -raw ecr_repository_url)"
gh variable set TF_STATE_BUCKET     --env production --body "$(terraform -chdir=infra/bootstrap output -raw state_bucket)"
gh variable set AWS_REGION          --env production --body us-east-1
gh variable set WEB_ORIGINS         --env production --body https://app.example.com
gh variable set MAIL_FROM           --env production --body you@example.com
gh variable set ALERT_EMAIL         --env production --body you@example.com
```

The CD workflow checks all seven before it deploys, and names any that is missing.

Settings that are the same for every account live in `infra/envs/prod/prod.tfvars`. The
defaults are in `infra/variables.tf`:
- `docs_enabled`: publishes `/docs` and `/openapi.json`. The clients generate their API
  client from that spec.
- `monthly_budget_usd`: default `10`. You get an email at 80% of it, and when the month's
  forecast exceeds it.
- `api_memory_mb`
- `api_reserved_concurrency`
- `log_retention_days`
- `deletion_protection`

## 3. First deploy

Push to `main`, or run the **CD** workflow by hand (`.github/workflows/cd.yml`). The
workflow:
1. Runs the checks.
2. Builds the two arm64 images (`api`, `jobs`) and pushes them to ECR.
3. Runs `terraform apply`.
4. Migrates the database.
5. Checks `/health/ready`.

The first apply takes a while, because the CloudFront distributions alone take several
minutes.

## 4. After the first deploy

1. **Confirm the sender address.** SES emails a verification link to `MAIL_FROM`; click
   it. Until you do, invitation emails fail. Invitations are still created, and the API
   returns their link.
2. **Confirm the alerts subscription.** SNS emails a confirmation link to `ALERT_EMAIL`.
   Until you click it, alarms send nothing. Budget alerts need no confirmation.
3. **Leave the SES sandbox when you have real users.** In the sandbox, SES only delivers
   to verified addresses. Request production access in the SES console.
4. **Configure the web and mobile apps** with the stack outputs (see "From your machine"
   below for the local `init`):

   | Output                                           | What the clients use it for                             |
   | ------------------------------------------------ | ------------------------------------------------------- |
   | `api_url`                                        | Base URL of the API                                     |
   | `user_pool_id`, `region`                         | Cognito sign-in                                         |
   | `web_client_id` / `mobile_client_id`             | Cognito app client of each app                          |
   | `appsync_http_domain`, `appsync_realtime_domain` | Realtime (AppSync Events)                               |
   | `media_base_url`                                 | Where product images, avatars and logos are served from |

5. **Update `WEB_ORIGINS`** once the web app is deployed (it comes after the API). Then
   run the CD workflow again. No code change is needed.

## 5. Check it works

- **Health:** `curl <api_url>/health/ready` answers `{"status":"ready"}`.
- **Account and data:** sign up from the web app, create a workspace, create a product,
  upload an image.
- **Realtime:** open the product list in two browsers; a change in one shows up in the
  other.
- **Realtime isolation:** the AppSync `onSubscribe` handler (`infra/realtime.tf`) must
  refuse a subscription to another user's channel `/users/<sub>`.

## From your machine

CD applies every change, but you can read the stack (or plan) locally with the same
values as the GitHub variables:

```sh
export TF_STATE_BUCKET=<state_bucket output> AWS_REGION=us-east-1
make tf-init
terraform -chdir=infra output
```

For `make plan`, also export:
- `TF_VAR_web_origins='["https://app.example.com"]'`
- `TF_VAR_mail_from`
- `TF_VAR_alert_email`
- `TF_VAR_region`
- `API_IMAGE_URI` / `JOBS_IMAGE_URI`: the digests the functions run.

## Checklist

- [ ] Bootstrap applied, and its state file kept safe
- [ ] GitHub environment `production` created
- [ ] Its seven variables set: `AWS_DEPLOY_ROLE_ARN`, `ECR_REPOSITORY`, `TF_STATE_BUCKET`, `AWS_REGION`, `WEB_ORIGINS`, `MAIL_FROM`, `ALERT_EMAIL`
- [ ] First deploy green
- [ ] SES sender address confirmed
- [ ] SNS alerts subscription confirmed
- [ ] Web and mobile apps configured with the outputs
- [ ] `WEB_ORIGINS` updated once the web app is live
