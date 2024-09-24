# SPDX-FileCopyrightText: 2024 Tazlin
# SPDX-FileCopyrightText: 2024 ceruleandeep
#
# SPDX-License-Identifier: AGPL-3.0-or-later

# Use a slim base image for Python 3.10
FROM python:3.10-slim AS python


##
## BUILD STAGE
##
FROM python AS python-build-stage

# Install Git
RUN apt-get update && apt-get install -y git

RUN --mount=type=cache,target=/root/.cache pip install --upgrade pip

# Build dependencies
COPY ./requirements.txt .
RUN --mount=type=cache,target=/root/.cache \
  pip wheel --wheel-dir /usr/src/app/wheels \
  -r requirements.txt


##
## RUN STAGE
##
FROM python AS python-run-stage

# git is required in the run stage because one dependency is not available in PyPI
RUN apt-get update && apt-get install -y git

RUN --mount=type=cache,target=/root/.cache pip install --upgrade pip

# Install dependencies
COPY --from=python-build-stage /usr/src/app/wheels /wheels/
COPY ./requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels/ \
  -r requirements.txt \
	&& rm -rf /wheels/

WORKDIR /app

COPY . /app

# Set the environment variables
ENV PROFILE=""

# Set the command to run when the container starts
CMD ["python", "server.py", "-vvvvi", "--horde", "stable"]

# Expose the port
EXPOSE 7001
