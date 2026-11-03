# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Write, verify, evict and restore kudos archive objects.

Codec, manifest, key and CSV-fold tests need no store. The ``object_storage`` tests stream ``COPY`` output from the test
Postgres into the S3-compatible archive bucket the test runtime provisions.
"""

from __future__ import annotations

import base64
import contextlib
import csv
import hashlib
import io
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
import zstandard
from botocore.exceptions import ClientError

from horde.enums import KudosRetentionDataset
from horde.kudos_archive import (
    ARCHIVE_PREFIX_ENV,
    ARCHIVE_REQUIRED_ENV,
    ArchiveCodec,
    ArchiveColumn,
    ArchiveManifest,
    ArchiveTier,
    GzipCodec,
    KudosArchive,
    KudosArchiveError,
    ZstdCodec,
    _ChunkReader,
    _CsvFold,
    codec_for_key,
    manifest_key,
    normalize_archive_prefix,
    parse_source_range,
    unit_upper_from_key,
)

if TYPE_CHECKING:
    from flask import Flask

pytestmark = pytest.mark.unit


def _round_trip(codec: ArchiveCodec, payload: bytes, chunk_size: int) -> bytes:
    compressor = codec.compressor()
    compressed = b"".join(compressor.compress(payload[start : start + chunk_size]) for start in range(0, len(payload), chunk_size))
    compressed += compressor.flush()
    decompressor = codec.decompressor()
    return b"".join(decompressor.decompress(compressed[start : start + 7]) for start in range(0, len(compressed), 7))


@pytest.mark.parametrize("codec", [ZstdCodec(), GzipCodec()], ids=lambda codec: codec.name)
def test_codec_round_trips_a_chunked_stream(codec: ArchiveCodec) -> None:
    payload = "".join(f"{row},user_{row % 17},{row * 3}.25\n" for row in range(5000)).encode("utf-8")
    assert _round_trip(codec, payload, 1013) == payload


def test_codec_is_chosen_by_extension() -> None:
    assert codec_for_key("currency/p__min_20260810T000000.csv.zst").name == "zstd"
    assert codec_for_key("statistics/p__min_20260810T000000.csv.gz").name == "gzip"
    with pytest.raises(KudosArchiveError):
        codec_for_key("statistics/p__min_20260810T000000.csv.bz2")


def _manifest(**overrides: object) -> ArchiveManifest:
    fields: dict[str, object] = {
        "schema_version": 2,
        "dataset": "currency",
        "partition_name": "kudos_ledger_p_20260803",
        "unit_lower": "20260803T000000",
        "unit_upper": "20260810T000000",
        "row_count": 3,
        "compressed_sha256": "ab" * 32,
        "compressed_bytes": 120,
        "columns": ["id", "user_id", "escrow", "amount"],
        "column_types": [
            ArchiveColumn(name="id", postgres_type="bigint"),
            ArchiveColumn(name="user_id", postgres_type="integer"),
            ArchiveColumn(name="escrow", postgres_type="boolean"),
            ArchiveColumn(name="amount", postgres_type="numeric(20,2)"),
        ],
        "codec": "zstd",
        "amount_sum": "12.50",
        "per_user_totals": {"7": ["10.00", "2.50"]},
    }
    fields.update(overrides)
    return ArchiveManifest(**fields)  # type: ignore[arg-type]


def test_manifest_serialization_round_trips() -> None:
    manifest = _manifest()
    assert ArchiveManifest.from_json(manifest.to_json()) == manifest
    statistics_manifest = _manifest(dataset="statistics", unit_lower=None, per_user_totals=None)
    assert ArchiveManifest.from_json(statistics_manifest.to_json().encode("utf-8")) == statistics_manifest


def test_manifest_rejects_an_unknown_schema_version_and_malformed_documents() -> None:
    with pytest.raises(KudosArchiveError, match="schema version"):
        ArchiveManifest.from_json(_manifest(schema_version=3).to_json())
    with pytest.raises(KudosArchiveError, match="Malformed"):
        ArchiveManifest.from_json('{"schema_version": 1}')
    with pytest.raises(KudosArchiveError, match="Malformed"):
        ArchiveManifest.from_json(_manifest(column_types=None).to_json())
    with pytest.raises(KudosArchiveError, match="do not list its columns"):
        ArchiveManifest.from_json(_manifest(columns=["id", "user_id", "amount", "escrow"]).to_json())


def test_version_1_manifest_parses_without_column_types() -> None:
    version_1 = _manifest(schema_version=1, column_types=None)
    assert ArchiveManifest.from_json(version_1.to_json()) == version_1


def test_object_key_layout() -> None:
    archive = KudosArchive(client=object(), bucket="archive")  # type: ignore[arg-type]
    key = archive.object_key(tier=ArchiveTier.STATISTICS, source_range="kudos_stat_events_p_legacy/min_20260810T000000")
    assert key == "statistics/kudos_stat_events_p_legacy__min_20260810T000000.csv.zst"
    assert manifest_key(key) == f"{key}.manifest.json"
    assert unit_upper_from_key(key) == "20260810T000000"
    gzip_archive = KudosArchive(client=object(), bucket="archive", codec=GzipCodec())  # type: ignore[arg-type]
    assert gzip_archive.object_key(
        tier=ArchiveTier.CURRENCY,
        source_range="kudos_ledger_p_20260803/20260803T000000_20260810T000000",
    ).endswith("kudos_ledger_p_20260803__20260803T000000_20260810T000000.csv.gz")


def test_object_key_carries_the_deployment_prefix() -> None:
    source_range = "kudos_ledger_p_20260803/20260803T000000_20260810T000000"
    unprefixed = KudosArchive(client=object(), bucket="archive")  # type: ignore[arg-type]
    prefixed = KudosArchive(client=object(), bucket="archive", prefix="/dev/horde/")  # type: ignore[arg-type]
    plain_key = unprefixed.object_key(tier=ArchiveTier.CURRENCY, source_range=source_range)
    prefixed_key = prefixed.object_key(tier=ArchiveTier.CURRENCY, source_range=source_range)
    assert unprefixed.prefix == ""
    assert plain_key == "currency/kudos_ledger_p_20260803__20260803T000000_20260810T000000.csv.zst"
    assert prefixed_key == f"dev/horde/{plain_key}"
    assert unit_upper_from_key(prefixed_key) == "20260810T000000"


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [("", ""), ("/", ""), ("prod", "prod/"), ("/prod", "prod/"), ("prod//", "prod/"), ("dev/rehearsal-1.v2_a/", "dev/rehearsal-1.v2_a/")],
)
def test_archive_prefix_normalization(raw: str, normalized: str) -> None:
    assert normalize_archive_prefix(raw) == normalized


@pytest.mark.parametrize("raw", ["prod env", "prod\\x", "prod?", "dev//prod", "../prod", "dev/./prod", "pröd"])
def test_bad_archive_prefix_is_refused_at_load(raw: str) -> None:
    from horde.database.kudos_retention import load_retention_policy

    with pytest.raises(ValueError, match=ARCHIVE_PREFIX_ENV):
        normalize_archive_prefix(raw)
    with pytest.raises(ValueError, match=ARCHIVE_PREFIX_ENV):
        load_retention_policy({ARCHIVE_PREFIX_ENV: raw})
    environ = dict.fromkeys(ARCHIVE_REQUIRED_ENV, "x") | {ARCHIVE_PREFIX_ENV: raw}
    with pytest.raises(ValueError, match=ARCHIVE_PREFIX_ENV):
        KudosArchive.from_environment(environ)


def test_source_range_parsing() -> None:
    assert parse_source_range("kudos_ledger_p_legacy/min_20260810T000000") == ("kudos_ledger_p_legacy", None, "20260810T000000")
    assert parse_source_range("kudos_ledger_p_20260803/20260803T000000_20260810T000000") == (
        "kudos_ledger_p_20260803",
        "20260803T000000",
        "20260810T000000",
    )
    with pytest.raises(KudosArchiveError):
        parse_source_range("no-slash")


def test_csv_fold_handles_quoted_newlines_split_across_chunks() -> None:
    text = 'id,user_id,escrow,amount,detail\n1,7,f,10.00,"{""reason"": ""line\none""}"\n2,7,t,2.50,\n3,8,f,-1.25,"a,b"\n'
    fold = _CsvFold(dataset=KudosRetentionDataset.CURRENCY)
    encoded = text.encode("utf-8")
    for start in range(0, len(encoded), 5):
        fold.feed_bytes(encoded[start : start + 5])
    fold.finish()
    assert fold.columns == ["id", "user_id", "escrow", "amount", "detail"]
    assert fold.row_count == 3
    assert fold.amount_sum == Decimal("11.25")
    assert fold.per_user_totals == {7: (Decimal("10.00"), Decimal("2.50")), 8: (Decimal("-1.25"), Decimal(0))}


def test_from_environment_requires_every_variable() -> None:
    environ = {variable: "x" for variable in ARCHIVE_REQUIRED_ENV}
    environ.pop(ARCHIVE_REQUIRED_ENV[-1])
    assert KudosArchive.from_environment(environ) is None


def test_chunk_reader_returns_every_byte_in_order_with_short_reads() -> None:
    chunks = [b"", b"a" * 10, b"", b"bc", b"d" * 7]
    reader = _ChunkReader(iter(chunks))
    pieces: list[bytes] = []
    while piece := reader.read(4):
        assert len(piece) <= 4
        pieces.append(piece)
    assert b"".join(pieces) == b"".join(chunks)
    assert reader.read(4) == b""
    remaining_reader = _ChunkReader(iter([b"xy", b"z"]))
    assert remaining_reader.read(1) == b"x"
    assert remaining_reader.read() == b"yz"


# Restore verification, multipart cleanup and object size against an in-memory store

RESTORE_CSV = "id,user_id,escrow,amount\n1,7,f,10.00\n2,7,t,2.50\n3,8,f,-1.25\n"
RESTORE_KEY = "currency/kudos_ledger_p_20260803__20260803T000000_20260810T000000.csv.zst"


def _client_error(code: str, operation_name: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation_name)


class _FakeBody:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def iter_chunks(self, chunk_size: int) -> Iterator[bytes]:
        for start in range(0, len(self._payload), chunk_size):
            yield self._payload[start : start + chunk_size]


class _FakeUploadPaginator:
    def __init__(self, store: _FakeS3) -> None:
        self._store = store

    def paginate(self, *, Bucket: str, Prefix: str) -> Iterator[dict[str, Any]]:  # noqa: N803 (boto3 parameter names)
        if self._store.list_uploads_error is not None:
            raise self._store.list_uploads_error
        yield {"Uploads": [upload for upload in self._store.uploads if str(upload["Key"]).startswith(Prefix)]}


class _FakeS3:
    """Hold objects and multipart uploads in memory for the archive methods that read, inspect or abort."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploads: list[dict[str, Any]] = []
        self.aborted: list[tuple[str, str]] = []
        self.list_uploads_error: ClientError | None = None
        self.head_error: ClientError | None = None

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 (boto3 parameter names)
        if Key not in self.objects:
            raise _client_error("NoSuchKey", "GetObject")
        return {"Body": _FakeBody(self.objects[Key])}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 (boto3 parameter names)
        if self.head_error is not None:
            raise self.head_error
        if Key not in self.objects:
            raise _client_error("404", "HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def abort_multipart_upload(self, *, Bucket: str, Key: str, UploadId: str) -> dict[str, Any]:  # noqa: N803 (boto3 parameter names)
        self.aborted.append((Key, UploadId))
        return {}

    def get_paginator(self, operation_name: str) -> _FakeUploadPaginator:
        assert operation_name == "list_multipart_uploads"
        return _FakeUploadPaginator(self)


class _FakeCopyCursor:
    """Read a ``COPY ... FROM STDIN WITH (FORMAT csv, HEADER)`` stream as psycopg2 does and count its data rows."""

    def __init__(self, copied: list[list[str]]) -> None:
        self._copied = copied
        self.rowcount = -1

    def __enter__(self) -> _FakeCopyCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def copy_expert(self, statement: object, file: io.RawIOBase, size: int = 8192) -> None:
        received = bytearray()
        while chunk := file.read(size):
            received.extend(chunk)
        rows = list(csv.reader(io.StringIO(received.decode("utf-8"))))[1:]
        self._copied.extend(rows)
        self.rowcount = len(rows)


class _FakeCopyConnection:
    def __init__(self) -> None:
        self.copied: list[list[str]] = []

    def cursor(self) -> _FakeCopyCursor:
        return _FakeCopyCursor(self.copied)


def _compress_per_row(csv_text: str) -> tuple[bytes, list[int]]:
    """Return a checksummed zstd frame of ``csv_text`` and the frame length after each line's block is flushed."""
    compressor = zstandard.ZstdCompressor(level=3, write_checksum=True).compressobj()
    compressed = b""
    line_ends: list[int] = []
    for line in csv_text.splitlines(keepends=True):
        compressed += compressor.compress(line.encode("utf-8"))
        compressed += compressor.flush(zstandard.COMPRESSOBJ_FLUSH_BLOCK)
        line_ends.append(len(compressed))
    return compressed + compressor.flush(), line_ends


def _manifest_for(stored: bytes, *, row_count: int) -> ArchiveManifest:
    return _manifest(
        row_count=row_count,
        compressed_sha256=hashlib.sha256(stored).hexdigest(),
        compressed_bytes=len(stored),
    )


def _store(client: _FakeS3, key: str, stored: bytes, manifest: ArchiveManifest) -> None:
    client.objects[key] = stored
    client.objects[manifest_key(key)] = manifest.to_json().encode("utf-8")


def test_restore_unit_copies_a_matching_object() -> None:
    client = _FakeS3()
    compressed, _ = _compress_per_row(RESTORE_CSV)
    _store(client, RESTORE_KEY, compressed, _manifest_for(compressed, row_count=3))
    connection = _FakeCopyConnection()
    archive = KudosArchive(client=client, bucket="archive")  # type: ignore[arg-type]
    assert archive.restore_unit(RESTORE_KEY, table_name="public.kudos_restore_check", connection=connection) == 3  # type: ignore[arg-type]
    assert [row[0] for row in connection.copied] == ["1", "2", "3"]


def test_restore_unit_refuses_an_object_truncated_on_a_row_boundary() -> None:
    client = _FakeS3()
    compressed, line_ends = _compress_per_row(RESTORE_CSV)
    # The header and the first two rows decompress completely, so COPY alone would accept the prefix.
    truncated = compressed[: line_ends[2]]
    _store(client, RESTORE_KEY, truncated, _manifest_for(compressed, row_count=3))
    connection = _FakeCopyConnection()
    archive = KudosArchive(client=client, bucket="archive")  # type: ignore[arg-type]
    with pytest.raises(KudosArchiveError, match="does not match its manifest") as raised:
        archive.restore_unit(RESTORE_KEY, table_name="kudos_restore_check", connection=connection)  # type: ignore[arg-type]
    assert len(connection.copied) == 2
    for mismatch in ("ends before the end of its frame", "compressed_sha256", "compressed_bytes", "row_count"):
        assert mismatch in str(raised.value)


def test_restore_unit_refuses_an_unfinished_frame_even_when_the_manifest_describes_it() -> None:
    client = _FakeS3()
    compressed, line_ends = _compress_per_row(RESTORE_CSV)
    truncated = compressed[: line_ends[2]]
    _store(client, RESTORE_KEY, truncated, _manifest_for(truncated, row_count=2))
    archive = KudosArchive(client=client, bucket="archive")  # type: ignore[arg-type]
    with pytest.raises(KudosArchiveError, match="ends before the end of its frame"):
        archive.restore_unit(RESTORE_KEY, table_name="kudos_restore_check", connection=_FakeCopyConnection())  # type: ignore[arg-type]


def test_restore_unit_refuses_an_object_whose_manifest_belongs_to_another_object() -> None:
    client = _FakeS3()
    compressed, _ = _compress_per_row(RESTORE_CSV)
    other_compressed, _ = _compress_per_row("id,user_id,escrow,amount\n9,7,f,1.00\n")
    _store(client, RESTORE_KEY, compressed, _manifest_for(other_compressed, row_count=1))
    archive = KudosArchive(client=client, bucket="archive")  # type: ignore[arg-type]
    with pytest.raises(KudosArchiveError, match="does not match its manifest") as raised:
        archive.restore_unit(RESTORE_KEY, table_name="kudos_restore_check", connection=_FakeCopyConnection())  # type: ignore[arg-type]
    assert "ends before the end of its frame" not in str(raised.value)
    assert "row_count 3 differs" in str(raised.value)


def test_restore_unit_requires_a_manifest() -> None:
    client = _FakeS3()
    compressed, _ = _compress_per_row(RESTORE_CSV)
    client.objects[RESTORE_KEY] = compressed
    archive = KudosArchive(client=client, bucket="archive")  # type: ignore[arg-type]
    with pytest.raises(KudosArchiveError, match="manifest read"):
        archive.restore_unit(RESTORE_KEY, table_name="kudos_restore_check", connection=_FakeCopyConnection())  # type: ignore[arg-type]


def test_zstd_codec_writes_a_content_checksum() -> None:
    compressor = ZstdCodec().compressor()
    compressed = compressor.compress(RESTORE_CSV.encode("utf-8")) + compressor.flush()
    assert zstandard.get_frame_parameters(compressed).has_checksum


def test_object_size_reports_present_and_absent_objects() -> None:
    client = _FakeS3()
    client.objects[RESTORE_KEY] = b"x" * 42
    archive = KudosArchive(client=client, bucket="archive")  # type: ignore[arg-type]
    assert archive.object_size(RESTORE_KEY) == 42
    assert archive.object_size("currency/absent.csv.zst") is None
    client.head_error = _client_error("403", "HeadObject")
    with pytest.raises(KudosArchiveError, match="metadata read"):
        archive.object_size(RESTORE_KEY)


def test_abort_stale_multipart_uploads_aborts_only_old_uploads_under_the_prefix() -> None:
    client = _FakeS3()
    now = datetime.now(UTC)
    client.uploads = [
        {"Key": "prod/currency/old.csv.zst", "UploadId": "old-currency", "Initiated": now - timedelta(days=2)},
        {"Key": "prod/statistics/old.csv.zst", "UploadId": "old-statistics", "Initiated": now - timedelta(hours=25)},
        {"Key": "prod/statistics/running.csv.zst", "UploadId": "running", "Initiated": now - timedelta(minutes=5)},
        {"Key": "dev/currency/old.csv.zst", "UploadId": "other-deployment", "Initiated": now - timedelta(days=2)},
        {"Key": "prod/elsewhere/old.csv.zst", "UploadId": "outside-the-tiers", "Initiated": now - timedelta(days=2)},
    ]
    archive = KudosArchive(client=client, bucket="archive", prefix="prod")  # type: ignore[arg-type]
    assert archive.abort_stale_multipart_uploads(older_than=timedelta(days=1)) == 2
    assert sorted(client.aborted) == [("prod/currency/old.csv.zst", "old-currency"), ("prod/statistics/old.csv.zst", "old-statistics")]


def test_abort_stale_multipart_uploads_reads_a_naive_initiated_time_as_utc() -> None:
    client = _FakeS3()
    naive_old = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=2)
    client.uploads = [{"Key": "currency/old.csv.zst", "UploadId": "old", "Initiated": naive_old}]
    archive = KudosArchive(client=client, bucket="archive")  # type: ignore[arg-type]
    assert archive.abort_stale_multipart_uploads(older_than=timedelta(days=1)) == 1


