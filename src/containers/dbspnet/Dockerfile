# DbspNet engine builder — clones the dbsp-net repo (a .NET DBSP implementation) at a
# pinned commit and builds the DbspNet control service (the pipeline-manager analogue).
# Follows the same from-source pattern as the other built engines (e.g. duckdb-openivm):
# ivm-bench fetches the engine source itself, no sibling checkout needed. Bump the engine
# by overriding DBSPNET_COMMIT (and DBSPNET_REPO for a fork).
FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
# ReadyToRun needs a runtime identifier: BuildKit sets TARGETARCH; the classic builder leaves it
# empty, so fall back to the build stage's own architecture.
ARG TARGETARCH
ARG DBSPNET_REPO=https://github.com/clast-project/dbsp-net.git
ARG DBSPNET_COMMIT=86d0ce1

# git + certs for the clone.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone "${DBSPNET_REPO}" . \
 && git checkout "${DBSPNET_COMMIT}"
# ReadyToRun: a fresh process (a restart included) starts from precompiled code, not tier-0 JIT
# (dbsp-net docs/design-durable-state.md, issue #8).
RUN arch="${TARGETARCH:-$(dpkg --print-architecture)}" \
 && case "$arch" in \
      arm64) rid=linux-arm64 ;; \
      amd64) rid=linux-x64 ;; \
      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac \
 && dotnet publish src/DbspNet.Server/DbspNet.Server.csproj -c Release -o /app \
      -r "$rid" --self-contained false -p:PublishReadyToRun=true

FROM mcr.microsoft.com/dotnet/aspnet:10.0
# curl for the compose healthcheck — the aspnet runtime image is Debian-slim and
# does NOT ship curl, so without this the /healthz probe fails with "curl: not
# found" and the container never reports healthy (even when the service is up),
# which cascades into the dbt-server dependency failing to start.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app .
ENV ASPNETCORE_URLS=http://+:8080
# Tiering counts calls from the first one, not after a 100 ms quiet period: batch-1 drain -25%
# at SF=3, W=12, Nexmark neutral (dbsp-net issue #8).
ENV DOTNET_TC_CallCountingDelayMs=0
EXPOSE 8080
ENTRYPOINT ["dotnet", "DbspNet.Server.dll"]
