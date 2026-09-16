# Bothy in one image, for people who would rather pull than build.
#
# There is no build stage. Nothing here compiles, so the image is an interpreter
# and the source tree -- which is the point of the port as much as it was of the
# static binary: what runs is what you can read.
FROM python:3.12-slim

# wget is the healthcheck in docker-compose.yml, and ca-certificates matters
# because the client dials a registry over HTTPS in any real deployment.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY bothy/ /app/bothy/

# No installation step: the package has no dependencies, so `python -m bothy` from
# the source tree is the whole story. Bytecode is not written, because a container
# that is replaced rather than edited has nothing to gain from a cache.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 11223 is Bothy's own port for borrowing, 7777 is the port peers reach, and
# 8080 is the registry. Deliberately not 11434: an engine keeps its own port and
# Bothy sits beside it, so a machine that already runs Ollama loses nothing.
EXPOSE 11223 7777 8080

# No default role on purpose: the same image is the registry, the host, the client
# and the mock engine. `command:` picks the role in compose, or pass it to
# `docker run`: bothy run | share | connect | discovery | mock
ENTRYPOINT ["python", "-m", "bothy"]
CMD ["help"]
