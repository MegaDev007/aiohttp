import asyncio
import pytest
import unittest
from unittest import mock
from aiohttp.signals import Signal
from aiohttp.web import Request
from aiohttp.multidict import MultiDict, CIMultiDict
from aiohttp.protocol import HttpVersion
from aiohttp.protocol import RawRequestMessage


@pytest.fixture
def make_request():
    def maker(method, path, headers=CIMultiDict(), *,
              version=HttpVersion(1, 1), closing=False,
              sslcontext=None,
              secure_proxy_ssl_header=None):
        if version < HttpVersion(1, 1):
            closing = True
        app = mock.Mock()
        app._debug = False
        app.on_response_prepare = Signal(app)
        message = RawRequestMessage(method, path, version, headers, closing,
                                    False)
        payload = mock.Mock()
        transport = mock.Mock()

        def get_extra_info(key):
            if key == 'sslcontext':
                return sslcontext
            else:
                return None

        transport.get_extra_info.side_effect = get_extra_info
        writer = mock.Mock()
        reader = mock.Mock()
        req = Request(app, message, payload,
                      transport, reader, writer,
                      secure_proxy_ssl_header=secure_proxy_ssl_header)

        assert req.app is app
        assert req.content is payload
        assert req.transport is transport

        return req
    return maker


def test_ctor(make_request, warning):
    req = make_request('GET', '/path/to?a=1&b=2')

    assert 'GET' == req.method
    assert HttpVersion(1, 1) == req.version
    assert req.host is None
    assert '/path/to?a=1&b=2' == req.path_qs
    assert '/path/to' == req.path
    assert 'a=1&b=2' == req.query_string

    get = req.GET
    assert MultiDict([('a', '1'), ('b', '2')]) == get
    # second call should return the same object
    assert get is req.GET

    with warning(DeprecationWarning):
        req.payload

    assert req.keep_alive


def test_doubleslashes(make_request):
    req = make_request('GET', '//foo/')
    assert '//foo/' == req.path


def test_POST(make_request):
    req = make_request('POST', '/')
    with pytest.raises(RuntimeError):
        req.POST

    marker = object()
    req._post = marker
    assert req.POST is marker
    assert req.POST is marker


def test_content_type_not_specified(make_request):
    req = make_request('Get', '/')
    assert 'application/octet-stream' == req.content_type


def test_content_type_from_spec(make_request):
    req = make_request('Get', '/',
                       CIMultiDict([('CONTENT-TYPE', 'application/json')]))
    assert 'application/json' == req.content_type