def test_abort_stale_multipart_uploads_reports_a_store_without_the_listing() -> None:
    client = _FakeS3()
    client.list_uploads_error = _client_error("NotImplemented", "ListMultipartUploads")
    archive = KudosArchive(client=client, bucket="archive")  # type: ignore[arg-type]
    with pytest.raises(KudosArchiveError, match="does not support ListMultipartUploads"):
        archive.abort_stale_multipart_uploads(older_than=timedelta(days=1))
    client.list_uploads_error = _client_error("AccessDenied", "ListMultipartUploads")
    with pytest.raises(KudosArchiveError, match="multipart upload listing"):
        archive.abort_stale_multipart_uploads(older_than=timedelta(days=1))


# Object storage

VALUES_SQL = (
    "SELECT * FROM (VALUES (1, 7, false, 10.00::numeric), (2, 7, true, 2.50::numeric), (3, 8, false, -1.25::numeric)) "
    "AS rows (id, user_id, escrow, amount)"
)


@pytest.fixture
def archive(object_store_ready: None) -> Iterator[KudosArchive]:
    """Return a client for the archive bucket, emptied before and after the test."""
    import os

    configured = KudosArchive.from_environment(os.environ)
    if configured is None:
        pytest.fail(f"The object-storage runtime did not set all of {', '.join(ARCHIVE_REQUIRED_ENV)}")
    _empty_bucket(configured)
    yield configured
    _empty_bucket(configured)


