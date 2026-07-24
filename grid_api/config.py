# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from functools import lru_cache

from pydantic_settings import BaseSettings


class GridSettings(BaseSettings):
    # PostgreSQL — reads the same env vars as the Flask app
    postgres_user: str = "postgres"
    postgres_pass: str = "changeme"
    postgres_url: str = "localhost/postgres"  # host/dbname format from .env

    # Redis Streams, pub/sub, quota, and rate limiting
    redis_ip: str = "localhost"
    redis_port: int = 6379
    redis_stream_db: int = 7

    # Grid API server
    grid_api_host: str = "0.0.0.0"
    grid_api_port: int = 7002

    # Timeouts
    job_timeout_seconds: int = 300  # 5 min max generation time
    worker_ping_interval: int = 30  # Keepalive ping every 30s
    stream_subscribe_timeout: int = 300  # SSE connection max lifetime

    # Worker identity. Managed profiles and audio workers always require this;
    # flip the global gate after every worker runtime supports delegation.
    worker_identity_audience: str = "api.aipowergrid.io"
    worker_identity_chain_id: int = 8453
    worker_registration_skew_seconds: int = 300
    require_worker_identity: bool = False
    # Comma-separated SHA-256 profile digests approved after signature and
    # exact-scope hardware qualification. Empty accepts no managed profile.
    approved_worker_profile_digests: str = ""
    # Product gate separate from profile approval and global charging. A
    # private pilot may enable this after its exact-hardware canary; public
    # manager publication remains a separate, broader qualification gate.
    audio_enabled: bool = False
    # Native-manager device enrollment. Independently dark until the console
    # approval page and release manager are deployed together.
    worker_enrollment_enabled: bool = False
    worker_enrollment_console_url: str = (
        "https://console.aipowergrid.io/dashboard/connect-worker"
    )
    worker_enrollment_ttl_seconds: int = 900

    class Config:
        env_file = ".env"
        extra = "ignore"

    @property
    def async_database_url(self) -> str:
        """Construct asyncpg connection URL from the existing env var format."""
        # POSTGRES_URL in .env is "host/dbname" (e.g. "172.22.22.24/postgres")
        host_db = self.postgres_url
        return f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_pass}@{host_db}"

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_ip}:{self.redis_port}/{self.redis_stream_db}"


@lru_cache
def get_settings() -> GridSettings:
    return GridSettings()