class TestWebRequest(unittest.TestCase):

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(None)

    def tearDown(self):
        self.loop.close()

    def make_request(self, method, path, headers=CIMultiDict(), *,
                     version=HttpVersion(1, 1), closing=False,
                     sslcontext=None,
                     secure_proxy_ssl_header=None):
        if version < HttpVersion(1, 1):
            closing = True
        self.app = mock.Mock()
        self.app._debug = False
        self.app.on_response_prepare = Signal(self.app)
        message = RawRequestMessage(method, path, version, headers, closing,
                                    False)
        self.payload = mock.Mock()
        self.transport = mock.Mock()

        def get_extra_info(key):
            if key == 'sslcontext':
                return sslcontext
            else:
                return None

        self.transport.get_extra_info.side_effect = get_extra_info
        self.writer = mock.Mock()
        self.reader = mock.Mock()
        req = Request(self.app, message, self.payload,
                      self.transport, self.reader, self.writer,
                      secure_proxy_ssl_header=secure_proxy_ssl_header)
        return req

    def test_content_type_from_spec_with_charset(self):
        req = self.make_request(
            'Get', '/',
            CIMultiDict([('CONTENT-TYPE', 'text/html; charset=UTF-8')]))
        self.assertEqual('text/html', req.content_type)
        self.assertEqual('UTF-8', req.charset)

    def test_calc_content_type_on_getting_charset(self):
        req = self.make_request(
            'Get', '/',
            CIMultiDict([('CONTENT-TYPE', 'text/html; charset=UTF-8')]))
        self.assertEqual('UTF-8', req.charset)
        self.assertEqual('text/html', req.content_type)

    def test_urlencoded_querystring(self):
        req = self.make_request(
            'GET',
            '/yandsearch?text=%D1%82%D0%B5%D0%BA%D1%81%D1%82')
        self.assertEqual({'text': 'текст'}, req.GET)

    def test_non_ascii_path(self):
        req = self.make_request('GET', '/путь')
        self.assertEqual('/путь', req.path)

    def test_content_length(self):
        req = self.make_request(
            'Get', '/',
            CIMultiDict([('CONTENT-LENGTH', '123')]))

        self.assertEqual(123, req.content_length)

    def test_non_keepalive_on_http10(self):
        req = self.make_request('GET', '/', version=HttpVersion(1, 0))
        self.assertFalse(req.keep_alive)

    def test_non_keepalive_on_closing(self):
        req = self.make_request('GET', '/', closing=True)
        self.assertFalse(req.keep_alive)

    def test_call_POST_on_GET_request(self):
        req = self.make_request('GET', '/')

        ret = self.loop.run_until_complete(req.post())
        self.assertEqual(CIMultiDict(), ret)

    def test_call_POST_on_weird_content_type(self):
        req = self.make_request(
            'POST', '/',
            headers=CIMultiDict({'CONTENT-TYPE': 'something/weird'}))

        ret = self.loop.run_until_complete(req.post())
        self.assertEqual(CIMultiDict(), ret)

    def test_call_POST_twice(self):
        req = self.make_request('GET', '/')

        ret1 = self.loop.run_until_complete(req.post())
        ret2 = self.loop.run_until_complete(req.post())
        self.assertIs(ret1, ret2)

    def test_no_request_cookies(self):
        req = self.make_request('GET', '/')

        self.assertEqual(req.cookies, {})

        cookies = req.cookies
        self.assertIs(cookies, req.cookies)

    def test_request_cookie(self):
        headers = CIMultiDict(COOKIE='cookie1=value1; cookie2=value2')
        req = self.make_request('GET', '/', headers=headers)

        self.assertEqual(req.cookies, {
            'cookie1': 'value1',
            'cookie2': 'value2',
        })

    def test_request_cookie__set_item(self):
        headers = CIMultiDict(COOKIE='name=value')
        req = self.make_request('GET', '/', headers=headers)

        self.assertEqual(req.cookies, {'name': 'value'})

        with self.assertRaises(TypeError):
            req.cookies['my'] = 'value'

    def test_match_info(self):
        req = self.make_request('GET', '/')
        self.assertIsNone(req.match_info)
        match = {'a': 'b'}
        req._match_info = match
        self.assertIs(match, req.match_info)

    def test_request_is_dict(self):
        req = self.make_request('GET', '/')
        self.assertTrue(isinstance(req, dict))
        req['key'] = 'value'
        self.assertEqual('value', req['key'])

    def test___repr__(self):
        req = self.make_request('GET', '/path/to')
        self.assertEqual("<Request GET /path/to >", repr(req))

    def test_http_scheme(self):
        req = self.make_request('GET', '/')
        self.assertEqual("http", req.scheme)

    def test_https_scheme_by_ssl_transport(self):
        req = self.make_request('GET', '/', sslcontext=True)
        self.assertEqual("https", req.scheme)

    def test_https_scheme_by_secure_proxy_ssl_header(self):
        req = self.make_request('GET', '/',
                                secure_proxy_ssl_header=('X-HEADER', '1'),
                                headers=CIMultiDict({'X-HEADER': '1'}))
        self.assertEqual("https", req.scheme)

    def test_https_scheme_by_secure_proxy_ssl_header_false_test(self):
        req = self.make_request('GET', '/',
                                secure_proxy_ssl_header=('X-HEADER', '1'),
                                headers=CIMultiDict({'X-HEADER': '0'}))
        self.assertEqual("http", req.scheme)