def _empty_bucket(archive: KudosArchive) -> None:
    client = archive._client
    paginator = client.get_paginator("list_objects_v2")
    keys = [entry["Key"] for page in paginator.paginate(Bucket=archive.bucket) for entry in page.get("Contents", [])]
    for start in range(0, len(keys), 1000):
        client.delete_objects(Bucket=archive.bucket, Delete={"Objects": [{"Key": key} for key in keys[start : start + 1000]]})


@pytest.fixture
def raw_connection(app: Flask) -> Iterator[object]:
    """Return a psycopg2 connection from the app engine, rolled back afterwards."""
    from horde.flask import db

    with app.app_context():
        pooled = db.engine.raw_connection()
        try:
            yield pooled.driver_connection
        finally:
            pooled.rollback()
            pooled.close()


def _put(archive: KudosArchive, connection: object, *, tier: ArchiveTier, source_range: str, copy_sql: str = VALUES_SQL) -> str:
    dataset = KudosRetentionDataset.CURRENCY if tier == ArchiveTier.CURRENCY else KudosRetentionDataset.STATISTICS
    manifest = archive.put_unit(tier=tier, dataset=dataset, source_range=source_range, copy_sql=copy_sql, connection=connection)
    key = archive.object_key(tier=tier, source_range=source_range)
    archive.put_manifest(key, manifest)
    return key


