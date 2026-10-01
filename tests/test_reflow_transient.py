"""reflow_handler._is_transient / _is_transient_message: a network-layer
failure (DNS, reset, unreachable, TLS, token-refresh transport) is the
network's fault, not the owner's, and must never spend a give-up attempt.
2026-10: a DNS outage during an attended drain counted
"Unable to find the server at oauth2.googleapis.com" as permanent and stamped
320 owners gave_up."""
import errno
import socket
import ssl

import httplib2
import pytest
from google.auth import exceptions as gae
from googleapiclient.errors import HttpError

from mcpbrain.sync import reflow_handler as rh


def _http(status):
    return HttpError(httplib2.Response({"status": status}), b"x")


def _wrapped(inner, outer_cls=RuntimeError, how="cause"):
    try:
        try:
            raise inner
        except BaseException as e:
            if how == "cause":
                raise outer_cls("wrapped") from e
            raise outer_cls("wrapped")          # __context__ only
    except BaseException as outer:
        return outer


@pytest.mark.parametrize("exc", [
    httplib2.ServerNotFoundError("Unable to find the server at oauth2.googleapis.com"),
    httplib2.error.HttpLib2Error("socket"),
    gae.TransportError(httplib2.ServerNotFoundError("Unable to find the server at x")),
    gae.TransportError("plain transport"),
    gae.TimeoutError("token timeout"),
    gae.RefreshError("retry later", retryable=True),
    gae.RefreshError(gae.TransportError("dns")),
    socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided"),
    ssl.SSLError("bad record mac"),
    ConnectionResetError(errno.ECONNRESET, "Connection reset by peer"),
    ConnectionRefusedError(errno.ECONNREFUSED, "refused"),
    TimeoutError("timed out"),
    socket.timeout("timed out"),
    *[OSError(n, "net") for n in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ECONNRESET,
                                  errno.ECONNREFUSED, errno.ETIMEDOUT, errno.ENETDOWN)],
    _http(429), _http(503),
])
def test_network_failures_are_transient(exc):
    assert rh._is_transient(exc)


@pytest.mark.parametrize("how", ["cause", "context"])
def test_a_wrapped_network_error_is_still_transient(how):
    assert rh._is_transient(_wrapped(
        httplib2.ServerNotFoundError("Unable to find the server"), how=how))
    assert rh._is_transient(_wrapped(socket.gaierror(8, "x"), gae.RefreshError, how=how))


def test_refresh_error_with_a_transport_cause_is_transient():
    assert rh._is_transient(_wrapped(gae.TransportError("x"), gae.RefreshError))


@pytest.mark.parametrize("exc", [
    ValueError("bad"), RuntimeError("partial re-extraction"),
    gae.RefreshError("invalid_grant: Token has been expired or revoked."),
    FileNotFoundError(errno.ENOENT, "nope"), OSError(errno.ENOSPC, "disk full"),
    _http(500), _http(404), _http(403),
    _wrapped(ValueError("inner"), how="cause"),
])
def test_non_network_failures_stay_permanent(exc):
    assert not rh._is_transient(exc)


def test_the_cause_walk_is_bounded_and_survives_a_cycle():
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__cause__, b.__cause__ = b, a
    assert not rh._is_transient(a)


@pytest.mark.parametrize("text", [
    "Unable to find the server at oauth2.googleapis.com",
    "[Errno -2] Name or service not known",
    "[Errno 8] nodename nor servname provided, or not known",
    "[Errno -3] Temporary failure in name resolution",
    "ConnectionResetError: [Errno 54] Connection reset by peer",
    "The read operation timed out",
    "[Errno 51] Network is unreachable",
    "TransportError: HTTPSConnectionPool(...)",
])
def test_stored_network_messages_are_recognised(text):
    assert rh._is_transient_message(text)


@pytest.mark.parametrize("text", [
    "", None, "RuntimeError: reflow drive F: partial re-extraction",
    "HttpError 404 File not found", "ValueError: plan would drop a lineage",
])
def test_other_stored_messages_are_not(text):
    assert not rh._is_transient_message(text)
