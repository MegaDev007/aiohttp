import abc
import asyncio

import keyword
import collections
import mimetypes
import re
import os
import sys
import inspect

from collections.abc import Sized, Iterable, Container
from urllib.parse import urlencode, unquote
from types import MappingProxyType

from . import hdrs
from .abc import AbstractRouter, AbstractMatchInfo, AbstractView
from .protocol import HttpVersion11
from .web_exceptions import HTTPMethodNotAllowed, HTTPNotFound, HTTPNotModified
from .web_reqrep import StreamResponse
from .multidict import upstr


__all__ = ('UrlDispatcher', 'UrlMappingMatchInfo',
           'Resource', 'PlainResource', 'DynamicResource',
           'Route', 'PlainRoute', 'DynamicRoute', 'StaticRoute', 'View')


PY_35 = sys.version_info >= (3, 5)


class UrlMappingMatchInfo(dict, AbstractMatchInfo):

    def __init__(self, match_dict, route):
        super().__init__(match_dict)
        self._route = route

    @property
    def handler(self):
        return self._route.handler

    @property
    def route(self):
        return self._route

    def __repr__(self):
        return "<MatchInfo {}: {}>".format(super().__repr__(), self._route)


@asyncio.coroutine
def _defaultExpectHandler(request):
    """Default handler for Except: 100-continue"""
    if request.version == HttpVersion11:
        request.transport.write(b"HTTP/1.1 100 Continue\r\n\r\n")


class Resource(metaclass=abc.ABCMeta):
    def __init__(self, *, name=None):
        self._name = name

    @property
    def name(self):
        return self._name

    @abc.abstractmethod  # pragma: no branch
    def match(self, path):
        """Return dict with info for given path or
        None if route cannot process path."""

    @abc.abstractmethod  # pragma: no branch
    def url(self, **kwargs):
        """Construct url for route with additional params."""

    @staticmethod
    def _append_query(url, query):
        if query is not None:
            return url + "?" + urlencode(query)
        else:
            return url


class PlainResource(Resource):

    def __init__(self, path, *, name=None):
        super().__init__(name=name)
        self._path = path

    def match(self, path):
        # string comparison is about 10 times faster than regexp matching
        if self._path == path:
            return {}
        else:
            return None

    def url(self, *, query=None):
        return self._append_query(self._path, query)

    def __repr__(self):
        name = "'" + self.name + "' " if self.name is not None else ""
        return "<PlainResource {name} {path}".format(name=name,
                                                     path=self._path)


class DynamicResource(Resource):

    def __init__(self, pattern, formatter, *, name=None):
        super().__init__(name=name)
        self._pattern = pattern
        self._formatter = formatter

    def match(self, path):
        match = self._pattern.match(path)
        if match is None:
            return None
        else:
            return {key: unquote(value) for key, value in
                    match.groupdict().items()}

    def url(self, *, parts, query=None):
        url = self._formatter.format_map(parts)
        return self._append_query(url, query)

    def __repr__(self):
        name = "'" + self.name + "' " if self.name is not None else ""
        return ("<DynamicResource {name} {formatter}"
                .format(name=name, formatter=self._formatter))


class Route(metaclass=abc.ABCMeta):

    def __init__(self, method, handler, resource, *, expect_handler=None):
        if expect_handler is None:
            expect_handler = _defaultExpectHandler
        assert asyncio.iscoroutinefunction(expect_handler), \
            'Coroutine is expected, got {!r}'.format(expect_handler)

        self._method = method
        self._handler = handler
        self._resource = resource
        self._expect_handler = expect_handler

    def __repr__(self):
        return "<Route [{method}] {resource} -> {handler!r}".format(
            method=self.method, resource=self._resource,
            handler=self.handler)

    @property
    def method(self):
        return self._method

    @property
    def handler(self):
        return self._handler

    @property
    def name(self):
        return self._resource.name

    @property
    def resource(self):
        return self._resource

    def match(self, path):
        """Return dict with info for given path or
        None if route cannot process path."""
        return self._resource.match(path)

    def url(self, **kwargs):
        """Construct url for route with additional params."""
        return self._resource.url(**kwargs)

    @asyncio.coroutine
    def handle_expect_header(self, request):
        return (yield from self._expect_handler(request))


