# SPDX-FileCopyrightText: 2025 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from environs import Env

env = Env()
env.read_env()  # read .env file, if it exists


class Config:
    # WIP - Not enabled yet
    horde_title: str = env.str("HORDE_TITLE", default="AI Horde")
    chat_admission_enabled: bool = env.bool("HORDE_CHAT_ADMISSION_ENABLED", default=True)
    """Whether ``POST /v2/generate/chat/async`` accepts new chat requests. Turning it off refuses only new
    requests: status, cancel, pop and submit keep serving chat requests already queued, so a deployment can stop
    admitting chat work and drain what is in flight."""
    chat_request_2sec_limit_per_ip: str | None = env.str("HORDE_CHAT_REQUEST_2SEC_LIMIT_PER_IP", default=None)
    """The per-second limit, in flask-limiter notation such as ``5/second``, on ``POST /v2/generate/chat/async``
    from one ordinary address. Clients behind one egress address, such as the users of one chat front, share it.
    Unset, the route follows the text submit limit."""
    chat_request_90min_limit_per_ip: str | None = env.str("HORDE_CHAT_REQUEST_90MIN_LIMIT_PER_IP", default=None)
    """The per-minute limit, in flask-limiter notation such as ``300/minute``, on ``POST /v2/generate/chat/async``
    from one ordinary address. Unset, the route follows the text submit limit."""
    chat_messages_offload_enabled: bool = env.bool("HORDE_CHAT_MESSAGES_OFFLOAD_ENABLED", default=False)
    """Whether a new chat request whose messages are larger than ``chat_messages_offload_bytes`` has them stored as
    one object, which a worker that declared the ``messages_url`` transport fetches in place of receiving them in the
    pop response. Off by default; turning it off leaves requests already offloaded served as before."""
    chat_messages_offload_bytes: int = env.int("HORDE_CHAT_MESSAGES_OFFLOAD_BYTES", default=256 * 1024)
    """The largest compact JSON size, in bytes and with images inlined as base64 JPEG, of a chat request's messages
    that stays inline in the pop response when offloading is enabled. 256 KiB keeps text-only requests inline, since
    the message text of one is at most the SDK's 1 MiB ``MAX_CHAT_TEXT_BYTES`` and typically far below it, while any
    request carrying a stored image of more than about 192 KiB is offloaded."""
    chat_messages_offload_gzip: bool = env.bool("HORDE_CHAT_MESSAGES_OFFLOAD_GZIP", default=False)
    """Whether an offloaded messages body is stored gzipped, with ``Content-Encoding: gzip``, when that makes it
    smaller. Off by default: Cloudflare may already compress the download at the edge, and R2 may transcode an object
    stored gzipped, so which of the two saves transfer is measured on a deployment before turning it on. Off, the body
    is stored as plain JSON."""
    text_free_output_ceiling: int = env.int("HORDE_TEXT_FREE_OUTPUT_CEILING", default=4096)
    """The most tokens a text or chat request may ask for without holding its kudos upfront, reached when no
    competing job waits for the workers eligible for it. 4096 is what an idle fleet serves free: current instruct and
    reasoning models routinely need more than 512 tokens for one answer."""
    text_free_output_floor: int = env.int("HORDE_TEXT_FREE_OUTPUT_FLOOR", default=512)
    """The fewest tokens a text or chat request may ask for without holding its kudos upfront, however busy its
    eligible workers are and when none is eligible. 512 keeps the chat default ``max_tokens`` free under any load."""
    text_free_output_slope: float = env.float("HORDE_TEXT_FREE_OUTPUT_SLOPE", default=1792.0)
    """The tokens the free allowance loses per competing job waiting for each eligible thread. The default is half
    of the 3584 tokens between the default ceiling and floor, so one competing job per eligible thread halves the
    headroom above the floor and two reach the floor."""
