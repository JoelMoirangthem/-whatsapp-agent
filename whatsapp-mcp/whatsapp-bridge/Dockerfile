# whatsapp-mcp Go bridge — multi-stage build (CGO required for go-sqlite3)
FROM golang:1.27-bookworm AS build

WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download

COPY *.go ./
# mattn/go-sqlite3 requires cgo
RUN CGO_ENABLED=1 go build -trimpath -o /out/whatsapp-bridge .

FROM debian:bookworm-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The bridge resolves its SQLite stores relative to CWD ("store/…"), so the
# working dir doubles as the persistence mount point.
RUN mkdir -p /data \
    && useradd --uid 10000 --system --home-dir /nonexistent bridge \
    && chown -R bridge:bridge /data
WORKDIR /data
VOLUME ["/data"]

COPY --from=build /out/whatsapp-bridge /usr/local/bin/whatsapp-bridge

ENV WHATSAPP_BRIDGE_PORT=8080

EXPOSE 8080

# TCP-liveness probe; deep health (WhatsApp connectivity) needs the bearer
# token, so authenticated checks belong to the orchestrator (see compose).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD ["bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/${WHATSAPP_BRIDGE_PORT:-8080}"]

# Pairing: scan the QR printed to stdout (`docker compose logs -f bridge`)
ENTRYPOINT ["/usr/local/bin/whatsapp-bridge"]
