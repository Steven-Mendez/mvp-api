# syntax=docker/dockerfile:1
#
# Two Lambda images (arm64) from one set of dependencies:
#
#   api   FastAPI behind Lambda Web Adapter: the adapter is a Lambda extension that starts
#         with the container, waits for the server to answer on
#         AWS_LWA_READINESS_CHECK_PATH and turns every invocation into an HTTP request.
#   jobs  The scheduled jobs, a plain Lambda handler (`app.jobs.handler`) on AWS's Python
#         base image, which brings the Lambda runtime client.
#
# Build: docker buildx build --platform linux/arm64 --target <api|jobs> .
# The deps stage runs on the build machine and asks uv for wheels of the target
# architecture, so a cross-build needs no emulation.

ARG PYTHON_VERSION=3.14

# uv runs in the deps stage, on the build machine: it must be the build platform's binary
# (a bare `COPY --from=<image>` would fetch the target platform's).
FROM --platform=$BUILDPLATFORM ghcr.io/astral-sh/uv:0.12.19 AS uv

FROM --platform=$BUILDPLATFORM public.ecr.aws/docker/library/python:${PYTHON_VERSION}-slim AS deps
ARG PYTHON_VERSION
ARG TARGETARCH
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project --no-hashes --quiet -o requirements.txt \
    && uv pip install --quiet --requirements requirements.txt --target /deps \
        --python-platform "$([ "$TARGETARCH" = arm64 ] && echo aarch64 || echo x86_64)-manylinux_2_28" \
        --python-version "${PYTHON_VERSION}" --only-binary :all:

FROM public.ecr.aws/lambda/python:${PYTHON_VERSION} AS jobs
# The task root comes first on the handler's import path, ahead of the runtime's own
# boto3: the locked versions win.
COPY --from=deps /deps ${LAMBDA_TASK_ROOT}/
COPY app ${LAMBDA_TASK_ROOT}/app
CMD ["app.jobs.handler"]

FROM public.ecr.aws/docker/library/python:${PYTHON_VERSION}-slim AS api
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:1.1.0 /lambda-adapter /opt/extensions/lambda-adapter
ENV PORT=8000 \
    AWS_LWA_READINESS_CHECK_PATH=/health \
    PYTHONPATH=/var/task \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /var/task
COPY --from=deps /deps ./
COPY app ./app
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-server-header"]
