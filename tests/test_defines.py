"""Tests for the string and helper functions in ``utils/defines``."""

import re

import pytest

from mercury_ocip_fast.utils.defines import (
    expand_phone_range,
    generate_secure,
    highest_version_for,
    is_boolean,
    is_camel_case,
    is_none,
    is_snake_case,
    normalise_phone_number,
    parse_version,
    snake_to_camel,
    str_to_bool,
    to_snake_case,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("CamelCase", "camel_case"),
        ("Some Name Here", "some_name_here"),
        ("XMLParser", "xml_parser"),
        ("userId", "user_id"),
        ("already_snake", "already_snake"),
    ],
)
def test_to_snake_case(value, expected):
    assert to_snake_case(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("user_id", "userId"),
        ("service_provider_id", "serviceProviderId"),
        ("single", "single"),
    ],
)
def test_snake_to_camel(value, expected):
    assert snake_to_camel(value) == expected


def test_snake_camel_round_trip():
    # The two functions are inverse for simple snake_case names.
    assert to_snake_case(snake_to_camel("service_provider_id")) == "service_provider_id"


def test_is_snake_case():
    assert is_snake_case("user_id")
    assert not is_snake_case("userId")


def test_is_camel_case():
    assert is_camel_case("userId")
    assert not is_camel_case("user_id")


@pytest.mark.parametrize(
    ("value", "expected"), [("true", True), ("FALSE", True), ("yes", False)]
)
def test_is_boolean(value, expected):
    assert is_boolean(value) is expected


def test_str_to_bool():
    assert str_to_bool("True") is True
    assert str_to_bool("false") is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [("none", True), ("None", True), ("  ", True), ("", True), ("x", False)],
)
def test_is_none(value, expected):
    assert is_none(value) is expected


def test_is_none_rejects_non_string():
    assert is_none(5) is False  # type: ignore[arg-type]


def test_normalise_phone_number_strips_quotes():
    assert normalise_phone_number('"+1-4072383011"') == "+1-4072383011"
    assert normalise_phone_number("  '+1-4072383011'  ") == "+1-4072383011"
    assert normalise_phone_number("") == ""


def test_expand_phone_range():
    result = expand_phone_range("+1-4072383011 - +1-4072383013")
    assert result == ["+1-4072383011", "+1-4072383012", "+1-4072383013"]


def test_expand_phone_range_no_range():
    assert expand_phone_range("+1-4072383011") == ["+1-4072383011"]


class TestGenerateSecure:
    def test_length(self):
        assert len(generate_secure(16)) == 16

    def test_meets_categories(self):
        password = generate_secure(12)
        assert re.search(r"[a-z]", password)
        assert re.search(r"[A-Z]", password)
        assert re.search(r"[0-9]", password)
        assert re.search(r"[!@#$%&*\-_=+]", password)

    def test_rejects_short_length(self):
        with pytest.raises(ValueError):
            generate_secure(4)


class TestParseVersion:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("UserGetRequest22", ("UserGetRequest", 22, 0, 0)),
            ("UserGetRequest21sp1", ("UserGetRequest", 21, 1, 0)),
            ("Foo12sp3V2", ("Foo", 12, 3, 2)),
            ("GroupGetRequest", ("GroupGetRequest", 0, 0, 0)),
        ],
    )
    def test_parse(self, name, expected):
        assert parse_version(name) == expected

    def test_invalid(self):
        with pytest.raises(ValueError):
            parse_version("Bad-Name!")


def test_highest_version_for():
    names = {
        "UserGetRequest14",
        "UserGetRequest21sp1",
        "UserGetRequest22",
        "GroupGetRequest",
    }
    assert highest_version_for("UserGetRequest", names) == "UserGetRequest22"


def test_highest_version_for_no_match():
    assert highest_version_for("MissingRequest", {"UserGetRequest22"}) is None
