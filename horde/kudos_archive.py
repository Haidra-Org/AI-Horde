# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Write, verify, evict and restore kudos retention archive objects.

The archive is one S3-compatible bucket with credentials of its own. Each retention unit is one compressed CSV object
written once, with a JSON manifest beside it (``<object key>.manifest.json``). Keys start with the tier, so each tier
is listed and sized on its own. Nothing is evicted automatically. ``KudosArchive.enforce_budget`` deletes statistic
dumps only when an operator runs it, and currency objects are never deleted.

- ``KudosArchive`` is the client; ``KudosArchive.from_environment`` builds it from ``KUDOS_ARCHIVE_*`` variables.
- ``ArchiveManifest`` is the manifest stored beside each object; ``VerificationReport`` is a full read-back of one.
- ``ArchiveColumn`` is one archived column's name and PostgreSQL type, which the manifest records at export so a
  restore can build its table without the live schema.
- ``ArchiveObjectAudit`` is the result of ``KudosArchive.audit_object``, a read-back that reports failures instead of
  raising them.
- ``ArchiveCodec`` is the streaming compression interface. ``ZstdCodec`` compresses new objects, and ``GzipCodec``
  stays readable. A reader picks the codec from the object key's extension, and the manifest records it.
- ``KudosArchiveError`` wraps every object-store failure. Callers decide whether a failure blocks a step.
"""

from __future__ import annotations

import codecs
import csv
import enum
import hashlib
import io
import json
import os
import re
import zlib
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Protocol

import boto3
import zstandard
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from loguru import logger
from psycopg2 import sql as psycopg2_sql
from psycopg2.extensions import connection as PsycopgConnection

from horde.enums import KudosRetentionDataset

ARCHIVE_ACCOUNT_ENV: Final[str] = "KUDOS_ARCHIVE_ACCOUNT"
"""The environment variable holding the archive store's S3 endpoint URL."""
ARCHIVE_BUCKET_ENV: Final[str] = "KUDOS_ARCHIVE_BUCKET"
"""The environment variable holding the archive bucket name."""
ARCHIVE_ACCESS_KEY_ID_ENV: Final[str] = "KUDOS_ARCHIVE_AWS_ACCESS_KEY_ID"
"""The environment variable holding the access key id of the token scoped to the archive bucket."""
ARCHIVE_SECRET_ACCESS_KEY_ENV: Final[str] = "KUDOS_ARCHIVE_AWS_SECRET_ACCESS_KEY"
"""The environment variable holding the secret of the token scoped to the archive bucket."""
ARCHIVE_PREFIX_ENV: Final[str] = "KUDOS_ARCHIVE_PREFIX"
"""The environment variable holding the key prefix of this deployment's objects; empty by default.

Deployments that share one bucket (development and production, or a rehearsal on a restored copy) each set their own
prefix, so their listings, budgets and evictions never reach each other's objects.
"""
ARCHIVE_REGION_ENV: Final[str] = "KUDOS_ARCHIVE_REGION"
"""The environment variable holding the signing region; R2 signs with ``auto``, the default."""
ARCHIVE_REQUIRED_ENV: Final[tuple[str, ...]] = (
    ARCHIVE_ACCOUNT_ENV,
    ARCHIVE_BUCKET_ENV,
    ARCHIVE_ACCESS_KEY_ID_ENV,
    ARCHIVE_SECRET_ACCESS_KEY_ENV,
)
"""The variables that must all be set for the archive to be configured."""
DEFAULT_ARCHIVE_REGION: Final[str] = "auto"
"""The signing region when ``KUDOS_ARCHIVE_REGION`` is unset."""

ARCHIVE_MANIFEST_SUFFIX: Final[str] = ".manifest.json"
"""The suffix appended to an object key to form the key of its manifest."""
ARCHIVE_MANIFEST_SCHEMA_VERSION: Final[int] = 2
"""The manifest layout version written by this module."""
ARCHIVE_MANIFEST_UNTYPED_SCHEMA_VERSION: Final[int] = 1
"""The manifest layout version that does not record column types. It stays readable for verification and audit, and restore
refuses it."""
ARCHIVE_MANIFEST_READABLE_SCHEMA_VERSIONS: Final[frozenset[int]] = frozenset(
    {ARCHIVE_MANIFEST_UNTYPED_SCHEMA_VERSION, ARCHIVE_MANIFEST_SCHEMA_VERSION},
)
"""The manifest layout versions this module parses."""
EXPORT_DESCRIBE_VIEW: Final[str] = "kudos_archive_export_columns"
"""The temporary view an export defines over its query to read the result column types. It is dropped before the copy."""
ARCHIVE_PART_BYTES: Final[int] = 8 * 1024 * 1024
"""The compressed bytes buffered before one multipart part is uploaded; S3 requires at least 5 MiB per non-final part."""
ARCHIVE_READ_CHUNK_BYTES: Final[int] = 1024 * 1024
"""The bytes read from the store per chunk while verifying or restoring an object."""
ARCHIVE_DELETE_BATCH_SIZE: Final[int] = 1000
"""The most keys one ``DeleteObjects`` request takes, the S3 limit."""
ARCHIVE_CONNECT_TIMEOUT_SECONDS: Final[int] = 5
"""How long an archive request waits to connect; the retention job retries on its next tick."""
ARCHIVE_READ_TIMEOUT_SECONDS: Final[int] = 60
"""How long an archive request waits for a response; one part upload carries ``ARCHIVE_PART_BYTES``."""
ARCHIVE_MAX_ATTEMPTS: Final[int] = 3
"""The attempts per archive request, including the first."""
ZSTD_COMPRESSION_LEVEL: Final[int] = 12
"""The zstd level for new objects.

Level 19 is single-threaded and several times slower than 12 for a few percent smaller objects, and one export of a
legacy sub-range already streams a week of rows.
"""
SOURCE_RANGE_MINIMUM: Final[str] = "min"
"""The lower bound written in a source range for a unit that starts at the partition's minimum."""
CSV_TRUE: Final[str] = "t"
"""How PostgreSQL ``COPY ... CSV`` writes a true boolean."""


_ARCHIVE_PREFIX_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._/-]*$")
_MISSING_OBJECT_CODES: Final[frozenset[str]] = frozenset({"404", "NoSuchKey", "NotFound"})
"""The error codes S3-compatible stores report for a missing object. A HEAD response has no body, so only the status."""
_UNSUPPORTED_OPERATION_CODES: Final[frozenset[str]] = frozenset({"501", "NotImplemented", "MethodNotAllowed", "405"})
"""The error codes S3-compatible stores report for an operation they do not implement."""


