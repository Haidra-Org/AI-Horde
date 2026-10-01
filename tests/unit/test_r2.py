# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit coverage for the pure URL/key construction and account routing in
``horde.r2``.

These functions decide *which* S3 account/bucket a request lands in and *what*
object key it targets. Getting either wrong silently misroutes user images.
Live bucket I/O stays in the ``object_storage``-marked integration tests; here we
substitute a recording fake client and assert the request shape.
"""

from __future__ import annotations

import pytest

from horde import r2


class _FakeS3:
    """Records ``generate_presigned_url`` calls and returns a sentinel URL."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[dict] = []

    def generate_presigned_url(self, ClientMethod, Params, ExpiresIn):  # noqa: N803 (boto3 kwarg names)
        self.calls.append({"method": ClientMethod, "params": Params, "expires": ExpiresIn})
        return f"https://{self.name}/{Params['Bucket']}/{Params['Key']}?method={ClientMethod}"


@pytest.fixture
def fake_clients(monkeypatch):
    transient = _FakeS3("transient")
    shared = _FakeS3("shared")
    monkeypatch.setattr(r2, "s3_client", transient)
    monkeypatch.setattr(r2, "s3_client_shared", shared)
    return transient, shared


class TestProcgenUrls:
    def test_upload_url_default_routes_to_transient(self, fake_clients):
        transient, shared = fake_clients
        r2.generate_procgen_upload_url("abc123")
        assert len(transient.calls) == 1
        assert not shared.calls
        call = transient.calls[0]
        assert call["method"] == "put_object"
        assert call["params"]["Key"] == "abc123.webp"
        assert call["params"]["Bucket"] == r2.r2_transient_bucket
        assert call["expires"] == 1800

    def test_upload_url_shared_routes_to_shared_client(self, fake_clients):
        transient, shared = fake_clients
        r2.generate_procgen_upload_url("abc123", shared=True)
        assert len(shared.calls) == 1
        assert not transient.calls

    def test_download_url_uses_get_object(self, fake_clients):
        transient, _ = fake_clients
        r2.generate_procgen_download_url("def456")
        assert transient.calls[0]["method"] == "get_object"
        assert transient.calls[0]["params"]["Key"] == "def456.webp"


class TestUuidImgUrls:
    def test_uuid_upload_url_builds_typed_key(self, fake_clients):
        transient, _ = fake_clients
        r2.generate_uuid_img_upload_url("uuid-1", "webp")
        assert transient.calls[0]["method"] == "put_object"
        assert transient.calls[0]["params"]["Key"] == "uuid-1.webp"

    def test_uuid_download_url_builds_typed_key(self, fake_clients):
        transient, _ = fake_clients
        r2.generate_uuid_img_download_url("uuid-2", "png")
        assert transient.calls[0]["method"] == "get_object"
        assert transient.calls[0]["params"]["Key"] == "uuid-2.png"


class TestImgUrls:
    def test_download_url_honours_explicit_bucket(self, fake_clients):
        transient, _ = fake_clients
        r2.generate_img_download_url("file.webp", bucket="some-bucket")
        assert transient.calls[0]["params"]["Bucket"] == "some-bucket"
        assert transient.calls[0]["method"] == "get_object"

    def test_upload_url_defaults_to_transient_bucket(self, fake_clients):
        transient, _ = fake_clients
        r2.generate_img_upload_url("file.webp")
        assert transient.calls[0]["params"]["Bucket"] == r2.r2_transient_bucket
        assert transient.calls[0]["method"] == "put_object"


class TestFileExistence:
    def test_check_file_returns_false_on_404(self, fake_clients):
        from botocore.exceptions import ClientError

        class _Missing:
            def head_object(self, Bucket, Key):  # noqa: N803
                raise ClientError({"Error": {"Code": "404"}}, "HeadObject")

        # check_file returns (code != 404) on ClientError -> False for a 404.
        assert r2.check_file(_Missing(), "bucket", "missing.webp") is False
        # file_exists treats any non-dict (here a bool) as "absent".
        assert r2.file_exists(_Missing(), "bucket", "missing.webp") is False


class TestEvidenceText:
    @pytest.fixture
    def evidence_store(self, monkeypatch) -> _FakeS3:
        store = _FakeS3("evidence")
        monkeypatch.setattr(r2, "evidence_client", store)
        monkeypatch.setattr(r2, "r2_evidence_bucket", "evidence-bucket")
        return store

    @pytest.mark.parametrize("event_id", [7, "7"])
    def test_key_is_the_event_id_under_the_evidence_prefix(self, event_id) -> None:
        assert r2.evidence_object_key(event_id) == f"evidence/{int(event_id)}.json"

    def test_key_refuses_anything_but_an_integer(self) -> None:
        with pytest.raises(ValueError):
            r2.evidence_object_key("../prompts/x")

    def test_text_url_is_a_short_private_json_read_of_the_event_key(self, evidence_store) -> None:
        url = r2.evidence_text_url(42)

        assert url == "https://evidence/evidence-bucket/evidence/42.json?method=get_object"
        (call,) = evidence_store.calls
        assert call["expires"] == r2.EVIDENCE_TEXT_URL_SECONDS == 600
        assert call["params"] == {
            "Bucket": "evidence-bucket",
            "Key": "evidence/42.json",
            "ResponseCacheControl": "private, no-store",
            "ResponseContentType": "application/json; charset=utf-8",
        }

    def test_no_text_url_without_an_evidence_store(self, monkeypatch) -> None:
        monkeypatch.setattr(r2, "evidence_client", None)

        assert r2.evidence_text_url(42) is None


class TestEvidenceClientConfiguration:
    COMPLETE: dict[str, str] = {
        r2.EVIDENCE_ACCOUNT_ENV: "https://evidence.invalid",
        r2.EVIDENCE_BUCKET_ENV: "evidence-bucket",
        r2.EVIDENCE_ACCESS_KEY_ID_ENV: "key-id",
        r2.EVIDENCE_SECRET_ACCESS_KEY_ENV: "secret",
    }
    """A configuration with every variable the evidence client requires."""

    @pytest.fixture
    def warnings(self, monkeypatch) -> list[str]:
        logged: list[str] = []

        class _Logger:
            def warning(self, message: str) -> None:
                logged.append(message)

        monkeypatch.setattr(r2, "logger", _Logger())
        return logged

    def test_complete_configuration_builds_a_client(self, warnings) -> None:
        client = r2.build_evidence_client(self.COMPLETE)

        assert client is not None
        assert client.meta.endpoint_url == "https://evidence.invalid"
        assert warnings == []

    @pytest.mark.parametrize("unset", [r2.EVIDENCE_ACCESS_KEY_ID_ENV, r2.EVIDENCE_SECRET_ACCESS_KEY_ENV])
    def test_missing_credentials_build_no_client(self, warnings, unset: str) -> None:
        """Without its scoped token the client would sign with the default credential chain, so none is built."""
        environ = {variable: value for variable, value in self.COMPLETE.items() if variable != unset}

        assert r2.build_evidence_client(environ) is None
        (warning,) = warnings
        assert unset in warning
        assert r2.EVIDENCE_ACCOUNT_ENV not in warning
