"""TLS to the router: the certificate is pinned on first use and checked before the login."""

import ssl

import pytest
from fake_router import (
    FakeRouter,
    certificate_sha256,
    make_self_signed_certificate,
    server_ssl_context,
)
from mikrotik_push.routeros import AccessRefused, CertificateChanged, RouterOSConnection


@pytest.fixture
async def tls_router(tmp_path):
    certificate, key = make_self_signed_certificate(tmp_path)
    router = FakeRouter()
    router.port = await router.start(server_ssl_context(certificate, key))
    router.certificate_sha256 = certificate_sha256(certificate)
    yield router
    router.stop()


def unverified_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


async def open_tls(router, **options):
    return await RouterOSConnection.open(
        "127.0.0.1", router.port, "u", "secret", ssl_context=unverified_context(), **options
    )


async def test_the_fingerprint_is_that_of_the_certificate_the_router_presents(tls_router):
    connection = await open_tls(tls_router)

    assert connection.peer_certificate_sha256() == tls_router.certificate_sha256
    await connection.close()


async def test_a_matching_pinned_fingerprint_connects(tls_router):
    connection = await open_tls(tls_router, expected_certificate_sha256=tls_router.certificate_sha256)

    assert await connection.call("/system/identity/print") == [{"name": "fake"}]
    await connection.close()


async def test_a_different_certificate_is_refused_before_the_password_is_sent(tls_router):
    with pytest.raises(CertificateChanged) as refused:
        await open_tls(tls_router, expected_certificate_sha256="0" * 64)

    assert isinstance(refused.value, AccessRefused)
    assert tls_router.login_count == 0  # the login never reached the router


async def test_a_plain_connection_has_no_certificate(router_without_tls):
    connection = await RouterOSConnection.open("127.0.0.1", router_without_tls.port, "u", "secret")

    assert connection.peer_certificate_sha256() is None
    await connection.close()


@pytest.fixture
async def router_without_tls():
    router = FakeRouter()
    router.port = await router.start()
    yield router
    router.stop()
