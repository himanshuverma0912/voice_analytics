# Worker image for voice analytics commands.
#
# This image has no listening port and runs no server. It executes one command
# and exits, which is what an Airflow KubernetesPodOperator expects:
#
#   docker run --rm \
#     -e LLM_BASE_URL=... -e LLM_API_KEY=... \
#     -v $(pwd)/data:/data \
#     voice-analytics:0.1.0 \
#     transcribe --input /data/call.wav --output /data/call.json
#
# Override BASE_IMAGE to build against an approved internal base:
#   docker build --build-arg BASE_IMAGE=<internal-registry>/python-312-ubi9 .
#
# The `transfer` command (SFTP to GCS) needs an SSH stack and a cloud SDK,
# which every other command does not. Build with them only when this image
# will run transfers:
#   docker build --build-arg EXTRAS="--extra transfer" .
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

# Private index credentials, supplied at build time. Never baked into a layer.
ARG PYPI_INDEX_URL=https://pypi.org/simple

# Empty by default: a transcribe-or-score image carries neither paramiko nor
# google-cloud-storage. Pass "--extra transfer" to include them.
ARG EXTRAS=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN pip install --no-cache-dir --index-url "${PYPI_INDEX_URL}" uv==0.5.28

# Dependencies first, so a source-only change reuses this cached layer.
COPY pyproject.toml uv.lock README.md ./
RUN UV_DEFAULT_INDEX="${PYPI_INDEX_URL}" uv sync --locked --no-install-project --no-dev ${EXTRAS}

# Then the source, and install the project itself.
COPY src ./src
RUN UV_DEFAULT_INDEX="${PYPI_INDEX_URL}" uv sync --locked --no-dev ${EXTRAS}

# Run unprivileged: a worker never needs root.
RUN useradd --uid 1001 --create-home --shell /usr/sbin/nologin worker \
    && chown -R 1001:0 /app
USER 1001

# Fail fast at build time if the package or its prompt data did not land.
# Fail fast at build time if the package, its prompt data, or any subcommand
# did not land. Catching a missing prompt here beats catching it per pod.
RUN python -c "import voice_analytics; \
from voice_analytics.prompts import default_prompt_manager as pm; \
from voice_analytics.cli.main import build_parser; \
[pm().get(domain='banking', task=t) for t in ('transcription','translation','keyword_extraction')]; \
cmds = set(build_parser()._subparsers._group_actions[0].choices); \
assert cmds >= {'transfer','transcribe','build-prompt','analyse','extract','summarize'}, cmds; \
print('image ok:', voice_analytics.__version__, sorted(cmds))"

# The command is the argument, so `docker run <image> transcribe --input ...`
# works directly. No CMD: running with no arguments prints help and exits 2.
ENTRYPOINT ["python", "-m", "voice_analytics"]
