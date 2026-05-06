# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import datetime

from horde.utils import (
    ConvertAmount,
    count_digits,
    count_parentheses,
    datetime_parser,
    does_extra_text_reference_exist,
    generate_api_key,
    generate_client_id,
    hash_api_key,
    hash_dictionary,
    is_profane,
    sanitize_string,
    validate_regex,
)


class TestCountDigits:
    def test_single_digit(self):
        assert count_digits(1) == 1
        assert count_digits(9) == 1

    def test_multi_digit(self):
        # count_digits uses `> 10` (strict), so 10 itself counts as 1
        assert count_digits(10) == 1
        assert count_digits(11) == 2
        assert count_digits(100) == 2
        assert count_digits(101) == 3
        assert count_digits(1000) == 3
        assert count_digits(1001) == 4

    def test_large_numbers(self):
        assert count_digits(1_000_001) == 7
        assert count_digits(1_000_000_001) == 10


class TestConvertAmount:
    def test_small_amounts(self):
        ca = ConvertAmount(500)
        assert ca.char == ""
        assert ca.prefix == ""
        assert ca.amount == 500

    def test_kilo(self):
        ca = ConvertAmount(5000)
        assert ca.char == "K"
        assert ca.prefix == "kilo"
        assert ca.amount == 5.0

    def test_mega(self):
        ca = ConvertAmount(5_000_000)
        assert ca.char == "M"
        assert ca.prefix == "mega"
        assert ca.amount == 5.0

    def test_giga(self):
        ca = ConvertAmount(5_000_000_000)
        assert ca.char == "G"
        assert ca.prefix == "giga"
        assert ca.amount == 5.0

    def test_tera(self):
        ca = ConvertAmount(5_000_000_000_000)
        assert ca.char == "T"
        assert ca.prefix == "tera"

    def test_peta(self):
        ca = ConvertAmount(5_000_000_000_000_000)
        assert ca.char == "P"
        assert ca.prefix == "peta"


class TestSanitizeString:
    def test_strips_html(self):
        assert sanitize_string("<script>alert('xss')</script>hello") == "&lt;script&gt;alert('xss')&lt;/script&gt;hello"

    def test_strips_whitespace(self):
        assert sanitize_string("  hello  ") == "hello"

    def test_plain_text_unchanged(self):
        assert sanitize_string("hello world") == "hello world"


class TestHashFunctions:
    def test_hash_api_key_deterministic(self):
        result1 = hash_api_key("test_key")
        result2 = hash_api_key("test_key")
        assert result1 == result2

    def test_hash_api_key_different_inputs(self):
        assert hash_api_key("key1") != hash_api_key("key2")

    def test_hash_dictionary_deterministic(self):
        d = {"a": 1, "b": 2}
        assert hash_dictionary(d) == hash_dictionary(d)

    def test_hash_dictionary_order_independent(self):
        d1 = {"a": 1, "b": 2}
        d2 = {"b": 2, "a": 1}
        assert hash_dictionary(d1) == hash_dictionary(d2)

    def test_hash_dictionary_different_inputs(self):
        assert hash_dictionary({"a": 1}) != hash_dictionary({"a": 2})


class TestCountParentheses:
    def test_no_parentheses(self):
        assert count_parentheses("hello") == 0

    def test_single_pair(self):
        assert count_parentheses("(hello)") == 1

    def test_multiple_pairs(self):
        assert count_parentheses("(a)(b)(c)") == 3

    def test_nested(self):
        # Uses a boolean flag, not a stack — only counts outermost pairs
        assert count_parentheses("((a))") == 1

    def test_unmatched_open(self):
        assert count_parentheses("(hello") == 0

    def test_unmatched_close(self):
        assert count_parentheses("hello)") == 0


class TestValidateRegex:
    def test_valid_regex(self):
        assert validate_regex(r"\d+") is True
        assert validate_regex(r"^hello$") is True

    def test_invalid_regex(self):
        assert validate_regex(r"[invalid") is False

    def test_empty_regex(self):
        assert validate_regex("") is True


class TestDoesExtraTextReferenceExist:
    def test_found(self):
        extra_texts = [{"reference": "ref1"}, {"reference": "ref2"}]
        assert does_extra_text_reference_exist(extra_texts, "ref1") is True

    def test_not_found(self):
        extra_texts = [{"reference": "ref1"}]
        assert does_extra_text_reference_exist(extra_texts, "ref_missing") is False

    def test_empty_list(self):
        assert does_extra_text_reference_exist([], "ref1") is False


class TestDatetimeParser:
    def test_iso_datetime_converted(self):
        result = datetime_parser({"created": "2024-01-15T10:30:00"})
        assert isinstance(result["created"], datetime)
        assert result["created"].year == 2024
        assert result["created"].month == 1
        assert result["created"].day == 15

    def test_non_datetime_unchanged(self):
        result = datetime_parser({"name": "hello", "count": "42"})
        assert result["name"] == "hello"
        assert result["count"] == "42"

    def test_mixed_fields(self):
        result = datetime_parser({"created": "2024-01-15T10:30:00", "name": "test"})
        assert isinstance(result["created"], datetime)
        assert result["name"] == "test"


class TestGenerateKeys:
    def test_generate_client_id_format(self):
        cid = generate_client_id()
        assert isinstance(cid, str)
        assert len(cid) > 10

    def test_generate_client_id_unique(self):
        ids = {generate_client_id() for _ in range(10)}
        assert len(ids) == 10

    def test_generate_api_key_format(self):
        key = generate_api_key()
        assert isinstance(key, str)
        assert len(key) > 10

    def test_generate_api_key_unique(self):
        keys = {generate_api_key() for _ in range(10)}
        assert len(keys) == 10


class TestIsProfane:
    def test_clean_text(self):
        assert is_profane("hello world") is False

    def test_profane_text(self):
        assert is_profane("fuck you") is True