def normalize_archive_prefix(raw: str) -> str:
    """Parse a key prefix into its stored form: no leading slash and exactly one trailing slash, or empty.

    Raises:
        ValueError: The prefix holds a character outside letters, digits, ``-``, ``_``, ``/`` and ``.``, or an empty,
            ``.`` or ``..`` path segment.
    """
    if not _ARCHIVE_PREFIX_PATTERN.match(raw):
        raise ValueError(f"{ARCHIVE_PREFIX_ENV} may hold only letters, digits, '-', '_', '/' and '.', got {raw!r}")
    stripped = raw.strip("/")
    if not stripped:
        return ""
    if any(segment in ("", ".", "..") for segment in stripped.split("/")):
        raise ValueError(f"{ARCHIVE_PREFIX_ENV} must not hold empty, '.' or '..' path segments, got {raw!r}")
    return f"{stripped}/"


class KudosArchiveError(Exception):
    """An archive object-store request failed, or an object could not be read as an archive object."""


class ArchiveBudgetExceeded(KudosArchiveError):
    """The measured compressed upload exceeds the remaining tier allowance, so the multipart upload is aborted."""


class ArchiveEvictionError(KudosArchiveError):
    """An eviction stopped partway. ``deleted_keys`` holds the object keys whose deletes the store confirmed."""

    def __init__(self, message: str, *, deleted_keys: list[str]) -> None:
        """Create the error.

        Args:
            message: What failed.
            deleted_keys: The object keys deleted before the failure, oldest first.
        """
        super().__init__(message)
        self.deleted_keys: list[str] = list(deleted_keys)
        """The object keys deleted before the failure, oldest first."""


class ArchiveTier(enum.StrEnum):
    """The budget class of an archive object; only statistic objects can be evicted."""

    CURRENCY = "currency"
    STATISTICS = "statistics"


class StreamCompressor(Protocol):
    """A stateful compressor fed in chunks."""

    def compress(self, chunk: bytes) -> bytes:
        """Return the compressed output available after consuming ``chunk``."""
        ...

    def flush(self) -> bytes:
        """Return the remaining compressed output and end the stream."""
        ...


class StreamDecompressor(Protocol):
    """A stateful decompressor fed in chunks."""

    def decompress(self, chunk: bytes) -> bytes:
        """Return the decompressed output available after consuming ``chunk``."""
        ...

    @property
    def eof(self) -> bool:
        """Return whether the end of the compressed stream has been reached."""
        ...


class ArchiveCodec(Protocol):
    """A streaming compression format for archive objects.

    Subclass Integration:
        ``name`` is recorded in the manifest and ``extension`` ends the object key, which is how a reader chooses the
        codec. Both must stay stable once objects exist.
    """

    @property
    def name(self) -> str:
        """Return the codec name recorded in manifests."""
        ...

    @property
    def extension(self) -> str:
        """Return the object key extension, without the dot."""
        ...

    def compressor(self) -> StreamCompressor:
        """Create a compressor for one object."""
        ...

    def decompressor(self) -> StreamDecompressor:
        """Create a decompressor for one object."""
        ...


class GzipCodec:
    """Compress with gzip from the standard library; kept so gzip objects stay readable."""

    @property
    def name(self) -> str:
        """Return the codec name recorded in manifests."""
        return "gzip"

    @property
    def extension(self) -> str:
        """Return the object key extension, without the dot."""
        return "gz"

    def compressor(self) -> StreamCompressor:
        """Create a gzip-framed deflate compressor."""
        return zlib.compressobj(level=9, wbits=16 + zlib.MAX_WBITS)

    def decompressor(self) -> StreamDecompressor:
        """Create a gzip-framed deflate decompressor."""
        return zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)


class ZstdCodec:
    """Compress with zstd at ``ZSTD_COMPRESSION_LEVEL``; the codec for new objects."""

    @property
    def name(self) -> str:
        """Return the codec name recorded in manifests."""
        return "zstd"

    @property
    def extension(self) -> str:
        """Return the object key extension, without the dot."""
        return "zst"

    def compressor(self) -> StreamCompressor:
        """Create a zstd compressor writing one frame with a content checksum.

        The checksum lets the decompressor reject corrupted content at the end of the frame, independently of the
        manifest's digest of the stored bytes.
        """
        return zstandard.ZstdCompressor(level=ZSTD_COMPRESSION_LEVEL, write_checksum=True).compressobj()

    def decompressor(self) -> StreamDecompressor:
        """Create a zstd decompressor for one frame."""
        return zstandard.ZstdDecompressor().decompressobj()


ARCHIVE_CODECS: Final[tuple[ArchiveCodec, ...]] = (ZstdCodec(), GzipCodec())
"""Every codec a reader recognizes."""


def codec_for_key(key: str) -> ArchiveCodec:
    """Return the codec matching an object key's extension.

    Raises:
        KudosArchiveError: The key does not end with a known codec's extension.
    """
    for codec in ARCHIVE_CODECS:
        if key.endswith(f".csv.{codec.extension}"):
            return codec
    raise KudosArchiveError(f"Unknown archive codec for key {key!r}")


def manifest_key(object_key: str) -> str:
    """Return the key of the manifest stored beside ``object_key``."""
    return f"{object_key}{ARCHIVE_MANIFEST_SUFFIX}"


def unit_upper_from_key(key: str) -> str:
    """Parse the unit upper bound, as written in the source range, from an object key.

    Keys end with ``<partition>__<lower>_<upper>.csv.<extension>``, and the bound is ``%Y%m%dT%H%M%S``, so the parsed
    strings sort in time order.

    Raises:
        KudosArchiveError: The key does not have that layout.
    """
    codec = codec_for_key(key)
    stem = key.removesuffix(f".csv.{codec.extension}")
    _, separator, bounds = stem.rpartition("__")
    _, bound_separator, upper = bounds.rpartition("_")
    if not separator or not bound_separator or not upper:
        raise KudosArchiveError(f"Archive key {key!r} does not end with <partition>__<lower>_<upper>")
    return upper


def parse_source_range(source_range: str) -> tuple[str, str | None, str]:
    """Parse ``<partition>/<lower or min>_<upper>`` into the partition name and the two bound strings.

    Raises:
        KudosArchiveError: The value does not have that layout.
    """
    partition_name, separator, bounds = source_range.partition("/")
    lower, bound_separator, upper = bounds.rpartition("_")
    if not separator or not bound_separator or not partition_name or not lower or not upper:
        raise KudosArchiveError(f"Malformed source range {source_range!r}")
    return partition_name, None if lower == SOURCE_RANGE_MINIMUM else lower, upper


@dataclass(frozen=True, kw_only=True)
class ArchiveColumn:
    """Represents one archived column as PostgreSQL described it at export."""

    name: str
    """The column name, as in the CSV header."""
    postgres_type: str
    """The type as ``format_type`` writes it, modifiers included, such as ``numeric(20,2)``."""


