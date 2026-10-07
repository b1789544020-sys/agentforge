FROM python:3.11-slim

# AgentForge sandbox template image.
# Used as the launch template for throw-away containers that execute
# untrusted code produced by an LLM tool call.

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Keep the image small.  Add additional packages here when a custom
# tool needs them (e.g. `numpy`, `pandas`, `requests`).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
 && rm -rf /var/lib/apt/lists/*

# Create a non-root user; the agent runs as this user, never root.
RUN useradd --create-home --shell /bin/bash --uid 10001 agent
WORKDIR /workspace
USER agent

# Default entrypoint: read a Python script from stdin and execute it.
# The host side uses `docker run -i` to stream the script in.
ENTRYPOINT ["python", "-c", "import sys, runpy; \
runpy.run_path('/dev/stdin', run_name='__main__')"]