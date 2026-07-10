# SPDX-FileCopyrightText: 2024 Tazlin <tazlin.on.github@gmail.com>
# SPDX-FileCopyrightText: 2024 ceruleandeep
#
# SPDX-License-Identifier: AGPL-3.0-or-later

# Use a slim base image for Python 3.12
FROM python:3.12-slim AS python


##
## BUILD STAGE
##
FROM python AS python-build-stage

ARG AI_HORDE_UV_VERSION=0.9.18
ARG AI_HORDE_DEPENDENCY_GROUPS=""

# Install Git
RUN apt-get update && apt-get install -y git

RUN --mount=type=cache,target=/root/.cache pip install --upgrade pip "uv==${AI_HORDE_UV_VERSION}"

# Build dependencies from uv.lock. AI_HORDE_DEPENDENCY_GROUPS is a space-separated
# list of pyproject dependency groups, e.g. "telemetry-profiling".
COPY ./pyproject.toml ./uv.lock ./
RUN --mount=type=cache,target=/root/.cache \
  set -eux; \
  dependency_group_args=""; \
  for dependency_group in ${AI_HORDE_DEPENDENCY_GROUPS}; do \
    dependency_group_args="${dependency_group_args} --group ${dependency_group}"; \
  done; \
  uv export --frozen --no-dev --no-emit-project --no-header --no-hashes ${dependency_group_args} --output-file requirements.docker.txt; \
  pip wheel --wheel-dir /usr/src/app/wheels -r requirements.docker.txt


##
## RUN STAGE
##
FROM python AS python-run-stage

RUN --mount=type=cache,target=/root/.cache pip install --upgrade pip

# Install dependencies from pre-built wheels.
# The git+ URL for patreon-python is replaced with the package name so pip
# uses the wheel that was already built in the build stage (its setup.py has
# a bug that unconditionally requires pytest-runner, which isn't available offline).
COPY --from=python-build-stage /usr/src/app/wheels /wheels/
COPY --from=python-build-stage /requirements.docker.txt .
RUN sed -i -E 's|^patreon @ git\+https://github.com/Patreon/patreon-python.git(@[^ ;]+)?|patreon|' requirements.docker.txt \
  && sed -i -E 's|^git\+https://github.com/Patreon/patreon-python.git(@[^ ;]+)?|patreon|' requirements.docker.txt \
  && pip install --no-cache-dir --no-index --find-links=/wheels/ \
  -r requirements.docker.txt \
	&& rm -rf /wheels/

WORKDIR /app

COPY . /app

# Set the environment variables
ENV PROFILE=""

# Set the command to run when the container starts
CMD ["python", "server.py", "-vvvvi", "--horde", "stable"]

# Expose the port
EXPOSE 7001
