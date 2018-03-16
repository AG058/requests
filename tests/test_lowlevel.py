# -*- coding: utf-8 -*-
import pytest
import threading
import requests3 as requests

from tests.testserver.server import Server, consume_socket_content

from .utils import override_environ


def test_chunked_upload():
    """can safely send generators"""
    close_server = threading.Event()
    server = Server.basic_response_server(wait_to_close_event=close_server)
    data = iter([b'a', b'b', b'c'])
    with server as (host, port):
        url = 'http://{0}:{1}/'.format(host, port)
        r = requests.post(url, data=data, stream=True)
        close_server.set()  # release server block
    assert r.status_code == 200
    assert r.request.headers['Transfer-Encoding'] == 'chunked'


def test_incorrect_content_length():
    """Test ConnectionError raised for incomplete responses"""
    close_server = threading.Event()
    server = Server.text_response_server(
        "HTTP/1.1 200 OK\r\n" + "Content-Length: 50\r\n\r\n" + "Hello World."
    )
    with server as (host, port):
        url = 'http://{0}:{1}/'.format(host, port)
        r = requests.Request('GET', url).prepare()
        s = requests.Session()
        with pytest.raises(requests.exceptions.ConnectionError) as e:
            resp = s.send(r)
        assert "12 bytes read, 38 more expected" in str(e)
        close_server.set()  # release server block


def test_digestauth_401_count_reset_on_redirect():
    """Ensure we correctly reset num_401_calls after a successful digest auth,
    followed by a 302 redirect to another digest auth prompt.

    See https://github.com/requests/requests/issues/1979.
    """
    text_401 = (
        b'HTTP/1.1 401 UNAUTHORIZED\r\n'
        b'Content-Length: 0\r\n'
        b'WWW-Authenticate: Digest nonce="6bf5d6e4da1ce66918800195d6b9130d"'
        b', opaque="372825293d1c26955496c80ed6426e9e", '
        b'realm="me@kennethreitz.com", qop=auth\r\n\r\n'
    )
    text_302 = (
        b'HTTP/1.1 302 FOUND\r\n'
        b'Content-Length: 0\r\n'
        b'Location: /\r\n\r\n'
    )
    text_200 = (b'HTTP/1.1 200 OK\r\n' b'Content-Length: 0\r\n\r\n')
    expected_digest = (
        b'Authorization: Digest username="user", '
        b'realm="me@kennethreitz.com", '
        b'nonce="6bf5d6e4da1ce66918800195d6b9130d", uri="/"'
    )
    auth = requests.auth.HTTPDigestAuth('user', 'pass')

    def digest_response_handler(sock):
        # Respond to initial GET with a challenge.
        request_content = consume_socket_content(sock, timeout=0.5)
        assert request_content.startswith(b"GET / HTTP/1.1")
        sock.send(text_401)
        # Verify we receive an Authorization header in response, then redirect.
        request_content = consume_socket_content(sock, timeout=0.5)
        assert expected_digest in request_content
        sock.send(text_302)
        # Verify Authorization isn't sent to the redirected host,
        # then send another challenge.
        request_content = consume_socket_content(sock, timeout=0.5)
        assert b'Authorization:' not in request_content
        sock.send(text_401)
        # Verify Authorization is sent correctly again, and return 200 OK.
        request_content = consume_socket_content(sock, timeout=0.5)
        assert expected_digest in request_content
        sock.send(text_200)
        return request_content

    close_server = threading.Event()
    server = Server(digest_response_handler, wait_to_close_event=close_server)
    with server as (host, port):
        url = 'http://{0}:{1}/'.format(host, port)
        r = requests.get(url, auth=auth)
        # Verify server succeeded in authenticating.
        assert r.status_code == 200
        # Verify Authorization was sent in final request.
        assert 'Authorization' in r.request.headers
        assert r.request.headers['Authorization'].startswith('Digest ')
        # Verify redirect happened as we expected.
        assert r.history[0].status_code == 302
        close_server.set()


