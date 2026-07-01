# 8.kb-pipeline/docker/edgequake.Dockerfile
FROM rust:1-slim AS build
WORKDIR /src
RUN apt-get update && apt-get install -y --no-install-recommends \
      pkg-config libssl-dev libpq-dev curl && rm -rf /var/lib/apt/lists/*
COPY edgequake/edgequake /src
RUN cargo build --release --locked --bin edgequake

FROM debian:bookworm-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl libssl3 libpq5 && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/target/release/edgequake /usr/local/bin/edgequake
# migrations are embedded via sqlx::migrate!() at compile time — no runtime copy needed
# edgequake reads HOST/PORT (not EDGEQUAKE_HOST/EDGEQUAKE_PORT)
ENV HOST=0.0.0.0 PORT=8081 EDGEQUAKE_CHUNKER=passthrough PDFIUM_AUTO_CACHE_DIR=/tmp/eqkbp-pdfium
RUN mkdir -p /tmp/eqkbp-pdfium
EXPOSE 8081
CMD ["edgequake"]