PlainRoute = DynamicRoute = Route


class StaticRoute(Route):

    def __init__(self, name, prefix, directory, *,
                 expect_handler=None, chunk_size=256*1024,
                 response_factory=StreamResponse):
        assert prefix.startswith('/'), prefix
        assert prefix.endswith('/'), prefix
        super().__init__(
            'GET', self.handle, name, expect_handler=expect_handler)
        self._prefix = prefix
        self._prefix_len = len(self._prefix)
        self._directory = os.path.abspath(directory) + os.sep
        self._chunk_size = chunk_size
        self._response_factory = response_factory

        if not os.path.isdir(self._directory):
            raise ValueError(
                "No directory exists at '{}'".format(self._directory))

        if bool(os.environ.get("AIOHTTP_NOSENDFILE")):
            self._sendfile = self._sendfile_fallback

    def match(self, path):
        if not path.startswith(self._prefix):
            return None
        return {'filename': path[self._prefix_len:]}

    def url(self, *, filename, query=None):
        while filename.startswith('/'):
            filename = filename[1:]
        url = self._prefix + filename
        return Resource._append_query(url, query)

    def _sendfile_cb(self, fut, out_fd, in_fd, offset, count, loop,
                     registered):
        if registered:
            loop.remove_writer(out_fd)
        try:
            n = os.sendfile(out_fd, in_fd, offset, count)
            if n == 0:  # EOF reached
                n = count
        except (BlockingIOError, InterruptedError):
            n = 0
        except Exception as exc:
            fut.set_exception(exc)
            return

        if n < count:
            loop.add_writer(out_fd, self._sendfile_cb, fut, out_fd, in_fd,
                            offset + n, count - n, loop, True)
        else:
            fut.set_result(None)

    @asyncio.coroutine
    def _sendfile_system(self, req, resp, fobj, count):
        """
        Write `count` bytes of `fobj` to `resp` starting from `offset` using
        the ``sendfile`` system call.

        `req` should be a :obj:`aiohttp.web.Request` instance.

        `resp` should be a :obj:`aiohttp.web.StreamResponse` instance.

        `fobj` should be an open file object.

        `offset` should be an integer >= 0.

        `count` should be an integer > 0.
        """
        transport = req.transport

        if transport.get_extra_info("sslcontext"):
            yield from self._sendfile_fallback(req, resp, fobj, count)
            return

        yield from resp.drain()

        loop = req.app.loop
        out_fd = transport.get_extra_info("socket").fileno()
        in_fd = fobj.fileno()
        fut = asyncio.Future(loop=loop)

        self._sendfile_cb(fut, out_fd, in_fd, 0, count, loop, False)

        yield from fut

    @asyncio.coroutine
    def _sendfile_fallback(self, req, resp, fobj, count):
        """
        Mimic the :meth:`_sendfile_system` method, but without using the
        ``sendfile`` system call. This should be used on systems that don't
        support the ``sendfile`` system call.

        To avoid blocking the event loop & to keep memory usage low, `fobj` is
        transferred in chunks controlled by the `chunk_size` argument to
        :class:`StaticRoute`.
        """
        chunk_size = self._chunk_size

        chunk = fobj.read(chunk_size)
        while chunk and count > chunk_size:
            resp.write(chunk)
            yield from resp.drain()
            count = count - chunk_size
            chunk = fobj.read(chunk_size)

        if chunk:
            resp.write(chunk[:count])
            yield from resp.drain()

    if hasattr(os, "sendfile"):  # pragma: no cover
        _sendfile = _sendfile_system
    else:  # pragma: no cover
        _sendfile = _sendfile_fallback

    @asyncio.coroutine
    def handle(self, request):
        filename = request.match_info['filename']
        filepath = os.path.abspath(os.path.join(self._directory, filename))
        if not filepath.startswith(self._directory):
            raise HTTPNotFound()
        if not os.path.exists(filepath) or not os.path.isfile(filepath):
            raise HTTPNotFound()

        st = os.stat(filepath)

        modsince = request.if_modified_since
        if modsince is not None and st.st_mtime <= modsince.timestamp():
            raise HTTPNotModified()

        ct, encoding = mimetypes.guess_type(filepath)
        if not ct:
            ct = 'application/octet-stream'

        resp = self._response_factory()
        resp.content_type = ct
        if encoding:
            resp.headers[hdrs.CONTENT_ENCODING] = encoding
        resp.last_modified = st.st_mtime

        file_size = st.st_size

        resp.content_length = file_size
        resp.set_tcp_cork(True)
        try:
            yield from resp.prepare(request)

            with open(filepath, 'rb') as f:
                yield from self._sendfile(request, resp, f, file_size)

        finally:
            resp.set_tcp_nodelay(True)

        return resp

    def __repr__(self):
        name = "'" + self.name + "' " if self.name is not None else ""
        return "<StaticRoute {name}[{method}] {path} -> {directory!r}".format(
            name=name, method=self.method, path=self._prefix,
            directory=self._directory)


