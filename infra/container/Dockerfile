# One image, two entry points: the Memory chat service and the Memory project-knowledge service.
#
# The two deployments differ only in their entry point, their configuration file and their client
# identity, so they share one image and one dependency set. Nothing about a service is baked in
# here: the bind address, the certificate, the announced authorities and every credential arrive as
# arguments or environment variables of the container, because a value in an image is a value
# nobody can change without rebuilding it.
#
# Build (the ignore file is passed explicitly because this checkout keeps it under `infra/` rather
# than beside the build context, and a stray `.dockerignore` would silently change what is sent):
#
#   docker build \
#     --file infra/container/Dockerfile \
#     --ignorefile infra/container/Dockerfile.dockerignore \
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
FROM python:3.12.11-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# One pinned build tool. `uv` reads the repository's own `uv.lock`; nothing here re-resolves it.
RUN python -m pip install --no-cache-dir "uv==0.8.22"

WORKDIR /build

# Only the files the resolution needs, so a source edit does not invalidate the dependency layer.
COPY pyproject.toml uv.lock ./

# `--frozen` fails the build if `uv.lock` and `pyproject.toml` disagree, rather than resolving
# something new, and `--require-hashes` makes the wheel step verify every artifact against the
# hashes recorded in the lock file.
RUN uv export --frozen --no-dev --no-emit-project --output-file requirements.txt \
    && python -m venv /opt/tianshu/venv \
    && /opt/tianshu/venv/bin/python -m pip install --require-hashes -r requirements.txt

COPY src ./src

# The application itself, with no dependencies re-resolved: the interpreter above already has them.
RUN /opt/tianshu/venv/bin/python -m pip install --no-deps --no-build-isolation .

# The runtime image: the interpreter, the installed environment and the probe script. No shell
# tool for the probe to depend on, no build tooling, no source checkout and no test suite.
FROM python:3.12.11-slim-bookworm AS runtime

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
#                [--host <ip>] [--port <port>] serve
#                [--tls-certfile <pem> --tls-keyfile <pem> --allowed-host <authority>]...
#   knowledge: python -m tianshu_memory.knowledge_cli --config <file>
#                [--host <ip>] --port <port> --client <identity> serve
#                [--tls-certfile <pem> --tls-keyfile <pem> --allowed-host <authority>]...
#
# `--config` belongs to the command rather than to its `serve` subcommand, so it comes before the
# subcommand; the runtime options belong to `serve`, so they come after it.
CMD ["tianshu-memory", "--config", "/etc/tianshu/memory.json", "serve"]
