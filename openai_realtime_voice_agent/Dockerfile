# Home Assistant add-on image.
#
# Uses the Debian (glibc) base image instead of Alpine (musl). Heavy Python
# dependencies pulled in transitively by pipecat (llvmlite, numba, ...) ship
# prebuilt manylinux wheels for glibc, so they install without compiling
# against a specific LLVM. The previous Alpine build compiled llvmlite from
# source and broke once it resolved to llvmlite 0.47 (which requires LLVM 20)
# while the image only provided LLVM 15.
#
# Override the base per-arch via build.yaml (build_from) / --build-arg BUILD_FROM.
ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-debian:bookworm@sha256:68f4c1a11d10facc5442ff11509a164463f98b154c80dc1dfeedbc267dd71665
FROM ${BUILD_FROM}

# uv installs the locked dependencies into /opt/venv, using Debian's python3.
ENV PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON=/usr/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    PATH=/opt/venv/bin:$PATH

# Runtime deps + minimal build insurance. Most Python deps install from wheels;
# build-essential/python3-dev only cover the rare source-only package.
# ffmpeg + libsndfile1 cover audio decoding/encoding paths in pipecat.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-dev \
        build-essential \
        ffmpeg \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# Install exactly the dependency versions (and hashes) in uv.lock; --frozen
# fails the build rather than re-resolving if the lock is out of date.
COPY --from=ghcr.io/astral-sh/uv:0.12.15@sha256:62f8c047d0a0e9ece6b53fc63df902585a67a47a7f318ddec4a37db586edc8e3 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock /tmp/app_build/
RUN cd /tmp/app_build \
    && uv sync --frozen --no-dev \
    && rm -rf /tmp/app_build

# App code (kept at /app for `python3 -m app.main` and easy inspection)
COPY app/ /app/

# s6-overlay / bashio run script
COPY root/ /
RUN chmod a+x /run.sh

WORKDIR /
CMD ["/run.sh"]