class SystemRoute(Route):

    def __init__(self, status, reason):
        super().__init__(hdrs.METH_ANY, None, None)
        self._status = status
        self._reason = reason

    def url(self, **kwargs):
        raise RuntimeError(".url() is not allowed for SystemRoute")

    def match(self, path):
        return None

    @property
    def status(self):
        return self._status

    @property
    def reason(self):
        return self._reason

    def __repr__(self):
        return "<SystemRoute {status}: {reason}>".format(status=self._status,
                                                         reason=self._reason)


class _NotFoundMatchInfo(UrlMappingMatchInfo):

    route = SystemRoute(404, 'Not Found')

    def __init__(self):
        super().__init__({}, None)

    @property
    def handler(self):
        return self._not_found

    @asyncio.coroutine
    def _not_found(self, request):
        raise HTTPNotFound()

    def __repr__(self):
        return "<MatchInfo: not found>"


class _MethodNotAllowedMatchInfo(UrlMappingMatchInfo):

    route = SystemRoute(405, 'Method Not Allowed')

    def __init__(self, method, allowed_methods):
        super().__init__({}, None)
        self._method = method
        self._allowed_methods = allowed_methods

    @property
    def handler(self):
        return self._not_allowed

    @asyncio.coroutine
    def _not_allowed(self, request):
        raise HTTPMethodNotAllowed(self._method, self._allowed_methods)

    def __repr__(self):
        return ("<MatchInfo: method {} is not allowed (allowed methods: {}>"
                .format(self._method,
                        ', '.join(sorted(self._allowed_methods))))


class View(AbstractView):

    @asyncio.coroutine
    def __iter__(self):
        if self.request.method not in hdrs.METH_ALL:
            self._raise_allowed_methods()
        method = getattr(self, self.request.method.lower(), None)
        if method is None:
            self._raise_allowed_methods()
        resp = yield from method()
        return resp

    if PY_35:
        def __await__(self):
            return (yield from self.__iter__())

    def _raise_allowed_methods(self):
        allowed_methods = {m for m in hdrs.METH_ALL if hasattr(self, m)}
        raise HTTPMethodNotAllowed(self.request.method, allowed_methods)


class RoutesView(Sized, Iterable, Container):

    __slots__ = '_urls'

    def __init__(self, urls):
        self._urls = urls

    def __len__(self):
        return len(self._urls)

    def __iter__(self):
        yield from self._urls

    def __contains__(self, route):
        return route in self._urls