@dataclass(frozen=True, kw_only=True)
class ArchiveManifest:
    """Represents the control totals and digest stored beside one archive object."""

    schema_version: int
    """The manifest layout version."""
    dataset: str
    """The ``KudosRetentionDataset`` value the rows came from."""
    partition_name: str
    """The partition the rows came from."""
    unit_lower: str | None
    """The unit's inclusive lower ``created`` bound as ``%Y%m%dT%H%M%S``, or None for the partition's minimum."""
    unit_upper: str
    """The unit's exclusive upper ``created`` bound as ``%Y%m%dT%H%M%S``."""
    row_count: int
    """The CSV data rows in the object."""
    compressed_sha256: str
    """The hex SHA-256 of the stored (compressed) bytes."""
    compressed_bytes: int
    """The stored object size."""
    columns: list[str]
    """The CSV header."""
    column_types: list[ArchiveColumn] | None
    """Each column's name and PostgreSQL type at export, in CSV column order. None for a version 1 manifest."""
    codec: str
    """The ``ArchiveCodec.name`` that compressed the object."""
    amount_sum: str
    """The sum of the ``amount`` column, as a decimal string."""
    per_user_totals: dict[str, list[str]] | None
    """Currency only: user id to ``[balance_sum, escrow_sum]`` as decimal strings; None for statistics."""

    def to_json(self) -> str:
        """Convert the manifest to its stored JSON form."""
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "dataset": self.dataset,
                "partition_name": self.partition_name,
                "unit_lower": self.unit_lower,
                "unit_upper": self.unit_upper,
                "row_count": self.row_count,
                "compressed_sha256": self.compressed_sha256,
                "compressed_bytes": self.compressed_bytes,
                "columns": self.columns,
                "column_types": _column_types_to_manifest(self.column_types),
                "codec": self.codec,
                "amount_sum": self.amount_sum,
                "per_user_totals": self.per_user_totals,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> ArchiveManifest:
        """Parse a stored manifest.

        A version 1 manifest does not include ``column_types``, which parses as None.

        Raises:
            KudosArchiveError: The document is not a manifest of a known schema version, or its column types do not
                list its columns in order.
        """
        try:
            document = json.loads(raw)
            schema_version = int(document["schema_version"])
            if schema_version not in ARCHIVE_MANIFEST_READABLE_SCHEMA_VERSIONS:
                raise KudosArchiveError(f"Unsupported archive manifest schema version {schema_version}")
            per_user_totals = document["per_user_totals"]
            columns = [str(column) for column in document["columns"]]
            column_types = None
            if schema_version != ARCHIVE_MANIFEST_UNTYPED_SCHEMA_VERSION:
                column_types = _column_types_from_manifest(document["column_types"])
                if [column.name for column in column_types] != columns:
                    raise KudosArchiveError(f"Archive manifest column types do not list its columns {columns} in order")
            return cls(
                schema_version=schema_version,
                dataset=str(document["dataset"]),
                partition_name=str(document["partition_name"]),
                unit_lower=None if document["unit_lower"] is None else str(document["unit_lower"]),
                unit_upper=str(document["unit_upper"]),
                row_count=int(document["row_count"]),
                compressed_sha256=str(document["compressed_sha256"]),
                compressed_bytes=int(document["compressed_bytes"]),
                columns=columns,
                column_types=column_types,
                codec=str(document["codec"]),
                amount_sum=str(document["amount_sum"]),
                per_user_totals=(
                    None
                    if per_user_totals is None
                    else {str(user_id): [str(total) for total in totals] for user_id, totals in per_user_totals.items()}
                ),
            )
        except (ValueError, KeyError, TypeError, AttributeError) as err:
            raise KudosArchiveError(f"Malformed archive manifest: {err}") from err


def _column_types_to_manifest(column_types: list[ArchiveColumn] | None) -> list[dict[str, str]] | None:
    if column_types is None:
        return None
    return [{"name": column.name, "type": column.postgres_type} for column in column_types]


def _column_types_from_manifest(entries: list[Mapping[str, object]]) -> list[ArchiveColumn]:
    return [ArchiveColumn(name=str(entry["name"]), postgres_type=str(entry["type"])) for entry in entries]


def _describe_copy_columns(connection: PsycopgConnection, copy_sql: str) -> list[ArchiveColumn]:
    """Return the name and PostgreSQL type of each result column of ``copy_sql``, in result order.

    psycopg2 reports a result column's type without its modifier, so ``numeric(20,2)`` would read as ``numeric``. A
    temporary view over the query keeps the modifiers in ``pg_attribute``. The view is dropped in the same
    transaction, so a later export on that transaction can define it again.
    """
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE TEMPORARY VIEW {EXPORT_DESCRIBE_VIEW} AS {copy_sql}")
        cursor.execute(
            "SELECT attname, format_type(atttypid, atttypmod) FROM pg_attribute"
            " WHERE attrelid = CAST(%s AS regclass) AND attnum > 0 AND NOT attisdropped ORDER BY attnum",
            (f"pg_temp.{EXPORT_DESCRIBE_VIEW}",),
        )
        described = [ArchiveColumn(name=str(name), postgres_type=str(postgres_type)) for name, postgres_type in cursor.fetchall()]
        cursor.execute(f"DROP VIEW pg_temp.{EXPORT_DESCRIBE_VIEW}")
    return described


type PerUserTotals = dict[int, tuple[Decimal, Decimal]]
"""User id to ``(balance_sum, escrow_sum)``."""


def per_user_totals_to_manifest(totals: PerUserTotals) -> dict[str, list[str]]:
    """Convert folded per-user totals to the manifest's string form."""
    return {str(user_id): [str(balance), str(escrow)] for user_id, (balance, escrow) in sorted(totals.items())}


def per_user_totals_from_manifest(totals: Mapping[str, list[str]]) -> PerUserTotals:
    """Parse the manifest's per-user totals."""
    return {int(user_id): (Decimal(pair[0]), Decimal(pair[1])) for user_id, pair in totals.items()}


@dataclass(frozen=True, kw_only=True)
class VerificationReport:
    """Represents a full read-back of one archive object compared with its manifest."""

    compressed_sha256: str
    """The recomputed hex SHA-256 of the stored bytes."""
    compressed_bytes: int
    """The stored bytes read."""
    row_count: int
    """The CSV data rows read."""
    amount_sum: Decimal
    """The recomputed sum of the ``amount`` column."""
    per_user_totals: PerUserTotals | None
    """The recomputed per-user totals for currency objects; None for statistics."""
    mismatches: list[str] = field(default_factory=list)
    """The manifest fields whose recomputed value differs."""

    @property
    def matches_manifest(self) -> bool:
        """Return whether every recomputed value equals the manifest."""
        return not self.mismatches


@dataclass(frozen=True, kw_only=True)
class ArchiveObjectAudit:
    """Represents the check of one archive object against its manifest, with every problem found.

    A metadata check leaves ``row_count`` and ``compressed_bytes`` unset. A full read-back fills both.
    """

    key: str
    """The object key."""
    tier: ArchiveTier
    """The tier the object was listed under."""
    problems: list[str] = field(default_factory=list)
    """A description of each problem: an unreadable manifest or object, or a field that differs from the manifest."""
    row_count: int | None = None
    """The CSV data rows read, or None when the object was not read in full."""
    compressed_bytes: int | None = None
    """The stored bytes read, or None when the object was not read in full."""

    @property
    def matches_manifest(self) -> bool:
        """Return whether the manifest was read and the object matched it."""
        return not self.problems