@pytest.mark.object_storage
def test_put_unit_manifest_and_verify_round_trip(archive: KudosArchive, raw_connection: object) -> None:
    key = _put(archive, raw_connection, tier=ArchiveTier.CURRENCY, source_range="kudos_ledger_p_20260803/20260803T000000_20260810T000000")
    manifest = archive.get_manifest(key)
    assert manifest.codec == "zstd"
    assert manifest.row_count == 3
    assert Decimal(manifest.amount_sum) == Decimal("11.25")
    assert manifest.columns == ["id", "user_id", "escrow", "amount"]
    assert manifest.column_types == [
        ArchiveColumn(name="id", postgres_type="integer"),
        ArchiveColumn(name="user_id", postgres_type="integer"),
        ArchiveColumn(name="escrow", postgres_type="boolean"),
        ArchiveColumn(name="amount", postgres_type="numeric"),
    ]
    assert manifest.per_user_totals == {"7": ["10.00", "2.50"], "8": ["-1.25", "0"]}
    assert (manifest.partition_name, manifest.unit_lower, manifest.unit_upper) == (
        "kudos_ledger_p_20260803",
        "20260803T000000",
        "20260810T000000",
    )
    report = archive.verify_unit(key, manifest)
    assert report.matches_manifest, report.mismatches

    tampered = ArchiveManifest.from_json(manifest.to_json().replace('"row_count": 3', '"row_count": 4'))
    assert archive.verify_unit(key, tampered).mismatches == ["row_count"]


