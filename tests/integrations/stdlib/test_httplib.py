import platform
import sys
import random
import responses
import pytest

try:
    # py3
    from urllib.request import urlopen
except ImportError:
    # py2
    from urllib import urlopen

try:
    # py2
    from httplib import HTTPConnection, HTTPSConnection
except ImportError:
    # py3
    from http.client import HTTPConnection, HTTPSConnection

try:
    from unittest import mock  # python 3.3 and above
except ImportError:
    import mock  # python < 3.3

from sentry_sdk import capture_message, start_transaction
from sentry_sdk.tracing import Transaction
from sentry_sdk.integrations.stdlib import StdlibIntegration


def test_crumb_capture(sentry_init, capture_events):
    sentry_init(integrations=[StdlibIntegration()])

    url = "http://example.com/"
    responses.add(responses.GET, url, status=200)

    events = capture_events()

    response = urlopen(url)
    assert response.getcode() == 200
    capture_message("Testing!")

    (event,) = events
    (crumb,) = event["breadcrumbs"]["values"]
    assert crumb["type"] == "http"
    assert crumb["category"] == "httplib"
    assert crumb["data"] == {
        "url": url,
        "method": "GET",
        "status_code": 200,
        "reason": "OK",
        "http.fragment": "",
        "http.query": "",
    }


def test_crumb_capture_hint(sentry_init, capture_events):
    def before_breadcrumb(crumb, hint):
        crumb["data"]["extra"] = "foo"
        return crumb

    sentry_init(integrations=[StdlibIntegration()], before_breadcrumb=before_breadcrumb)

    url = "http://example.com/"
    responses.add(responses.GET, url, status=200)

    events = capture_events()

    response = urlopen(url)
    assert response.getcode() == 200
    capture_message("Testing!")

    (event,) = events
    (crumb,) = event["breadcrumbs"]["values"]
    assert crumb["type"] == "http"
    assert crumb["category"] == "httplib"
    assert crumb["data"] == {
        "url": url,
        "method": "GET",
        "status_code": 200,
        "reason": "OK",
        "extra": "foo",
        "http.fragment": "",
        "http.query": "",
    }

    if platform.python_implementation() != "PyPy":
        assert sys.getrefcount(response) == 2


def test_empty_realurl(sentry_init, capture_events):
    """
    Ensure that after using sentry_sdk.init you can putrequest a
    None url.
    """

    sentry_init(dsn="")
    HTTPConnection("example.com", port=443).putrequest("POST", None)


def test_httplib_misuse(sentry_init, capture_events, request):
    """HTTPConnection.getresponse must be called after every call to
    HTTPConnection.request. However, if somebody does not abide by
    this contract, we still should handle this gracefully and not
    send mixed breadcrumbs.

    Test whether our breadcrumbs are coherent when somebody uses HTTPConnection
    wrongly.
    """

    sentry_init()
    events = capture_events()

    conn = HTTPSConnection("httpstat.us", 443)

    # make sure we release the resource, even if the test fails
    request.addfinalizer(conn.close)

    conn.request("GET", "/200")

    with pytest.raises(Exception):
        # This raises an exception, because we didn't call `getresponse` for
        # the previous request yet.
        #
        # This call should not affect our breadcrumb.
        conn.request("POST", "/200")

    response = conn.getresponse()
    assert response._method == "GET"

    capture_message("Testing!")

    (event,) = events
    (crumb,) = event["breadcrumbs"]["values"]

    assert crumb["type"] == "http"
    assert crumb["category"] == "httplib"
    assert crumb["data"] == {
        "url": "https://httpstat.us/200",
        "method": "GET",
        "status_code": 200,
        "reason": "OK",
        "http.fragment": "",
        "http.query": "",
    }


def test_outgoing_trace_headers(sentry_init, monkeypatch):
    # HTTPSConnection.send is passed a string containing (among other things)
    # the headers on the request. Mock it so we can check the headers, and also
    # so it doesn't try to actually talk to the internet.
    mock_send = mock.Mock()
    monkeypatch.setattr(HTTPSConnection, "send", mock_send)

    sentry_init(traces_sample_rate=1.0)

    headers = {}
    headers["baggage"] = (
        "other-vendor-value-1=foo;bar;baz, sentry-trace_id=771a43a4192642f0b136d5159a501700, "
        "sentry-public_key=49d0f7386ad645858ae85020e393bef3, sentry-sample_rate=0.01337, "
        "sentry-user_id=Am%C3%A9lie, other-vendor-value-2=foo;bar;"
    )

    transaction = Transaction.continue_from_headers(headers)

    with start_transaction(
        transaction=transaction,
        name="/interactions/other-dogs/new-dog",
        op="greeting.sniff",
        trace_id="12312012123120121231201212312012",
    ) as transaction:

        HTTPSConnection("www.squirrelchasers.com").request("GET", "/top-chasers")

        (request_str,) = mock_send.call_args[0]
        request_headers = {}
        for line in request_str.decode("utf-8").split("\r\n")[1:]:
            if line:
                key, val = line.split(": ")
                request_headers[key] = val

        request_span = transaction._span_recorder.spans[-1]
        expected_sentry_trace = "{trace_id}-{parent_span_id}-{sampled}".format(
            trace_id=transaction.trace_id,
            parent_span_id=request_span.span_id,
            sampled=1,
        )
        assert request_headers["sentry-trace"] == expected_sentry_trace

        expected_outgoing_baggage_items = [
            "sentry-trace_id=771a43a4192642f0b136d5159a501700",
            "sentry-public_key=49d0f7386ad645858ae85020e393bef3",
            "sentry-sample_rate=0.01337",
            "sentry-user_id=Am%C3%A9lie",
        ]

        assert sorted(request_headers["baggage"].split(",")) == sorted(
            expected_outgoing_baggage_items
        )


def test_outgoing_trace_headers_head_sdk(sentry_init, monkeypatch):
    # HTTPSConnection.send is passed a string containing (among other things)
    # the headers on the request. Mock it so we can check the headers, and also
    # so it doesn't try to actually talk to the internet.
    mock_send = mock.Mock()
    monkeypatch.setattr(HTTPSConnection, "send", mock_send)

    # make sure transaction is always sampled
    monkeypatch.setattr(random, "random", lambda: 0.1)

    sentry_init(traces_sample_rate=0.5, release="foo")
    transaction = Transaction.continue_from_headers({})

    with start_transaction(transaction=transaction, name="Head SDK tx") as transaction:
        HTTPSConnection("www.squirrelchasers.com").request("GET", "/top-chasers")

        (request_str,) = mock_send.call_args[0]
        request_headers = {}
        for line in request_str.decode("utf-8").split("\r\n")[1:]:
            if line:
                key, val = line.split(": ")
                request_headers[key] = val

        request_span = transaction._span_recorder.spans[-1]
        expected_sentry_trace = "{trace_id}-{parent_span_id}-{sampled}".format(
            trace_id=transaction.trace_id,
            parent_span_id=request_span.span_id,
            sampled=1,
        )
        assert request_headers["sentry-trace"] == expected_sentry_trace

        expected_outgoing_baggage_items = [
            "sentry-trace_id=%s" % transaction.trace_id,
            "sentry-sample_rate=0.5",
            "sentry-release=foo",
            "sentry-environment=production",
        ]

        assert sorted(request_headers["baggage"].split(",")) == sorted(
            expected_outgoing_baggage_items
        )