@dataclass(frozen=True, kw_only=True)
class ArchiveVersionUsage:
    """The storage held under one tier's prefix by noncurrent object versions and delete markers."""

    noncurrent_bytes: int
    """The summed size of every noncurrent version, manifests included."""
    delete_markers: int
    """The number of delete markers."""


class _CsvFold:
    """Fold the row count, amount sum and, for currency, per-user totals of a streamed ``COPY ... CSV HEADER``.

    Text arrives in arbitrary chunks. Complete lines are held until their double quotes balance, because a quoted CSV
    field may contain a newline, and only then parsed.
    """

    def __init__(self, *, dataset: KudosRetentionDataset) -> None:
        self._fold_per_user = dataset == KudosRetentionDataset.CURRENCY
        self._text_decoder = codecs.getincrementaldecoder("utf-8")()
        self._pending: list[str] = []
        self._pending_quotes = 0
        self._partial_line = ""
        self.columns: list[str] = []
        self.row_count = 0
        self.amount_sum = Decimal(0)
        self.per_user_totals: PerUserTotals = {}
        self._amount_index = -1
        self._user_index = -1
        self._escrow_index = -1

    def feed_bytes(self, chunk: bytes) -> None:
        """Consume a chunk of UTF-8 CSV bytes."""
        self.feed_text(self._text_decoder.decode(chunk, False))

    def feed_text(self, text: str) -> None:
        """Consume a chunk of CSV text."""
        if not text:
            return
        combined = self._partial_line + text
        lines = combined.split("\n")
        self._partial_line = lines.pop()
        for line in lines:
            self._pending.append(line + "\n")
            self._pending_quotes += line.count('"')
            if self._pending_quotes % 2 == 0:
                self._parse_pending()

    def finish(self) -> None:
        """Parse whatever remains after the last chunk.

        Raises:
            KudosArchiveError: The stream ended inside a quoted field or without a header.
        """
        self.feed_text(self._text_decoder.decode(b"", True))
        if self._partial_line:
            self._pending.append(self._partial_line)
            self._pending_quotes += self._partial_line.count('"')
            self._partial_line = ""
        if self._pending:
            if self._pending_quotes % 2 != 0:
                raise KudosArchiveError("Archive CSV ends inside a quoted field")
            self._parse_pending()
        if not self.columns:
            raise KudosArchiveError("Archive CSV has no header")

    def _parse_pending(self) -> None:
        for row in csv.reader(io.StringIO("".join(self._pending))):
            self._fold_row(row)
        self._pending = []
        self._pending_quotes = 0

    def _fold_row(self, row: list[str]) -> None:
        if not self.columns:
            self._read_header(row)
            return
        amount = Decimal(row[self._amount_index])
        self.row_count += 1
        self.amount_sum += amount
        if not self._fold_per_user:
            return
        user_id = int(row[self._user_index])
        balance, escrow = self.per_user_totals.get(user_id, (Decimal(0), Decimal(0)))
        if row[self._escrow_index] == CSV_TRUE:
            escrow += amount
        else:
            balance += amount
        self.per_user_totals[user_id] = (balance, escrow)

    def _read_header(self, row: list[str]) -> None:
        self.columns = row
        try:
            self._amount_index = row.index("amount")
            if self._fold_per_user:
                self._user_index = row.index("user_id")
                self._escrow_index = row.index("escrow")
        except ValueError as err:
            raise KudosArchiveError(f"Archive CSV header lacks a required column: {row}") from err


class S3Client(Protocol):
    """The boto3 S3 client methods the archive uses."""

    def create_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        """Start a multipart upload."""
        ...

    def upload_part(self, **kwargs: Any) -> dict[str, Any]:
        """Upload one part."""
        ...

    def complete_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        """Complete a multipart upload."""
        ...

    def abort_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        """Abort a multipart upload."""
        ...

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        """Write one object."""
        ...

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        """Read one object."""
        ...

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        """Read one object's metadata."""
        ...

    def delete_objects(self, **kwargs: Any) -> dict[str, Any]:
        """Delete up to ``ARCHIVE_DELETE_BATCH_SIZE`` objects."""
        ...

    def get_paginator(self, operation_name: str) -> Any:
        """Return a paginator for a list operation."""
        ...


@dataclass(frozen=True)
class _ListedObject:
    key: str
    size: int


@dataclass(frozen=True)
class _ListedUpload:
    key: str
    upload_id: str
    initiated: datetime