@pytest.mark.object_storage
def test_put_unit_spans_multiple_parts(archive: KudosArchive, raw_connection: object) -> None:
    # Random hex does not compress much, so the stream crosses the multipart part size.
    copy_sql = (
        "SELECT g AS id, md5(g::text) || md5((g * 7)::text) || md5((g * 13)::text) AS filler, 1.00::numeric AS amount "
        "FROM generate_series(1, 250000) AS g"
    )
    key = _put(
        archive,
        raw_connection,
        tier=ArchiveTier.STATISTICS,
        source_range="kudos_stat_events_p_20260803/20260803T000000_20260810T000000",
        copy_sql=copy_sql,
    )
    manifest = archive.get_manifest(key)
    assert manifest.row_count == 250000
    assert Decimal(manifest.amount_sum) == Decimal(250000)
    assert manifest.compressed_bytes > 8 * 1024 * 1024
    assert archive.verify_unit(key, manifest).matches_manifest


@pytest.mark.object_storage
def test_tier_bytes_excludes_manifests_and_budget_evicts_oldest_statistics_only(archive: KudosArchive, raw_connection: object) -> None:
    currency_key = _put(
        archive, raw_connection, tier=ArchiveTier.CURRENCY, source_range="kudos_ledger_p_20200106/20200106T000000_20200113T000000"
    )
    newer = _put(
        archive, raw_connection, tier=ArchiveTier.STATISTICS, source_range="kudos_stat_events_p_20260810/20260810T000000_20260817T000000"
    )
    oldest = _put(archive, raw_connection, tier=ArchiveTier.STATISTICS, source_range="kudos_stat_events_p_legacy/min_20260803T000000")
    middle = _put(
        archive, raw_connection, tier=ArchiveTier.STATISTICS, source_range="kudos_stat_events_p_20260803/20260803T000000_20260810T000000"
    )
    sizes = {key: archive.get_manifest(key).compressed_bytes for key in (currency_key, newer, oldest, middle)}
    assert archive.tier_bytes(ArchiveTier.CURRENCY) == sizes[currency_key]
    assert archive.tier_bytes(ArchiveTier.STATISTICS) == sizes[newer] + sizes[oldest] + sizes[middle]

    # The budget covers the statistics tier alone, so the currency object does not count toward it.
    assert archive.enforce_budget(budget_bytes=sizes[newer]) == [oldest, middle]
    assert archive.tier_bytes(ArchiveTier.STATISTICS) == sizes[newer]
    with pytest.raises(KudosArchiveError):
        archive.get_manifest(oldest)

    # Currency objects are never evicted, whatever the budget.
    assert archive.enforce_budget(budget_bytes=1) == [newer]
    assert archive.tier_bytes(ArchiveTier.CURRENCY) == sizes[currency_key]
    assert archive.verify_unit(currency_key, archive.get_manifest(currency_key)).matches_manifest