class UrlDispatcher(AbstractRouter, collections.abc.Mapping):

    DYN = re.compile(r'^\{(?P<var>[a-zA-Z][_a-zA-Z0-9]*)\}$')
    DYN_WITH_RE = re.compile(
        r'^\{(?P<var>[a-zA-Z][_a-zA-Z0-9]*):(?P<re>.+)\}$')
    GOOD = r'[^{}/]+'
    ROUTE_RE = re.compile(r'(\{[_a-zA-Z][^{}]*(?:\{[^{}]*\}[^{}]*)*\})')
    NAME_SPLIT_RE = re.compile('[.:-]')

    METHODS = {hdrs.METH_ANY, hdrs.METH_POST,
               hdrs.METH_GET, hdrs.METH_PUT, hdrs.METH_DELETE,
               hdrs.METH_PATCH, hdrs.METH_HEAD, hdrs.METH_OPTIONS}

    def __init__(self):
        super().__init__()
        self._resources = []
        self._named_resources = {}

    @asyncio.coroutine
    def resolve(self, request):
        path = request.raw_path
        method = request.method
        allowed_methods = set()

        for resource in self._resources:
            match_dict = resource.match(path)
            if match_dict is None:
                continue

            route_method = route.method
            if route_method == method or route_method == hdrs.METH_ANY:
                return UrlMappingMatchInfo(match_dict, route)

            allowed_methods.add(route_method)
        else:
            if allowed_methods:
                return _MethodNotAllowedMatchInfo(method, allowed_methods)
            else:
                return _NotFoundMatchInfo()

    def __iter__(self):
        return iter(self._named_resources)

    def __len__(self):
        return len(self._named_resources)

    def __contains__(self, name):
        return name in self._named_resources

    def __getitem__(self, name):
        return self._named_resources[name]

    def resources(self):
        # TODO: should distinguish between iterating over resources and views
        return RoutesView(self._resources)

    def routes(self):
        return RoutesView(self._resources)

    def named_resources(self):
        return MappingProxyType(self._named_resources)

    def named_routes(self):
        # TODO: it's ambiguous but it really resources.
        # DEPRECATE!
        return MappingProxyType(self._named_resources)

    def register_route(self, route):
        assert isinstance(route, Route), 'Instance of Route class is required.'
        self._reg_resource(route.resource)

    def _reg_resource(self, resource):
        assert isinstance(resource, Resource), \
            'Instance of Resource class is required.'

        name = resource.name

        if name is not None:
            parts = self.NAME_SPLIT_RE.split(name)
            for part in parts:
                if not part.isidentifier() or keyword.iskeyword(part):
                    raise ValueError('Incorrect route name {!r}, '
                                     'the name should be a sequence of '
                                     'python identifiers separated '
                                     'by dash, dot or column'.format(name))
            if name in self._named_resources:
                raise ValueError('Duplicate {!r}, '
                                 'already handled by {!r}'
                                 .format(name, self._named_resources[name]))
            self._named_resources[name] = resource
        self._resources.append(resource)

    def add_resource(self, path, *, name=None):
        if not path.startswith('/'):
            raise ValueError("path should be started with /")
        if not ('{' in path or '}' in path or self.ROUTE_RE.search(path)):
            resource = PlainResource(path, name=name)
            self._reg_resource(resource)
            return resource

        pattern = ''
        formatter = ''
        for part in self.ROUTE_RE.split(path):
            match = self.DYN.match(part)
            if match:
                pattern += '(?P<{}>{})'.format(match.group('var'), self.GOOD)
                formatter += '{' + match.group('var') + '}'
                continue

            match = self.DYN_WITH_RE.match(part)
            if match:
                pattern += '(?P<{var}>{re})'.format(**match.groupdict())
                formatter += '{' + match.group('var') + '}'
                continue

            if '{' in part or '}' in part:
                raise ValueError("Invalid path '{}'['{}']".format(path, part))

            formatter += part
            pattern += re.escape(part)

        try:
            compiled = re.compile('^' + pattern + '$')
        except re.error as exc:
            raise ValueError(
                "Bad pattern '{}': {}".format(pattern, exc)) from None
        resource = DynamicResource(compiled, formatter, name=name)
        self.register_resource(resource)
        return resource

    def add_route(self, method, path, handler,
                  *, name=None, expect_handler=None):

        assert callable(handler), handler
        if asyncio.iscoroutinefunction(handler):
            pass
        elif inspect.isgeneratorfunction(handler):
            pass
        elif isinstance(handler, type) and issubclass(handler, AbstractView):
            pass
        else:
            handler = asyncio.coroutine(handler)

        method = upstr(method)
        if method not in self.METHODS:
            raise ValueError("{} is not allowed HTTP method".format(method))

        resource = self.add_resource(path, name=name)
        route = Route(method, handler, resource, expect_handler=expect_handler)
        return route

    def add_static(self, prefix, path, *, name=None, expect_handler=None,
                   chunk_size=256*1024, response_factory=StreamResponse):
        """
        Adds static files view
        :param prefix - url prefix
        :param path - folder with files
        """
        assert prefix.startswith('/')
        if not prefix.endswith('/'):
            prefix += '/'
        route = StaticRoute(name, prefix, path,
                            expect_handler=expect_handler,
                            chunk_size=chunk_size,
                            response_factory=response_factory)
        self.register_route(route)
        return route