def _aware_utc(moment: datetime) -> datetime:
    """Return ``moment`` as an aware UTC time. A naive value from a store that omits the zone is read as UTC."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _restore_mismatches(
    *,
    manifest: ArchiveManifest,
    stream_complete: bool,
    compressed_sha256: str,
    compressed_bytes: int,
    row_count: int,
) -> list[str]:
    """Return a description of every way a restored stream differs from its manifest, empty when it matches."""
    mismatches: list[str] = []
    if not stream_complete:
        mismatches.append("the compressed stream ends before the end of its frame")
    if compressed_sha256 != manifest.compressed_sha256:
        mismatches.append(f"compressed_sha256 {compressed_sha256} differs from the manifest's {manifest.compressed_sha256}")
    if compressed_bytes != manifest.compressed_bytes:
        mismatches.append(f"compressed_bytes {compressed_bytes} differs from the manifest's {manifest.compressed_bytes}")
    if row_count != manifest.row_count:
        mismatches.append(f"row_count {row_count} differs from the manifest's {manifest.row_count}")
    return mismatches


class _MultipartSink:
    """Receive ``COPY ... TO STDOUT`` output, fold it, compress it and upload it in parts.

    psycopg2 calls ``write`` with each chunk of the copy stream.
    """

    def __init__(
        self,
        *,
        client: S3Client,
        bucket: str,
        key: str,
        upload_id: str,
        codec: ArchiveCodec,
        fold: _CsvFold,
        max_compressed_bytes: int | None = None,
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._key = key
        self._upload_id = upload_id
        self._compressor = codec.compressor()
        self._fold = fold
        self._digest = hashlib.sha256()
        self._buffer = bytearray()
        self._parts: list[dict[str, Any]] = []
        self.compressed_bytes = 0
        self._max_compressed_bytes = max_compressed_bytes

    def write(self, chunk: bytes | str) -> int:
        """Consume one chunk of the copy stream; returns its length."""
        raw = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
        self._fold.feed_bytes(raw)
        self._append_compressed(self._compressor.compress(raw))
        return len(chunk)

    def finish(self) -> tuple[str, list[dict[str, Any]]]:
        """Flush the compressor, upload the final part and return the digest and part list."""
        self._fold.finish()
        self._append_compressed(self._compressor.flush())
        if self._buffer or not self._parts:
            self._upload_part()
        return self._digest.hexdigest(), self._parts

    def _append_compressed(self, compressed: bytes) -> None:
        if not compressed:
            return
        if self._max_compressed_bytes is not None and self.compressed_bytes + len(compressed) > self._max_compressed_bytes:
            raise ArchiveBudgetExceeded(
                f"compressed upload exceeds remaining allowance of {self._max_compressed_bytes} bytes",
            )
        self._digest.update(compressed)
        self.compressed_bytes += len(compressed)
        self._buffer.extend(compressed)
        if len(self._buffer) >= ARCHIVE_PART_BYTES:
            self._upload_part()

    def _upload_part(self) -> None:
        part_number = len(self._parts) + 1
        response = self._client.upload_part(
            Bucket=self._bucket,
            Key=self._key,
            UploadId=self._upload_id,
            PartNumber=part_number,
            Body=bytes(self._buffer),
        )
        self._parts.append({"ETag": response["ETag"], "PartNumber": part_number})
        self._buffer = bytearray()


class _ChunkReader(io.RawIOBase):
    """Expose an iterator of byte chunks as a readable file for ``COPY ... FROM STDIN``."""

    def __init__(self, chunks: Iterator[bytes]) -> None:
        super().__init__()
        self._chunks = chunks
        self._current = memoryview(b"")

    def readable(self) -> bool:
        """Return True; the reader is readable."""
        return True

    def read(self, size: int = -1) -> bytes:
        """Return up to ``size`` bytes, or everything left when ``size`` is negative. The result is empty only at the end.

        A read returns at most the rest of the current chunk. Slicing a memoryview copies nothing, so a decompressed
        chunk much larger than ``size`` costs time linear in its length, not quadratic.
        """
        if size < 0:
            remaining = [bytes(self._current), *self._chunks]
            self._current = memoryview(b"")
            return b"".join(remaining)
        while not self._current:
            chunk = next(self._chunks, None)
            if chunk is None:
                return b""
            self._current = memoryview(chunk)
        result = bytes(self._current[:size])
        self._current = self._current[size:]
        return result


class KudosArchive:
    """Read and write kudos retention archive objects in one bucket.

    Thread Safety:
        Methods share one boto3 client, which is thread-safe; the retention job calls them from one thread.
    """

    def __init__(self, *, client: S3Client, bucket: str, codec: ArchiveCodec | None = None, prefix: str = "") -> None:
        """Create an archive client.

        Args:
            client: The S3 client scoped to the archive bucket.
            bucket: The archive bucket.
            codec: The codec for new objects; ``ZstdCodec`` when None.
            prefix: The key prefix of this deployment's objects; normalized by ``normalize_archive_prefix``.

        Raises:
            ValueError: The prefix is not a valid key prefix.
        """
        self._client = client
        self._bucket = bucket
        self._prefix = normalize_archive_prefix(prefix)
        self._codec: ArchiveCodec = codec if codec is not None else ZstdCodec()

    @property
    def bucket(self) -> str:
        """Return the archive bucket."""
        return self._bucket

    @property
    def prefix(self) -> str:
        """Return the normalized key prefix of this deployment's objects, empty or ending in one slash."""
        return self._prefix

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] = os.environ) -> KudosArchive | None:
        """Create the archive client, or return None unless the endpoint, bucket, access key id and secret are all set.

        A partial configuration builds no client, because boto3 would otherwise sign with the default credential
        chain. Logs one warning naming the unset variables.

        Args:
            environ: Variables to read; the process environment by default.

        Raises:
            ValueError: ``KUDOS_ARCHIVE_PREFIX`` is not a valid key prefix.
        """
        prefix = normalize_archive_prefix(environ.get(ARCHIVE_PREFIX_ENV, ""))
        missing = [variable for variable in ARCHIVE_REQUIRED_ENV if not environ.get(variable)]
        if missing:
            logger.warning(f"{', '.join(missing)} unset; kudos retention archive is not configured")
            return None
        client = boto3.client(
            "s3",
            endpoint_url=environ[ARCHIVE_ACCOUNT_ENV],
            aws_access_key_id=environ[ARCHIVE_ACCESS_KEY_ID_ENV],
            aws_secret_access_key=environ[ARCHIVE_SECRET_ACCESS_KEY_ENV],
            region_name=environ.get(ARCHIVE_REGION_ENV) or DEFAULT_ARCHIVE_REGION,
            config=Config(
                signature_version="s3v4",
                connect_timeout=ARCHIVE_CONNECT_TIMEOUT_SECONDS,
                read_timeout=ARCHIVE_READ_TIMEOUT_SECONDS,
                retries={"max_attempts": ARCHIVE_MAX_ATTEMPTS},
            ),
        )
        return cls(client=client, bucket=environ[ARCHIVE_BUCKET_ENV], prefix=prefix)

    def object_key(self, *, tier: ArchiveTier, source_range: str) -> str:
        """Return the key of a unit's object, written with this client's codec.

        The key has no dataset segment. The tier already names it, and the manifest records the dataset.
        """
        return f"{self._prefix}{tier}/{source_range.replace('/', '__')}.csv.{self._codec.extension}"

    def put_unit(
        self,
        *,
        tier: ArchiveTier,
        dataset: KudosRetentionDataset,
        source_range: str,
        copy_sql: str,
        connection: PsycopgConnection,
        max_compressed_bytes: int | None = None,
    ) -> ArchiveManifest:
        """Stream a query's rows as compressed CSV into one object and return its manifest.

        The query runs as ``COPY (<copy_sql>) TO STDOUT WITH (FORMAT csv, HEADER)`` on ``connection``. Rows are folded
        and compressed as they arrive and uploaded in ``ARCHIVE_PART_BYTES`` parts, so the compressed output held in
        memory is at most one part. For currency, the per-user totals also stay in memory until the manifest is built.
        They hold one entry per distinct user in the unit, a few hundred bytes each, and the manifest's string form
        holds a second copy, so memory grows with the distinct users of the unit, not with its rows.
        Before the copy, a temporary view over the query is read in the same transaction for each result column's
        PostgreSQL type, which the manifest records so a restore does not depend on the live schema.
        The manifest is not stored; call ``put_manifest``.

        Args:
            tier: The budget class of the object.
            dataset: The dataset the rows come from; currency also folds per-user totals.
            source_range: The unit's source range, which forms the key and the manifest's unit fields.
            copy_sql: A ``SELECT`` with every value inlined; ``COPY`` takes no bound parameters.
            connection: A psycopg2 connection; the copy runs in its current transaction, which the caller ends.
            max_compressed_bytes: Maximum compressed bytes of the completed object, including the final flush.
                An oversized upload is aborted without replacing any object already at the key.

        Raises:
            KudosArchiveError: An object-store request failed, ``source_range`` is malformed, or the CSV header
                differs from the described columns. The multipart upload is aborted.
            ArchiveBudgetExceeded: The measured upload exceeds ``max_compressed_bytes``, and no object is published.
        """
        partition_name, unit_lower, unit_upper = parse_source_range(source_range)
        key = self.object_key(tier=tier, source_range=source_range)
        fold = _CsvFold(dataset=dataset)
        column_types = _describe_copy_columns(connection, copy_sql)
        described_names = [column.name for column in column_types]
        try:
            upload_id = str(self._client.create_multipart_upload(Bucket=self._bucket, Key=key)["UploadId"])
        except (BotoCoreError, ClientError) as err:
            raise KudosArchiveError(f"Starting archive upload {key} failed: {err}") from err
        sink = _MultipartSink(
            client=self._client,
            bucket=self._bucket,
            key=key,
            upload_id=upload_id,
            codec=self._codec,
            fold=fold,
            max_compressed_bytes=max_compressed_bytes,
        )
        try:
            with connection.cursor() as cursor:
                cursor.copy_expert(f"COPY ({copy_sql}) TO STDOUT WITH (FORMAT csv, HEADER)", sink)
            digest, parts = sink.finish()
            # The view and the COPY plan the query separately, and recorded types must belong to the archived columns.
            if fold.columns != described_names:
                raise KudosArchiveError(f"Archive CSV header {fold.columns} differs from the described columns {described_names}")
            self._client.complete_multipart_upload(
                Bucket=self._bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except (BotoCoreError, ClientError) as err:
            self._abort_upload(key, upload_id)
            raise KudosArchiveError(f"Archive upload {key} failed: {err}") from err
        except BaseException:
            # The database or the fold failed; the parts uploaded so far must not linger as an incomplete upload.
            self._abort_upload(key, upload_id)
            raise
        return ArchiveManifest(
            schema_version=ARCHIVE_MANIFEST_SCHEMA_VERSION,
            dataset=str(dataset),
            partition_name=partition_name,
            unit_lower=unit_lower,
            unit_upper=unit_upper,
            row_count=fold.row_count,
            compressed_sha256=digest,
            compressed_bytes=sink.compressed_bytes,
            columns=fold.columns,
            column_types=column_types,
            codec=self._codec.name,
            amount_sum=str(fold.amount_sum),
            per_user_totals=per_user_totals_to_manifest(fold.per_user_totals) if dataset == KudosRetentionDataset.CURRENCY else None,
        )

    def put_manifest(self, key: str, manifest: ArchiveManifest) -> None:
        """Store ``manifest`` beside the object at ``key``.

        Raises:
            KudosArchiveError: The upload failed.
        """
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=manifest_key(key),
                Body=manifest.to_json().encode("utf-8"),
                ContentType="application/json",
            )
        except (BotoCoreError, ClientError) as err:
            raise KudosArchiveError(f"Archive manifest upload for {key} failed: {err}") from err

    def get_manifest(self, key: str) -> ArchiveManifest:
        """Return the manifest stored beside the object at ``key``.

        Raises:
            KudosArchiveError: The read failed or the manifest is malformed.
        """
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=manifest_key(key))
            raw = response["Body"].read()
        except (BotoCoreError, ClientError) as err:
            raise KudosArchiveError(f"Archive manifest read for {key} failed: {err}") from err
        return ArchiveManifest.from_json(raw)

    def verify_unit(self, key: str, manifest: ArchiveManifest) -> VerificationReport:
        """Read the object at ``key`` back in full and compare its digest and control totals with ``manifest``.

        Raises:
            KudosArchiveError: The read failed, or the object does not decompress or parse.
        """
        codec = codec_for_key(key)
        dataset = KudosRetentionDataset(manifest.dataset)
        fold = _CsvFold(dataset=dataset)
        digest = hashlib.sha256()
        compressed_bytes = 0
        decompressor = codec.decompressor()
        try:
            for chunk in self._iter_object(key):
                digest.update(chunk)
                compressed_bytes += len(chunk)
                fold.feed_bytes(decompressor.decompress(chunk))
        except (zlib.error, zstandard.ZstdError) as err:
            raise KudosArchiveError(f"Archive object {key} does not decompress: {err}") from err
        fold.finish()
        per_user_totals = fold.per_user_totals if dataset == KudosRetentionDataset.CURRENCY else None
        mismatches: list[str] = []
        if digest.hexdigest() != manifest.compressed_sha256:
            mismatches.append("compressed_sha256")
        if compressed_bytes != manifest.compressed_bytes:
            mismatches.append("compressed_bytes")
        if fold.row_count != manifest.row_count:
            mismatches.append("row_count")
        if fold.amount_sum != Decimal(manifest.amount_sum):
            mismatches.append("amount_sum")
        if fold.columns != manifest.columns:
            mismatches.append("columns")
        manifest_totals = None if manifest.per_user_totals is None else per_user_totals_from_manifest(manifest.per_user_totals)
        if per_user_totals != manifest_totals:
            mismatches.append("per_user_totals")
        return VerificationReport(
            compressed_sha256=digest.hexdigest(),
            compressed_bytes=compressed_bytes,
            row_count=fold.row_count,
            amount_sum=fold.amount_sum,
            per_user_totals=per_user_totals,
            mismatches=mismatches,
        )

    def tier_bytes(self, tier: ArchiveTier) -> int:
        """Return the stored bytes of a tier's objects, excluding manifests.

        Raises:
            KudosArchiveError: The listing failed.
        """
        return sum(listed.size for listed in self._list_objects(tier))

    def object_sizes(self, tier: ArchiveTier) -> dict[str, int]:
        """Return the stored size of each of a tier's objects by key, excluding manifests.

        Raises:
            KudosArchiveError: The listing failed.
        """
        return {listed.key: listed.size for listed in self._list_objects(tier)}

    def version_usage(self, tier: ArchiveTier) -> ArchiveVersionUsage | None:
        """Return the noncurrent bytes and delete markers under a tier's prefix, or None when they cannot be listed.

        ``tier_bytes`` and ``object_sizes`` count current objects only. In a versioned bucket an evicted or re-exported
        object stays behind as a noncurrent version and costs storage until a lifecycle rule expires it, so this
        listing is the only place that storage shows up. Manifests are included because their old versions cost
        storage as well.

        A store that does not implement ``ListObjectVersions``, a key without permission to list versions, and an
        unreachable store all return None after a warning. The figure is informational, so it does not stop an audit.
        """
        prefix = f"{self._prefix}{tier}/"
        noncurrent_bytes = 0
        delete_markers = 0
        try:
            paginator = self._client.get_paginator("list_object_versions")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                for version in page.get("Versions", []):
                    if not version.get("IsLatest", True):
                        noncurrent_bytes += int(version["Size"])
                delete_markers += len(page.get("DeleteMarkers", []))
        except (BotoCoreError, ClientError) as err:
            logger.warning(f"Kudos archive version listing of {prefix!r} failed, so its noncurrent bytes are unknown: {err}")
            return None
        return ArchiveVersionUsage(noncurrent_bytes=noncurrent_bytes, delete_markers=delete_markers)

    def enforce_budget(self, *, budget_bytes: int, kept_keys: Collection[str] = frozenset()) -> list[str]:
        """Delete the oldest statistic objects, by unit upper bound, until the statistics tier fits ``budget_bytes``.

        An operator tool. The retention tick does not call it, and currency objects are never deleted. Each deleted
        object's manifest is deleted with it. Deletes go out one batch at a time, so a failure leaves the earlier
        batches deleted and reports them. The tier can stay above the budget when ``kept_keys`` holds too much of it.

        Args:
            budget_bytes: The byte budget of the statistics tier.
            kept_keys: Object keys that are not deleted, such as the dumps of units whose raw rows are still online.

        Returns:
            The deleted object keys, oldest first.

        Raises:
            KudosArchiveError: The listing failed, before anything was deleted.
            ArchiveEvictionError: A delete failed. Its ``deleted_keys`` holds the objects deleted before it.
        """
        statistics_objects = self._list_objects(ArchiveTier.STATISTICS)
        total = sum(listed.size for listed in statistics_objects)
        candidates: list[str] = []
        for listed in sorted(statistics_objects, key=lambda candidate: (unit_upper_from_key(candidate.key), candidate.key)):
            if total <= budget_bytes:
                break
            if listed.key in kept_keys:
                continue
            candidates.append(listed.key)
            total -= listed.size
        evicted: list[str] = []
        for start in range(0, len(candidates), ARCHIVE_DELETE_BATCH_SIZE // 2):
            batch = candidates[start : start + ARCHIVE_DELETE_BATCH_SIZE // 2]
            try:
                failed_keys = self._delete_keys([key for candidate_key in batch for key in (candidate_key, manifest_key(candidate_key))])
            except KudosArchiveError as err:
                raise ArchiveEvictionError(f"{err}. {len(evicted)} statistic objects were deleted before it", deleted_keys=evicted) from err
            evicted.extend(candidate_key for candidate_key in batch if candidate_key not in failed_keys)
            if failed_keys:
                raise ArchiveEvictionError(
                    f"Archive delete failed for {len(failed_keys)} keys, among them {sorted(failed_keys)[0]}. "
                    f"{len(evicted)} statistic objects were deleted",
                    deleted_keys=evicted,
                )
        if evicted:
            logger.warning(f"Kudos archive evicted {len(evicted)} statistic objects to fit {budget_bytes} bytes")
        return evicted

    def check_object_metadata(self, key: str, *, tier: ArchiveTier, listed_size: int) -> ArchiveObjectAudit:
        """Return the check of the object at ``key`` against its manifest without reading the object's bytes.

        The manifest is read and parsed, its stored byte count is compared with the size the listing reported, and its
        unit upper bound and codec are compared with the ones the key encodes. This finds a deleted, replaced or
        truncated object, a lost or unreadable manifest, and a manifest that belongs to another object. Damage that
        keeps the object's size is found only by ``audit_object``. Nothing is written.

        Args:
            key: The object key.
            tier: The tier the object was listed under.
            listed_size: The object's size as the listing reported it.
        """
        try:
            manifest = self.get_manifest(key)
        except KudosArchiveError as err:
            return ArchiveObjectAudit(key=key, tier=tier, problems=[f"manifest unreadable: {err}"])
        problems: list[str] = []
        if listed_size != manifest.compressed_bytes:
            problems.append(f"compressed_bytes differs from the manifest (listed {listed_size}, manifest {manifest.compressed_bytes})")
        try:
            key_upper = unit_upper_from_key(key)
            key_codec = codec_for_key(key).name
        except KudosArchiveError as err:
            return ArchiveObjectAudit(key=key, tier=tier, problems=[*problems, f"key unreadable: {err}"])
        if key_upper != manifest.unit_upper:
            problems.append(f"unit_upper differs from the manifest (key {key_upper}, manifest {manifest.unit_upper})")
        if key_codec != manifest.codec:
            problems.append(f"codec differs from the manifest (key {key_codec}, manifest {manifest.codec})")
        return ArchiveObjectAudit(key=key, tier=tier, problems=problems)

    def audit_object(self, key: str, *, tier: ArchiveTier) -> ArchiveObjectAudit:
        """Return the read-back of the object at ``key`` against its manifest, with any failure recorded as a problem.

        The checks are those of ``verify_unit``: digest, stored byte count, row count, amount sum, columns and, for
        currency, per-user totals. Nothing is written.

        Args:
            key: The object key.
            tier: The tier the object was listed under.
        """
        try:
            manifest = self.get_manifest(key)
        except KudosArchiveError as err:
            return ArchiveObjectAudit(key=key, tier=tier, problems=[f"manifest unreadable: {err}"])
        try:
            report = self.verify_unit(key, manifest)
        except KudosArchiveError as err:
            return ArchiveObjectAudit(key=key, tier=tier, problems=[f"object unreadable: {err}"])
        return ArchiveObjectAudit(
            key=key,
            tier=tier,
            problems=[f"{mismatch} differs from the manifest" for mismatch in report.mismatches],
            row_count=report.row_count,
            compressed_bytes=report.compressed_bytes,
        )

    def restore_unit(self, key: str, *, table_name: str, connection: PsycopgConnection) -> int:
        """Copy the rows of the object at ``key`` into an existing table, verified against its manifest. Return the count.

        The table must have the archived columns in the archived order. The copy runs in ``connection``'s current
        transaction, which the caller commits. The stored bytes are hashed and counted as they stream, and once the copy
        ends the digest, the compressed byte count and the copied row count must equal the manifest and the
        decompressor must have reached the end of the compressed stream. A stream cut on a row boundary otherwise
        copies a valid prefix of the rows without any error from the decompressor or from ``COPY``.

        Args:
            key: The object key.
            table_name: The scratch table to fill, unqualified or schema-qualified with a dot.
            connection: A psycopg2 connection.

        Returns:
            The rows copied, equal to the manifest's row count.

        Raises:
            KudosArchiveError: The manifest or object read failed, the object does not decompress, or the object does
                not match its manifest. The rows copied so far stay in the caller's transaction, which must roll back.
        """
        manifest = self.get_manifest(key)
        codec = codec_for_key(key)
        decompressor = codec.decompressor()
        digest = hashlib.sha256()
        compressed_bytes = 0
        identifier = psycopg2_sql.Identifier(*table_name.split("."))
        statement = psycopg2_sql.SQL("COPY {} FROM STDIN WITH (FORMAT csv, HEADER)").format(identifier)

        def decompressed_chunks() -> Iterator[bytes]:
            nonlocal compressed_bytes
            for chunk in self._iter_object(key):
                digest.update(chunk)
                compressed_bytes += len(chunk)
                yield decompressor.decompress(chunk)

        try:
            with connection.cursor() as cursor:
                cursor.copy_expert(statement, _ChunkReader(decompressed_chunks()))
                copied_rows = int(cursor.rowcount)
        except (zlib.error, zstandard.ZstdError) as err:
            raise KudosArchiveError(f"Archive object {key} does not decompress: {err}") from err
        mismatches = _restore_mismatches(
            manifest=manifest,
            stream_complete=decompressor.eof,
            compressed_sha256=digest.hexdigest(),
            compressed_bytes=compressed_bytes,
            row_count=copied_rows,
        )
        if mismatches:
            raise KudosArchiveError(f"Archive object {key} does not match its manifest: {'; '.join(mismatches)}")
        return copied_rows

    def object_size(self, key: str) -> int | None:
        """Return the stored size of the object at ``key``, or None when no object exists there.

        Args:
            key: The object key.

        Returns:
            The object's size in bytes, or None when the store reports no object at the key.

        Raises:
            KudosArchiveError: The request failed for any reason other than a missing object.
        """
        try:
            response = self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as err:
            if str(err.response.get("Error", {}).get("Code", "")) in _MISSING_OBJECT_CODES:
                return None
            raise KudosArchiveError(f"Archive object metadata read {key} failed: {err}") from err
        except BotoCoreError as err:
            raise KudosArchiveError(f"Archive object metadata read {key} failed: {err}") from err
        return int(response["ContentLength"])

    def abort_stale_multipart_uploads(self, *, older_than: timedelta) -> int:
        """Abort the incomplete multipart uploads under this archive's tier prefixes started before now minus ``older_than``.

        ``put_unit`` aborts its upload on any exception, but a killed process leaves the upload and its stored parts
        behind. Listing covers only the tier prefixes under this deployment's prefix, so another deployment's uploads
        in a shared bucket are not touched.

        Args:
            older_than: How long an upload must have been open before it counts as stale. It must exceed the longest
                export, or a running upload is aborted and its ``put_unit`` fails.

        Returns:
            The number of uploads aborted.

        Raises:
            KudosArchiveError: The store does not support ``ListMultipartUploads``, or a listing or abort failed.
        """
        cutoff = datetime.now(UTC) - older_than
        aborted = 0
        for tier in ArchiveTier:
            for stale_upload in self._list_multipart_uploads(f"{self._prefix}{tier}/"):
                if stale_upload.initiated >= cutoff:
                    continue
                try:
                    self._client.abort_multipart_upload(Bucket=self._bucket, Key=stale_upload.key, UploadId=stale_upload.upload_id)
                except (BotoCoreError, ClientError) as err:
                    raise KudosArchiveError(f"Aborting stale archive upload {stale_upload.key} failed: {err}") from err
                aborted += 1
        if aborted:
            logger.warning(f"Kudos archive aborted {aborted} stale multipart uploads started before {cutoff.isoformat()}")
        return aborted

    def _list_multipart_uploads(self, prefix: str) -> list[_ListedUpload]:
        listed: list[_ListedUpload] = []
        try:
            paginator = self._client.get_paginator("list_multipart_uploads")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                for entry in page.get("Uploads", []):
                    listed.append(
                        _ListedUpload(key=str(entry["Key"]), upload_id=str(entry["UploadId"]), initiated=_aware_utc(entry["Initiated"])),
                    )
        except ClientError as err:
            if str(err.response.get("Error", {}).get("Code", "")) in _UNSUPPORTED_OPERATION_CODES:
                raise KudosArchiveError(
                    f"The archive store does not support ListMultipartUploads, so stale uploads under {prefix!r} cannot be "
                    f"aborted here. Configure an incomplete-upload lifecycle rule on the bucket instead: {err}",
                ) from err
            raise KudosArchiveError(f"Archive multipart upload listing of {prefix!r} failed: {err}") from err
        except BotoCoreError as err:
            raise KudosArchiveError(f"Archive multipart upload listing of {prefix!r} failed: {err}") from err
        return listed

    def _iter_object(self, key: str) -> Iterator[bytes]:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            for chunk in response["Body"].iter_chunks(chunk_size=ARCHIVE_READ_CHUNK_BYTES):
                yield bytes(chunk)
        except (BotoCoreError, ClientError) as err:
            raise KudosArchiveError(f"Archive object read {key} failed: {err}") from err

    def _list_objects(self, tier: ArchiveTier) -> list[_ListedObject]:
        listed: list[_ListedObject] = []
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=f"{self._prefix}{tier}/"):
                for entry in page.get("Contents", []):
                    object_key = str(entry["Key"])
                    if object_key.endswith(ARCHIVE_MANIFEST_SUFFIX):
                        continue
                    listed.append(_ListedObject(key=object_key, size=int(entry["Size"])))
        except (BotoCoreError, ClientError) as err:
            raise KudosArchiveError(f"Archive listing of {tier} failed: {err}") from err
        return listed

    def _delete_keys(self, keys: list[str]) -> set[str]:
        """Delete ``keys`` in one request and return the keys the store reports it did not delete.

        Raises:
            KudosArchiveError: The request itself failed, so which keys were deleted is not known.
        """
        try:
            response = self._client.delete_objects(
                Bucket=self._bucket,
                Delete={"Objects": [{"Key": key} for key in keys], "Quiet": True},
            )
        except (BotoCoreError, ClientError) as err:
            raise KudosArchiveError(f"Archive delete failed: {err}") from err
        errors = response.get("Errors") or []
        return {str(error.get("Key", "")) for error in errors}

    def _abort_upload(self, key: str, upload_id: str) -> None:
        try:
            self._client.abort_multipart_upload(Bucket=self._bucket, Key=key, UploadId=upload_id)
        except (BotoCoreError, ClientError) as err:
            # The original failure is what the caller acts on. An orphaned upload only costs space until
            # abort_stale_multipart_uploads or a bucket lifecycle rule removes it.
            logger.error(f"Aborting archive upload {key} failed: {err}")


__all__ = [
    "ARCHIVE_ACCESS_KEY_ID_ENV",
    "ARCHIVE_ACCOUNT_ENV",
    "ARCHIVE_BUCKET_ENV",
    "ARCHIVE_PREFIX_ENV",
    "ARCHIVE_REGION_ENV",
    "ARCHIVE_REQUIRED_ENV",
    "ARCHIVE_SECRET_ACCESS_KEY_ENV",
    "ArchiveCodec",
    "ArchiveColumn",
    "ArchiveBudgetExceeded",
    "ArchiveEvictionError",
    "ArchiveManifest",
    "ArchiveObjectAudit",
    "ArchiveTier",
    "ArchiveVersionUsage",
    "GzipCodec",
    "KudosArchive",
    "KudosArchiveError",
    "S3Client",
    "VerificationReport",
    "ZstdCodec",
    "codec_for_key",
    "manifest_key",
    "normalize_archive_prefix",
    "parse_source_range",
    "unit_upper_from_key",
]