@pytest.mark.object_storage
def test_restore_unit_into_a_scratch_table(archive: KudosArchive, raw_connection: object) -> None:
    key = _put(archive, raw_connection, tier=ArchiveTier.CURRENCY, source_range="kudos_ledger_p_20260803/20260803T000000_20260810T000000")
    table_name = f"kudos_restore_{uuid.uuid4().hex[:8]}"
    with raw_connection.cursor() as cursor:  # type: ignore[attr-defined]
        cursor.execute(f"CREATE TEMP TABLE {table_name} (id bigint, user_id integer, escrow boolean, amount numeric(20, 2))")
    assert archive.restore_unit(key, table_name=table_name, connection=raw_connection) == 3
    with raw_connection.cursor() as cursor:  # type: ignore[attr-defined]
        cursor.execute(f"SELECT user_id, escrow, amount FROM {table_name} ORDER BY id")
        assert cursor.fetchall() == [(7, False, Decimal("10.00")), (7, True, Decimal("2.50")), (8, False, Decimal("-1.25"))]


@pytest.mark.object_storage
def test_unreachable_store_raises_archive_error(raw_connection: object, object_store_ready: None) -> None:
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url="http://127.0.0.1:9",
        aws_access_key_id="x",
        aws_secret_access_key="y",
        region_name="garage",
        config=Config(connect_timeout=1, retries={"max_attempts": 1}),
    )
    unreachable = KudosArchive(client=client, bucket="kudos-archive")
    with pytest.raises(KudosArchiveError):
        unreachable.tier_bytes(ArchiveTier.CURRENCY)
    with pytest.raises(KudosArchiveError):
        _put(unreachable, raw_connection, tier=ArchiveTier.CURRENCY, source_range="kudos_ledger_p_20260803/20260803T000000_20260810T000000")


@pytest.mark.object_storage
def test_prefixed_archives_in_one_bucket_do_not_see_each_other(archive: KudosArchive, raw_connection: object) -> None:
    development = KudosArchive(client=archive._client, bucket=archive.bucket, prefix="dev")
    production = KudosArchive(client=archive._client, bucket=archive.bucket, prefix="prod/")
    development_key = _put(
        development, raw_connection, tier=ArchiveTier.STATISTICS, source_range="kudos_stat_events_p_legacy/min_20260803T000000"
    )
    production_key = _put(
        production, raw_connection, tier=ArchiveTier.STATISTICS, source_range="kudos_stat_events_p_legacy/min_20260803T000000"
    )
    production_currency = _put(
        production, raw_connection, tier=ArchiveTier.CURRENCY, source_range="kudos_ledger_p_legacy/min_20260803T000000"
    )
    assert development_key.startswith("dev/statistics/")
    assert production_key.startswith("prod/statistics/")
    development_size = development.get_manifest(development_key).compressed_bytes
    production_size = production.get_manifest(production_key).compressed_bytes
    assert development.tier_bytes(ArchiveTier.STATISTICS) == development_size
    assert development.tier_bytes(ArchiveTier.CURRENCY) == 0
    assert production.tier_bytes(ArchiveTier.STATISTICS) == production_size
    assert archive.tier_bytes(ArchiveTier.STATISTICS) == 0

    # Development evicts only its own dump, and production's currency object does not count toward its budget.
    assert development.enforce_budget(budget_bytes=0) == [development_key]
    assert development.tier_bytes(ArchiveTier.STATISTICS) == 0
    assert production.tier_bytes(ArchiveTier.STATISTICS) == production_size
    assert production.verify_unit(production_key, production.get_manifest(production_key)).matches_manifest
    assert production.verify_unit(production_currency, production.get_manifest(production_currency)).matches_manifest


