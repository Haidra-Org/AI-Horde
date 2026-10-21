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

from unittest.mock import Mock

import pytest

from horde import r2


@pytest.mark.parametrize("read_fails", [False, True])
def test_chat_download_closes_body_even_when_read_fails(monkeypatch: pytest.MonkeyPatch, read_fails: bool) -> None:
    body = Mock()
    body.read.return_value = b"jpeg-bytes"
    client = Mock()
    client.get_object.return_value = {"Body": body}
    monkeypatch.setattr(r2, "s3_client", client)
    if read_fails:
        body.read.side_effect = OSError("read failed")
        with pytest.raises(OSError, match="read failed"):
            r2.download_chat_image("chat/test.jpg")
    else:
        assert r2.download_chat_image("chat/test.jpg") == b"jpeg-bytes"
    body.close.assert_called_once_with()
    client.get_object.assert_called_once_with(Bucket=r2.r2_source_image_bucket, Key="chat/test.jpg")


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


@pytest.fixture
def separate_chat_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the offloaded messages a bucket apart from the source images, as a deployment may."""
    monkeypatch.setattr(r2, "r2_source_image_bucket", "source-images")
    monkeypatch.setattr(r2, "r2_chat_messages_bucket", "chat-messages")


class TestChatObjectKeys:
    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("req_chat_messages.json", True),
            ("req_chat_0.jpg", False),
            ("req_chat_messages.json.jpg", False),
        ],
    )
    def test_a_key_is_a_messages_key_only_with_the_messages_suffix(self, key: str, expected: bool) -> None:
        assert r2.is_chat_messages_key(key) is expected


class TestChatImages:
    def test_a_download_url_is_signed_for_the_requested_lifetime(self, fake_clients) -> None:
        transient, _ = fake_clients

        r2.generate_chat_image_download_url("req_chat_0.jpg", expires_in=900)

        (call,) = transient.calls
        assert call == {
            "method": "get_object",
            "params": {"Bucket": r2.r2_source_image_bucket, "Key": "req_chat_0.jpg"},
            "expires": 900,
        }

    def test_bulk_delete_batches_keys_and_reports_only_real_failures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = Mock()
        client.delete_objects.side_effect = [
            {"Errors": [{"Key": "k0000", "Code": "NoSuchKey"}, {"Key": "k0001", "Code": "AccessDenied"}]},
            {},
        ]
        monkeypatch.setattr(r2, "s3_client", client)
        keys = [f"k{index:04d}" for index in range(r2.CHAT_IMAGE_DELETE_BATCH_SIZE + 1)]

        assert r2.delete_chat_images(keys) == {"k0001"}
        first_batch, second_batch = (call.kwargs["Delete"]["Objects"] for call in client.delete_objects.call_args_list)
        assert len(first_batch) == r2.CHAT_IMAGE_DELETE_BATCH_SIZE
        assert second_batch == [{"Key": keys[-1]}]

    @pytest.mark.usefixtures("separate_chat_buckets")
    def test_a_download_url_names_the_bucket_of_its_key(self, fake_clients) -> None:
        transient, _ = fake_clients

        r2.generate_chat_image_download_url("req_chat_0.jpg", expires_in=900)
        r2.generate_chat_image_download_url("req_chat_messages.json", expires_in=900)

        assert [call["params"] for call in transient.calls] == [
            {"Bucket": "source-images", "Key": "req_chat_0.jpg"},
            {"Bucket": "chat-messages", "Key": "req_chat_messages.json"},
        ]

    @pytest.mark.usefixtures("separate_chat_buckets")
    def test_a_mixed_bulk_delete_deletes_each_key_from_its_bucket_and_reports_failures(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = Mock()
        client.delete_objects.side_effect = [
            {"Errors": [{"Key": "req_chat_messages.json", "Code": "AccessDenied"}]},
            {"Errors": [{"Key": "req_chat_0.jpg", "Code": "NoSuchKey"}, {"Key": "req_chat_1.jpg", "Code": "InternalError"}]},
        ]
        monkeypatch.setattr(r2, "s3_client", client)

        failed_keys = r2.delete_chat_images(["req_chat_1.jpg", "req_chat_messages.json", "req_chat_0.jpg"])

        assert failed_keys == {"req_chat_messages.json", "req_chat_1.jpg"}
        assert [(call.kwargs["Bucket"], call.kwargs["Delete"]["Objects"]) for call in client.delete_objects.call_args_list] == [
            ("chat-messages", [{"Key": "req_chat_messages.json"}]),
            ("source-images", [{"Key": "req_chat_0.jpg"}, {"Key": "req_chat_1.jpg"}]),
        ]


class TestChatMessages:
    @pytest.mark.usefixtures("separate_chat_buckets")
    @pytest.mark.parametrize("gzipped", [True, False])
    def test_offloaded_messages_are_stored_as_json_that_is_not_transformed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        gzipped: bool,
    ) -> None:
        client = Mock()
        monkeypatch.setattr(r2, "s3_client", client)

        r2.upload_chat_messages(b"body", "req_chat_messages.json", gzipped=gzipped)

        expected_object = {
            "Bucket": "chat-messages",
            "Key": "req_chat_messages.json",
            "Body": b"body",
            "ContentType": "application/json",
            "CacheControl": "no-transform",
        }
        if gzipped:
            expected_object["ContentEncoding"] = "gzip"
        client.put_object.assert_called_once_with(**expected_object)
