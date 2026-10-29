from __future__ import annotations

import base64
import ssl

import pytest
from kraken_fakes import DOC_SECRET, FakeTransport, err, kraken_routes, ok

from krakenbot.config import KrakenConfig, RetryConfig
from krakenbot.kraken_client import (
    FORBIDDEN_PRIVATE_METHODS,
    HttpResponse,
    KrakenClient,
    KrakenError,
    NonceGenerator,
    _ssl_context,
    sign,
    urllib_transport,
)
from krakenbot.retry import PermanentError, RetryExhaustedError

RETRY = RetryConfig(max_attempts=3, base_delay_s=0.01, jitter=False)


def client(transport, *, key="pubkey", secret=DOC_SECRET, trading=False) -> KrakenClient:
    return KrakenClient(
        KrakenConfig(), RETRY, api_key=key, api_secret=secret, trading_enabled=trading,
        transport=transport, sleep=lambda _: None,
    )


def test_signature_matches_kraken_documentation_example():
    sig = sign(
        "/0/private/AddOrder",
        "nonce=1616492376594&ordertype=limit&pair=XBTUSD&price=37500&type=buy&volume=1.25",
        "1616492376594",
        DOC_SECRET,
    )
    assert sig == (
        "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ=="
    )


def test_nonce_strictly_increases_even_with_frozen_clock():
    gen = NonceGenerator(clock=lambda: 1_000.0)
    values = [int(gen()) for _ in range(5)]
    assert values == [1_000_000, 1_000_001, 1_000_002, 1_000_003, 1_000_004]


def test_public_call_is_get_over_https_without_key():
    t = FakeTransport(kraken_routes())
    assert client(t).server_time() == 1761736000
    call = t.calls[0]
    assert call.method == "GET"
    assert call.url == "https://api.kraken.com/0/public/Time"
    assert "API-Key" not in call.headers and "API-Sign" not in call.headers


def test_private_call_is_signed_post_and_secret_never_sent():
    t = FakeTransport(kraken_routes())
    nonce = NonceGenerator(clock=lambda: 1_700_000_000.0)
    c = KrakenClient(KrakenConfig(), RETRY, api_key="pubkey", api_secret=DOC_SECRET,
                     transport=t, nonce=nonce)
    c.balance()
    call = t.calls[0]
    assert call.method == "POST"
    assert call.path == "/0/private/Balance"
    assert call.headers["API-Key"] == "pubkey"
    assert call.form == {"nonce": "1700000000000"}
    expected = sign("/0/private/Balance", "nonce=1700000000000", "1700000000000", DOC_SECRET)
    assert call.headers["API-Sign"] == expected
    assert DOC_SECRET.encode() not in (call.body or b"")
    assert DOC_SECRET not in str(call.headers)


def test_each_retry_uses_new_nonce():
    t = FakeTransport(kraken_routes(**{"/0/private/Balance": [err("EService:Busy"), ok({})]}))
    client(t).balance()
    nonces = [c.form["nonce"] for c in t.calls]
    assert len(nonces) == 2 and int(nonces[1]) > int(nonces[0])


@pytest.mark.parametrize(
    "response",
    [
        err("EService:Unavailable"),
        err("EAPI:Rate limit exceeded"),
        HttpResponse(502, b"<html>bad gateway</html>"),
        HttpResponse(429, b""),
        HttpResponse(200, b"not json"),
    ],
)
def test_transient_errors_are_retried(response):
    t = FakeTransport(kraken_routes(**{"/0/public/Time": [response, response, response]}))
    with pytest.raises(RetryExhaustedError):
        client(t).server_time()
    assert len(t.calls) == 3


def test_network_error_retried_then_succeeds():
    t = FakeTransport(
        kraken_routes(**{"/0/public/Time": [ConnectionError("reset"), ok({"unixtime": 5})]})
    )
    assert client(t).server_time() == 5


@pytest.mark.parametrize(
    "code",
    ["EAPI:Invalid key", "EAPI:Invalid nonce", "EGeneral:Permission denied",
     "EGeneral:Temporary lockout", "EGeneral:Invalid arguments"],
)
def test_permanent_errors_fail_fast(code):
    t = FakeTransport(kraken_routes(**{"/0/private/Balance": err(code)}))
    with pytest.raises(KrakenError) as ei:
        client(t).balance()
    assert ei.value.errors == [code]
    assert len(t.calls) == 1  # bez ponawiania: kolejne złe nonce mogą zablokować klucz


def test_warnings_are_not_errors():
    t = FakeTransport(
        kraken_routes(**{"/0/public/Time": HttpResponse(
            200, b'{"error": ["WGeneral:Something"], "result": {"unixtime": 7}}')})
    )
    assert client(t).server_time() == 7


@pytest.mark.parametrize("method", sorted(FORBIDDEN_PRIVATE_METHODS))
def test_withdrawals_blocked_even_for_trading_client(method):
    t = FakeTransport(kraken_routes())
    with pytest.raises(PermanentError, match="zablokowana w kodzie"):
        client(t, trading=True).private(method, {"asset": "XBT", "amount": "1"})
    assert t.calls == []


def test_read_only_client_cannot_trade():
    t = FakeTransport(kraken_routes())
    with pytest.raises(PermanentError, match="tylko odczyt"):
        client(t).private("AddOrder", {"pair": "XBTEUR"})
    with pytest.raises(PermanentError):
        client(t).private("SomeNewMethod")
    assert t.calls == []


def test_trading_methods_allowed_only_with_flag():
    t = FakeTransport(kraken_routes(**{"/0/private/CancelOrder": ok({"count": 1})}))
    assert client(t, trading=True).private("CancelOrder", {"txid": "X"}) == {"count": 1}


def test_private_call_without_key_refused():
    t = FakeTransport(kraken_routes())
    with pytest.raises(PermanentError, match="brak klucza"):
        client(t, key=None, secret=None).balance()
    assert t.calls == []


def test_invalid_secret_rejected_early():
    with pytest.raises(ValueError, match="base64"):
        client(FakeTransport(), secret="to nie jest base64!")
    with pytest.raises(ValueError):
        client(FakeTransport(), key="x", secret=None)


def test_ticker_batches_pairs():
    t = FakeTransport(kraken_routes())
    client(t).ticker([f"P{i}EUR" for i in range(120)])
    assert [len(c.query["pair"].split(",")) for c in t.calls] == [50, 50, 20]
    assert client(t).ticker([]) == {}


def test_https_only_and_certificate_verification():
    with pytest.raises(PermanentError, match="HTTPS"):
        urllib_transport("GET", "http://api.kraken.com/0/public/Time", {}, None, 1)
    ctx = _ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_doc_secret_is_valid_base64():
    assert len(base64.b64decode(DOC_SECRET)) == 64


def test_transport_respects_ip_family(monkeypatch):
    import socket

    from krakenbot.kraken_client import _connect_family

    seen = {}

    def fake_getaddrinfo(host, port, family, socktype):
        seen["family"] = family
        raise socket.gaierror("brak adresu")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(OSError):
        _connect_family(socket.AF_INET)(("api.kraken.com", 443), 1)
    assert seen["family"] == socket.AF_INET


def test_make_transport_wraps_network_errors(monkeypatch):
    import socket

    from krakenbot.kraken_client import KrakenTransientError, make_transport

    def fail(*a, **k):
        raise socket.gaierror("no AAAA record")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(KrakenTransientError, match="IPv6"):
        make_transport(6)("GET", "https://api.kraken.com/0/public/Time", {}, None, 2)
