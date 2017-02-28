"""Http related parsers and protocol."""

import asyncio
import collections
import http.server
import string
import sys
import zlib
from urllib.parse import SplitResult

import yarl

import aiohttp

from .abc import AbstractPayloadWriter
from .helpers import create_future, noop

__all__ = ('RESPONSES', 'SERVER_SOFTWARE',
           'PayloadWriter', 'HttpVersion', 'HttpVersion10', 'HttpVersion11')

ASCIISET = set(string.printable)
SERVER_SOFTWARE = 'Python/{0[0]}.{0[1]} aiohttp/{1}'.format(
    sys.version_info, aiohttp.__version__)

RESPONSES = http.server.BaseHTTPRequestHandler.responses

HttpVersion = collections.namedtuple(
    'HttpVersion', ['major', 'minor'])
HttpVersion10 = HttpVersion(1, 0)
HttpVersion11 = HttpVersion(1, 1)


class PayloadWriter(AbstractPayloadWriter):

    def __init__(self, stream, loop, acquire=True):
        if loop is None:
            loop = asyncio.get_event_loop()

        self._stream = stream
        self._transport = None

        self.loop = loop
        self.length = None
        self.chunked = False
        self.buffer_size = 0
        self.output_size = 0

        self._eof = False
        self._buffer = []
        self._compress = None
        self._drain_waiter = None

        if self._stream.available:
            self._transport = self._stream.transport
            self._stream.available = False
        elif acquire:
            self._stream.acquire(self)

    def set_transport(self, transport):
        self._transport = transport

        chunk = b''.join(self._buffer)
        if chunk:
            transport.write(chunk)
            self._buffer.clear()

        if self._drain_waiter is not None:
            waiter, self._drain_waiter = self._drain_waiter, None
            if not waiter.done():
                waiter.set_result(None)

    @property
    def tcp_nodelay(self):
        return self._stream.tcp_nodelay

    def set_tcp_nodelay(self, value):
        self._stream.set_tcp_nodelay(value)

    @property
    def tcp_cork(self):
        return self._stream.tcp_cork

    def set_tcp_cork(self, value):
        self._stream.set_tcp_cork(value)

    def enable_chunking(self):
        self.chunked = True

    def enable_compression(self, encoding='deflate'):
        zlib_mode = (16 + zlib.MAX_WBITS
                     if encoding == 'gzip' else -zlib.MAX_WBITS)
        self._compress = zlib.compressobj(wbits=zlib_mode)

    def buffer_data(self, chunk):
        if chunk:
            size = len(chunk)
            self.buffer_size += size
            self.output_size += size
            self._buffer.append(chunk)

    def _write(self, chunk):
        size = len(chunk)
        self.buffer_size += size
        self.output_size += size

        if self._transport is not None:
            if self._buffer:
                self._buffer.append(chunk)
                self._transport.write(b''.join(self._buffer))
                self._buffer.clear()
            else:
                self._transport.write(chunk)
        else:
            self._buffer.append(chunk)

    def write(self, chunk, *, drain=True, LIMIT=64*1024):
        """Writes chunk of data to a stream.

        write_eof() indicates end of stream.
        writer can't be used after write_eof() method being called.
        write() return drain future.
        """
        if self._compress is not None:
            chunk = self._compress.compress(chunk)
            if not chunk:
                return noop()

        if self.length is not None:
            chunk_len = len(chunk)
            if self.length >= chunk_len:
                self.length = self.length - chunk_len
            else:
                chunk = chunk[:self.length]
                self.length = 0
                if not chunk:
                    return noop()

        if chunk:
            if self.chunked:
                chunk_len = ('%x\r\n' % len(chunk)).encode('ascii')
                chunk = chunk_len + chunk + b'\r\n'

            self._write(chunk)

            if self.buffer_size > LIMIT and drain:
                self.buffer_size = 0
                return self.drain()

        return noop()

    @asyncio.coroutine
    def write_eof(self, chunk=b''):
        if self._eof:
            return

        if self._compress:
            if chunk:
                chunk = self._compress.compress(chunk)

            chunk = chunk + self._compress.flush()
            if chunk and self.chunked:
                chunk_len = ('%x\r\n' % len(chunk)).encode('ascii')
                chunk = chunk_len + chunk + b'\r\n0\r\n\r\n'
        else:
            if self.chunked:
                if chunk:
                    chunk_len = ('%x\r\n' % len(chunk)).encode('ascii')
                    chunk = chunk_len + chunk + b'\r\n0\r\n\r\n'
                else:
                    chunk = b'0\r\n\r\n'

        if chunk:
            self.buffer_data(chunk)

        yield from self.drain(True)

        self._eof = True
        self._transport = None
        self._stream.release()

    @asyncio.coroutine
    def drain(self, last=False):
        if self._transport is not None:
            if self._buffer:
                self._transport.write(b''.join(self._buffer))
                if not last:
                    self._buffer.clear()
            yield from self._stream.drain()
        else:
            # wait for transport
            if self._drain_waiter is None:
                self._drain_waiter = create_future(self.loop)

            yield from self._drain_waiter


class URL(yarl.URL):

    def __init__(self, schema, netloc, port, path, query, fragment, userinfo):
        self._strict = False

        if port:
            netloc += ':{}'.format(port)
        if userinfo:
            netloc = yarl.quote(
                userinfo, safe='@:',
                protected=':', strict=False) + '@' + netloc

        if path:
            path = yarl.quote(path, safe='@:', protected='/', strict=False)

        if query:
            query = yarl.quote(
                query, safe='=+&?/:@',
                protected=yarl.PROTECT_CHARS, qs=True, strict=False)

        if fragment:
            fragment = yarl.quote(fragment, safe='?/:@', strict=False)

        self._val = SplitResult(
            schema or '',  # scheme
            netloc=netloc, path=path, query=query, fragment=fragment)
        self._cache = {}