@pytest.mark.object_storage
def test_restore_unit_refuses_a_truncated_stored_object(archive: KudosArchive, raw_connection: object) -> None:
    key = _put(archive, raw_connection, tier=ArchiveTier.CURRENCY, source_range="kudos_ledger_p_20260803/20260803T000000_20260810T000000")
    stored = archive._client.get_object(Bucket=archive.bucket, Key=key)["Body"].read()
    archive._client.put_object(Bucket=archive.bucket, Key=key, Body=stored[:-4])
    table_name = f"kudos_restore_{uuid.uuid4().hex[:8]}"
    with raw_connection.cursor() as cursor:  # type: ignore[attr-defined]
        cursor.execute(f"CREATE TEMP TABLE {table_name} (id bigint, user_id integer, escrow boolean, amount numeric(20, 2))")
    with pytest.raises(KudosArchiveError, match="does not match its manifest"):
        archive.restore_unit(key, table_name=table_name, connection=raw_connection)


@pytest.mark.object_storage
def test_object_size_and_stale_multipart_uploads_against_the_store(archive: KudosArchive, raw_connection: object) -> None:
    key = _put(archive, raw_connection, tier=ArchiveTier.CURRENCY, source_range="kudos_ledger_p_20260803/20260803T000000_20260810T000000")
    assert archive.object_size(key) == archive.get_manifest(key).compressed_bytes
    assert archive.object_size(f"{key}.absent") is None

    stale_key = archive.object_key(tier=ArchiveTier.STATISTICS, source_range="kudos_stat_events_p_20260803/20260803T000000_20260810T000000")
    upload_id = archive._client.create_multipart_upload(Bucket=archive.bucket, Key=stale_key)["UploadId"]
    try:
        assert archive.abort_stale_multipart_uploads(older_than=timedelta(hours=1)) == 0
        # A negative age puts the cutoff after now, so the upload just started counts as stale.
        assert archive.abort_stale_multipart_uploads(older_than=timedelta(minutes=-5)) == 1
        remaining = archive._client.list_multipart_uploads(Bucket=archive.bucket).get("Uploads", [])
        assert upload_id not in [upload["UploadId"] for upload in remaining]
    finally:
        with contextlib.suppress(ClientError):
            archive._client.abort_multipart_upload(Bucket=archive.bucket, Key=stale_key, UploadId=upload_id)


@pytest.mark.parametrize("limit_offset", [-1, 0, 1])
def test_upload_enforces_measured_bytes_including_final_flush(limit_offset: int) -> None:
    from horde.enums import KudosRetentionDataset
    from horde.kudos_archive import ArchiveBudgetExceeded
    from tests.unit.test_kudos_retention import _FakeObjectStore

    class CopySource:
        def cursor(self) -> CopySource:
            return self

        def __enter__(self) -> CopySource:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def copy_expert(self, sql: str, sink: Any) -> None:
            sink.write(RESTORE_CSV.encode())

        def execute(self, statement: str, parameters: object = None) -> None:
            return None

        def fetchall(self) -> list[tuple[str, str]]:
            return [("id", "bigint"), ("user_id", "integer"), ("escrow", "boolean"), ("amount", "numeric(20,2)")]

    store = _FakeObjectStore()
    archive = KudosArchive(client=store, bucket="test")
    kwargs = dict(
        tier=ArchiveTier.CURRENCY,
        dataset=KudosRetentionDataset.CURRENCY,
        source_range="kudos_ledger_p_test/min_20200106T000000",
        copy_sql="SELECT ignored",
        connection=CopySource(),
    )
    manifest = archive.put_unit(**kwargs)
    key = archive.object_key(tier=kwargs["tier"], source_range=kwargs["source_range"])
    previous = store.objects[key]
    if limit_offset < 0:
        with pytest.raises(ArchiveBudgetExceeded):
            archive.put_unit(**kwargs, max_compressed_bytes=manifest.compressed_bytes + limit_offset)
    else:
        assert archive.put_unit(**kwargs, max_compressed_bytes=manifest.compressed_bytes + limit_offset).compressed_bytes == len(previous)
    assert store.objects[key] == previous
    assert store.uploads == {}


# Format version fixtures
#
# Each format version keeps one stored object and manifest exactly as that version wrote them, so a change to the
# manifest or to restore that strands older objects fails here.

