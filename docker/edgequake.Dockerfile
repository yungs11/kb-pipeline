# 8.kb-pipeline/docker/edgequake.Dockerfile
# Build + runtime pinned to the SAME Debian release (trixie) so the binary's
# glibc requirement (2.38/2.39 from trixie) is satisfied at runtime. Using a
# bookworm runtime here fails with `GLIBC_2.39 not found`.
# NOTE: requires BuildKit (Docker default). The per-Dockerfile ignore
# docker/edgequake.Dockerfile.dockerignore overrides the root .dockerignore
# (which excludes edgequake/) only under BuildKit; DOCKER_BUILDKIT=0 → COPY fails.
FROM rust:1-slim-trixie AS build
WORKDIR /src
RUN apt-get update && apt-get install -y --no-install-recommends \
      pkg-config libssl-dev libpq-dev curl && rm -rf /var/lib/apt/lists/*
COPY edgequake/edgequake /src
RUN cargo build --release --locked --bin edgequake

FROM debian:trixie-slim
WORKDIR /app
# trixie renamed the OpenSSL runtime lib to libssl3t64 (time_t 64-bit transition).
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl libssl3t64 libpq5 && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/target/release/edgequake /usr/local/bin/edgequake
# migrations are embedded via sqlx::migrate!() at compile time — no runtime copy needed
# edgequake reads HOST/PORT (not EDGEQUAKE_HOST/EDGEQUAKE_PORT)
ENV HOST=0.0.0.0 PORT=8081 EDGEQUAKE_CHUNKER=passthrough PDFIUM_AUTO_CACHE_DIR=/tmp/eqkbp-pdfium
RUN mkdir -p /tmp/eqkbp-pdfium
EXPOSE 8081
CMD ["edgequake"]
