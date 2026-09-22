# One image, two entry points: the Memory chat service and the Memory project-knowledge service.
#
# The two deployments differ only in their entry point, their configuration file and their client
# identity, so they share one image and one dependency set. Nothing about a service is baked in
# here: the bind address, the certificate, the announced authorities and every credential arrive as
# arguments or environment variables of the container, because a value in an image is a value
# nobody can change without rebuilding it.
#
# Build with BuildKit. Docker automatically selects the adjacent Dockerfile.dockerignore:
#
#   docker build \
#     --platform linux/amd64 \
#     --file infra/container/Dockerfile \
#     --tag tianshu-memory:local .
#
# Run the chat service (TLS is mandatory off loopback, so both files and the announced authority are
# given explicitly; see docs/deployment-runtime.md):
#
#   docker run --read-only --tmpfs /tmp \
#     --mount type=bind,src=/etc/tianshu,dst=/etc/tianshu,readonly \
#     --mount type=bind,src=/srv/tianshu,dst=/srv/tianshu \
#     --mount type=volume,src=tianshu-logs,dst=/var/log/tianshu \
#     -p 127.0.0.1:8130:8130 \
#     tianshu-memory:local

# The build stage exists only to turn the locked dependency set into a wheel directory plus an
# installed tree. It is discarded, so no compiler, no package index credential and no lock tooling
# reaches the runtime image.
FROM python:3.12.11-slim-bookworm@sha256:c00fc7b44d844b6da22861ec24af43968a5200eac4ec607b4725d585165d6b49 AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

COPY pyproject.toml uv.lock ./
COPY infra/container/build-requirements.txt infra/container/build_install.py ./infra/container/
COPY src ./src

# The same path runs in a fresh local Python 3.12 environment for installation verification.
# It hash-pins build tools separately, checks lock freshness with `uv export --locked`, builds an
# application wheel, then installs locked binary dependencies and that wheel in a pip-free venv.
# `--frozen` would skip freshness checking and must not replace `--locked` here.
RUN python infra/container/build_install.py --source /build --output /opt/tianshu

# The runtime image: the interpreter, the installed environment and the probe script. No shell
# tool for the probe to depend on, no added build tooling, no source checkout and no test suite.
# The upstream Python base may include pip globally; the application venv contains no pip/tools.
FROM python:3.12.11-slim-bookworm@sha256:c00fc7b44d844b6da22861ec24af43968a5200eac4ec607b4725d585165d6b49 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/tianshu/venv/bin:${PATH}" \
    TIANSHU_LOG_DIR=/var/log/tianshu \
    TIANSHU_MEMORY_CONFIG=/etc/tianshu/memory.json

COPY --from=builder /opt/tianshu/venv /opt/tianshu/venv
COPY scripts/container_healthcheck.py /opt/tianshu/container_healthcheck.py

# Both directories are created here and owned by the account that writes them, so an operator can
# mount a volume or a read-only bind over either one without the process needing to create anything.
RUN groupadd --system --gid 10001 tianshu \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent tianshu \
    && install --directory --owner 10001 --group 10001 --mode 0750 /var/log/tianshu /srv/tianshu \
    && chmod 0555 /opt/tianshu/container_healthcheck.py

# A non-root account with no home directory and no shell: the process can write its log and its
# database and nothing else in the image. `--read-only` on the container is what makes the rest of
# the filesystem immutable, which is why every writable path is a mount.
USER 10001:10001

WORKDIR /srv/tianshu

# The port the chat entry point defaults to. The knowledge entry point has no default and is told
# its port on the command line, so an operator who publishes this port is publishing the chat
# service and must say so deliberately for the other one.
EXPOSE 8130

# Liveness only, over verified TLS, bounded in time and in bytes, and with no shell involved. It
# never establishes readiness: readiness needs its own credential and is not a reason to restart a
# container. `TIANSHU_HEALTHCHECK_HOST` must name an authority the server was told to serve, and its
# port must be the one this container listens on.
HEALTHCHECK --interval=15s --timeout=10s --start-period=20s --retries=3 CMD ["python", "/opt/tianshu/container_healthcheck.py"]

# Neither entry point is baked in: the same image runs either service, and the operator picks by
# giving the command. Both demand an explicit configuration, an explicit bind address off loopback,
# an explicit certificate and an explicit authority list.
#
#   chat:      tianshu-memory --config <file>
#                serve [--host <ip>] [--port <port>]
#                [--tls-certfile <pem> --tls-keyfile <pem> --allowed-host <authority>]...
#   knowledge: python -m tianshu_memory.knowledge_cli --config <file>
#                serve [--host <ip>] --port <port> --client <identity>
#                [--tls-certfile <pem> --tls-keyfile <pem> --allowed-host <authority>]...
#
# `--config` belongs to the command rather than to its `serve` subcommand, so it comes before the
# subcommand; the runtime options belong to `serve`, so they come after it.
CMD ["tianshu-memory", "--config", "/etc/tianshu/memory.json", "serve"]