FIXTURE_OBJECT = base64.b64decode(
    "KLUv/QRgfQYA8k4vI0CJOAe7GJiK5q00yKyJ7EJj7pWyEjIp+t9+UgCtDRFFDWpcyTuB++q8BjK7sIsa02ch1hwzLFpgw9RhZg8v38/G4N7OuWQtrPWU61oKYi3u"
    "SVpLYZ0NtBH/fqYh26M/VwwIFAAQJBSJgLdktw1m9LyFceqBNQ1k2GO2MYbUwRY5z0RkIloqSzIomePSvSWMqM1LQb5x8aaDL0cGRelbekwNPWSqoKNix+NMRXoR"
    "qVrib1y0xAj85Vau4DcGBQBAlgAdPodzhGry7JpkcDReCQ==",
)
"""A zstd currency object of two ``kudos_ledger`` rows, one with a quoted JSON detail holding a comma."""
FIXTURE_MANIFEST_VERSION_1 = (
    '{"amount_sum": "12.50", "codec": "zstd", "columns": ["id", "created", "event_id", "entry_type", "user_id", "escrow", "amount", '
    '"applied", "job_id", "wp_type", "detail"], "compressed_bytes": 220, '
    '"compressed_sha256": "1757a5e67d6616432db8087eec10a5ccb7e103ce259542873aa6b7e78dd041bd", "dataset": "currency", '
    '"partition_name": "kudos_ledger_p_20260803", "per_user_totals": {"7": ["10.00", "2.50"]}, "row_count": 2, "schema_version": 1, '
    '"unit_lower": "20260803T000000", "unit_upper": "20260810T000000"}'
)
"""The fixture object's manifest at format version 1, which records column names without types."""
FIXTURE_MANIFEST_VERSION_2 = (
    '{"amount_sum": "12.50", "codec": "zstd", "column_types": [{"name": "id", "type": "bigint"}, '
    '{"name": "created", "type": "timestamp without time zone"}, {"name": "event_id", "type": "uuid"}, '
    '{"name": "entry_type", "type": "character varying(32)"}, {"name": "user_id", "type": "integer"}, '
    '{"name": "escrow", "type": "boolean"}, {"name": "amount", "type": "numeric(20,2)"}, {"name": "applied", "type": "boolean"}, '
    '{"name": "job_id", "type": "uuid"}, {"name": "wp_type", "type": "character varying(20)"}, {"name": "detail", "type": "jsonb"}], '
    '"columns": ["id", "created", "event_id", "entry_type", "user_id", "escrow", "amount", "applied", "job_id", "wp_type", "detail"], '
    '"compressed_bytes": 220, "compressed_sha256": "1757a5e67d6616432db8087eec10a5ccb7e103ce259542873aa6b7e78dd041bd", '
    '"dataset": "currency", "partition_name": "kudos_ledger_p_20260803", "per_user_totals": {"7": ["10.00", "2.50"]}, "row_count": 2, '
    '"schema_version": 2, "unit_lower": "20260803T000000", "unit_upper": "20260810T000000"}'
)
"""The fixture object's manifest at format version 2, which records each column's PostgreSQL type."""


def _fixture_archive(manifest_json: str) -> KudosArchive:
    client = _FakeS3()
    client.objects[RESTORE_KEY] = FIXTURE_OBJECT
    client.objects[manifest_key(RESTORE_KEY)] = manifest_json.encode("utf-8")
    return KudosArchive(client=client, bucket="archive")  # type: ignore[arg-type]


def test_version_1_fixture_verifies_and_restore_refuses_it(app: Flask) -> None:
    from horde.database.kudos_retention import restore_archive_unit

    archive = _fixture_archive(FIXTURE_MANIFEST_VERSION_1)
    manifest = archive.get_manifest(RESTORE_KEY)
    assert (manifest.schema_version, manifest.column_types) == (1, None)
    assert archive.verify_unit(RESTORE_KEY, manifest).matches_manifest
    table_name = f"kudos_restore_{uuid.uuid4().hex[:8]}"
    with app.app_context(), pytest.raises(KudosArchiveError, match="format version 1 manifest, which predates recorded column types"):
        restore_archive_unit(archive, RESTORE_KEY, table_name=table_name)


def test_version_2_fixture_restores_from_its_recorded_column_types(app: Flask) -> None:
    from sqlalchemy import text

    from horde.database.kudos_retention import restore_archive_unit
    from horde.flask import db

    archive = _fixture_archive(FIXTURE_MANIFEST_VERSION_2)
    manifest = archive.get_manifest(RESTORE_KEY)
    assert manifest.column_types is not None
    assert archive.verify_unit(RESTORE_KEY, manifest).matches_manifest
    table_name = f"kudos_restore_{uuid.uuid4().hex[:8]}"
    with app.app_context():
        try:
            assert restore_archive_unit(archive, RESTORE_KEY, table_name=table_name) == 2
            described = db.session.execute(
                text(
                    "SELECT attname, format_type(atttypid, atttypmod) FROM pg_attribute"
                    " WHERE attrelid = CAST(:table_name AS regclass) AND attnum > 0 AND NOT attisdropped ORDER BY attnum",
                ),
                {"table_name": table_name},
            ).all()
            assert [tuple(row) for row in described] == [(column.name, column.postgres_type) for column in manifest.column_types]
            restored = db.session.execute(
                text(f"SELECT user_id, escrow, amount, CAST(job_id AS text), detail FROM {table_name} ORDER BY id"),
            ).all()
            assert [tuple(row) for row in restored] == [
                (7, False, Decimal("10.00"), None, {"note": "a, b"}),
                (7, True, Decimal("2.50"), "0b9d4e6a-5c3f-4e21-8d7a-1f2e3d4c5b6a", None),
            ]
        finally:
            db.session.rollback()
            db.session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
            db.session.commit()


def test_restore_refuses_a_manifest_column_type_that_is_not_a_built_in_type() -> None:
    from horde.database.kudos_retention import restore_archive_unit

    tampered = FIXTURE_MANIFEST_VERSION_2.replace('"type": "jsonb"', '"type": "jsonb); DROP TABLE users; --"')
    with pytest.raises(ValueError, match="unexpected column type"):
        restore_archive_unit(_fixture_archive(tampered), RESTORE_KEY, table_name="kudos_restore_refused")
