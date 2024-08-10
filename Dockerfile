# SPDX-FileCopyrightText: 2024 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

# Use a slim base image for Python 3.10
FROM python:3.10-slim

# Install Git
RUN apt-get update && apt-get install -y git

# Set the working directory
WORKDIR /app

# Copy the source code to the container
COPY . /app

# Install the dependencies
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir --prefer-binary -r requirements.txt

# Set the environment variables
ENV PROFILE=

# Set the command to run when the container starts
CMD ["python", "server.py", "-vvvvi", "--horde", "stable"]

# Expose the port
EXPOSE 7001
