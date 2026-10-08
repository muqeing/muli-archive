FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The build context is the repository root. Copy only the package metadata and
# source tree so local tests, state databases, and generated outputs never enter
# the image.
COPY pyproject.toml /app/pyproject.toml
COPY src /app/src

RUN pip install --no-cache-dir .

RUN groupadd --gid 1001 sorter \
    && useradd --uid 1000 --gid 1001 --create-home --home-dir /home/sorter --shell /usr/sbin/nologin sorter

USER 1000:1001

ENTRYPOINT ["python", "-m", "muli_sorter.queue_service"]
