# Bothy in one image, for people who would rather pull than build. The binary is
# static and has no dependencies, so the runtime stage is as small as it gets.
FROM golang:1.24-alpine AS build

WORKDIR /src
COPY go.mod ./
COPY cmd ./cmd
COPY internal ./internal

# Stamped by the release workflow; harmless when building locally.
ARG VERSION=dev
RUN CGO_ENABLED=0 go build -trimpath \
      -ldflags "-s -w -X main.version=${VERSION}" \
      -o /out/bothy ./cmd/bothy

FROM alpine:3.20
# wget is the healthcheck in docker-compose.yml; ca-certificates matters because
# the client dials a registry over HTTPS in any real deployment.
RUN apk add --no-cache ca-certificates wget
COPY --from=build /out/bothy /usr/local/bin/bothy

# 11434 is Ollama's port: a GPU-less machine runs the client there so existing
# tools find it without being reconfigured.
EXPOSE 11434 7777 8080

# No default command on purpose: the same image is the registry, the host, the
# client and the mock engine. `command:` picks the role in compose, or pass it to
# `docker run`: bothy share | connect | discovery | mock
ENTRYPOINT ["/usr/local/bin/bothy"]
CMD ["help"]
