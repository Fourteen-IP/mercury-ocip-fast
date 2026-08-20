"""Tests for the exception hierarchy in ``exceptions``."""

from mercury_ocip_fast.exceptions import (
    MError,
    MErrorHttpStatus,
    MErrorMalformedResponse,
    MErrorPoolClosed,
    MErrorResponse,
    MErrorTransport,
)


def test_default_message():
    assert MError().message == "An error occurred in mercury-ocip-fast"


def test_str_includes_class_and_message():
    err = MErrorResponse("bad login")
    assert str(err) == "MErrorResponse(bad login)"


def test_is_an_exception():
    assert isinstance(MError(), Exception)


def test_transport_hierarchy():
    # The malformed-response error is a transport error.
    assert issubclass(MErrorMalformedResponse, MErrorTransport)
    assert issubclass(MErrorTransport, MError)


def test_pool_closed_is_merror():
    assert issubclass(MErrorPoolClosed, MError)


def test_http_status_carries_status():
    err = MErrorHttpStatus("bad", status=503)
    assert err.status == 503