def test_digestauth_401_only_sent_once():
    """Ensure we correctly respond to a 401 challenge once, and then
    stop responding if challenged again.
    """
    text_401 = (
        b'HTTP/1.1 401 UNAUTHORIZED\r\n'
        b'Content-Length: 0\r\n'
        b'WWW-Authenticate: Digest nonce="6bf5d6e4da1ce66918800195d6b9130d"'
        b', opaque="372825293d1c26955496c80ed6426e9e", '
        b'realm="me@kennethreitz.com", qop=auth\r\n\r\n'
    )
    expected_digest = (
        b'Authorization: Digest username="user", '
        b'realm="me@kennethreitz.com", '
        b'nonce="6bf5d6e4da1ce66918800195d6b9130d", uri="/"'
    )
    auth = requests.auth.HTTPDigestAuth('user', 'pass')

    def digest_failed_response_handler(sock):
        # Respond to initial GET with a challenge.
        request_content = consume_socket_content(sock, timeout=0.5)
        assert request_content.startswith(b"GET / HTTP/1.1")
        sock.send(text_401)
        # Verify we receive an Authorization header in response, then
        # challenge again.
        request_content = consume_socket_content(sock, timeout=0.5)
        assert expected_digest in request_content
        sock.send(text_401)
        # Verify the client didn't respond to second challenge.
        request_content = consume_socket_content(sock, timeout=0.5)
        assert request_content == b''
        return request_content

    close_server = threading.Event()
    server = Server(
        digest_failed_response_handler, wait_to_close_event=close_server
    )
    with server as (host, port):
        url = 'http://{0}:{1}/'.format(host, port)
        r = requests.get(url, auth=auth)
        # Verify server didn't authenticate us.
        assert r.status_code == 401
        assert r.history[0].status_code == 401
        close_server.set()


def test_digestauth_only_on_4xx():
    """Ensure we only send digestauth on 4xx challenges.

    See https://github.com/requests/requests/issues/3772.
    """
    text_200_chal = (
        b'HTTP/1.1 200 OK\r\n'
        b'Content-Length: 0\r\n'
        b'WWW-Authenticate: Digest nonce="6bf5d6e4da1ce66918800195d6b9130d"'
        b', opaque="372825293d1c26955496c80ed6426e9e", '
        b'realm="me@kennethreitz.com", qop=auth\r\n\r\n'
    )
    auth = requests.auth.HTTPDigestAuth('user', 'pass')

    def digest_response_handler(sock):
        # Respond to GET with a 200 containing www-authenticate header.
        request_content = consume_socket_content(sock, timeout=0.5)
        assert request_content.startswith(b"GET / HTTP/1.1")
        sock.send(text_200_chal)
        # Verify the client didn't respond with auth.
        request_content = consume_socket_content(sock, timeout=0.5)
        assert request_content == b''
        return request_content

    close_server = threading.Event()
    server = Server(digest_response_handler, wait_to_close_event=close_server)
    with server as (host, port):
        url = 'http://{0}:{1}/'.format(host, port)
        r = requests.get(url, auth=auth)
        # Verify server didn't receive auth from us.
        assert r.status_code == 200
        assert len(r.history) == 0
        close_server.set()


_schemes_by_var_prefix = [
    ('http', ['http']), ('https', ['https']), ('all', ['http', 'https'])
]
_proxy_combos = []
for prefix, schemes in _schemes_by_var_prefix:
    for scheme in schemes:
        _proxy_combos.append(("{0}_proxy".format(prefix), scheme))
_proxy_combos += [(var.upper(), scheme) for var, scheme in _proxy_combos]


@pytest.mark.parametrize("var,scheme", _proxy_combos)
def test_use_proxy_from_environment(httpbin, var, scheme):
    url = "{0}://httpbin.org".format(scheme)
    fake_proxy = Server(
    )  # do nothing with the requests; just close the socket
    with fake_proxy as (host, port):
        proxy_url = "socks5://{0}:{1}".format(host, port)
        kwargs = {var: proxy_url}
        with override_environ(**kwargs):
            # fake proxy's lack of response will cause a ConnectionError
            with pytest.raises(requests.exceptions.ConnectionError):
                requests.get(url)
        # the fake proxy received a request
        assert len(fake_proxy.handler_results) == 1
        # it had actual content (not checking for SOCKS protocol for now)
        assert len(fake_proxy.handler_results[0]) > 0


def test_redirect_rfc1808_to_non_ascii_location():
    path = u'š'
    expected_path = b'%C5%A1'
    redirect_request = []  # stores the second request to the server

    def redirect_resp_handler(sock):
        consume_socket_content(sock, timeout=0.5)
        location = u'//{0}:{1}/{2}'.format(host, port, path)
        sock.send(
            b'HTTP/1.1 301 Moved Permanently\r\n'
            b'Content-Length: 0\r\n'
            b'Location: ' + location.encode('utf8') + b'\r\n'
            b'\r\n'
        )
        redirect_request.append(consume_socket_content(sock, timeout=0.5))
        sock.send(b'HTTP/1.1 200 OK\r\n\r\n')

    close_server = threading.Event()
    server = Server(redirect_resp_handler, wait_to_close_event=close_server)
    with server as (host, port):
        url = u'http://{0}:{1}'.format(host, port)
        r = requests.get(url=url, allow_redirects=True)
        assert r.status_code == 200
        assert len(r.history) == 1
        assert r.history[0].status_code == 301
        assert redirect_request[0].startswith(
            b'GET /' + expected_path + b' HTTP/1.1'
        )
        assert r.url == u'{0}/{1}'.format(url, expected_path.decode('ascii'))
        close_server.set()
