#!/usr/bin/env python3
"""Protocol parser example."""
import argparse
import asynchttp
import collections
import tulip
try:
    import signal
except ImportError:
    signal = None


MSG_TEXT = b'text:'
MSG_PING = b'ping:'
MSG_PONG = b'pong:'
MSG_STOP = b'stop:'

Message = collections.namedtuple('Message', ('tp', 'data'))


def my_protocol_parser(out, buf):
    """Parser is used with StreamParser for incremental protocol parsing.
    Parser is a generator function, but it is not a coroutine. Usually
    parsers are implemented as a state machine.

    more details in tulip/parsers.py
    existing parsers:
      * http protocol parsers tulip/http/protocol.py
      * websocket parser tulip/http/websocket.py
    """
    while True:
        tp = yield from buf.read(5)
        if tp in (MSG_PING, MSG_PONG):
            # skip line
            yield from buf.skipuntil(b'\r\n')
            out.feed_data(Message(tp, None))
        elif tp == MSG_STOP:
            out.feed_data(Message(tp, None))
        elif tp == MSG_TEXT:
            # read text
            text = yield from buf.readuntil(b'\r\n')
            out.feed_data(Message(tp, text.strip().decode('utf-8')))
        else:
            raise ValueError('Unknown protocol prefix.')


class MyProtocolWriter:

    def __init__(self, transport):
        self.transport = transport

    def ping(self):
        self.transport.write(b'ping:\r\n')

    def pong(self):
        self.transport.write(b'pong:\r\n')

    def stop(self):
        self.transport.write(b'stop:\r\n')

    def send_text(self, text):
        self.transport.write(
            'text:{}\r\n'.format(text.strip()).encode('utf-8'))


class EchoServer(tulip.Protocol):

    def connection_made(self, transport):
        print('Connection made')
        self.transport = transport
        self.stream = asynchttp.StreamParser()
        tulip.Task(self.dispatch())

    def data_received(self, data):
        self.stream.feed_data(data)

    def eof_received(self):
        self.stream.feed_eof()

    def connection_lost(self, exc):
        print('Connection lost')

    @tulip.coroutine
    def dispatch(self):
        reader = self.stream.set_parser(my_protocol_parser)
        writer = MyProtocolWriter(self.transport)

        while True:
            try:
                msg = yield from reader.read()
            except asynchttp.EofStream:
                # client has been disconnected
                break

            print('Message received: {}'.format(msg))

            if msg.tp == MSG_PING:
                writer.pong()
            elif msg.tp == MSG_TEXT:
                writer.send_text('Re: ' + msg.data)
            elif msg.tp == MSG_STOP:
                self.transport.close()
                break


@tulip.coroutine
def start_client(loop, host, port):
    transport, stream = yield from loop.create_connection(
        asynchttp.StreamProtocol, host, port)
    reader = stream.set_parser(my_protocol_parser)
    writer = MyProtocolWriter(transport)
    writer.ping()

    message = 'This is the message. It will be echoed.'

    while True:
        try:
            msg = yield from reader.read()
        except asynchttp.EofStream:
            print('Server has been disconnected.')
            break

        print('Message received: {}'.format(msg))
        if msg.tp == MSG_PONG:
            writer.send_text(message)
            print('data sent:', message)
        elif msg.tp == MSG_TEXT:
            writer.stop()
            print('stop sent')
            break

    transport.close()


def start_server(loop, host, port):
    f = loop.start_serving(EchoServer, host, port)
    x = loop.run_until_complete(f)[0]
    print('serving on', x.getsockname())
    loop.run_forever()


ARGS = argparse.ArgumentParser(description="Protocol parser example.")
ARGS.add_argument(
    '--server', action="store_true", dest='server',
    default=False, help='Run tcp server')
ARGS.add_argument(
    '--client', action="store_true", dest='client',
    default=False, help='Run tcp client')
ARGS.add_argument(
    '--host', action="store", dest='host',
    default='127.0.0.1', help='Host name')
ARGS.add_argument(
    '--port', action="store", dest='port',
    default=9999, type=int, help='Port number')


if __name__ == '__main__':
    args = ARGS.parse_args()

    if ':' in args.host:
        args.host, port = args.host.split(':', 1)
        args.port = int(port)

    if (not (args.server or args.client)) or (args.server and args.client):
        print('Please specify --server or --client\n')
        ARGS.print_help()
    else:
        loop = tulip.get_event_loop()
        if signal is not None:
            loop.add_signal_handler(signal.SIGINT, loop.stop)

        if args.server:
            start_server(loop, args.host, args.port)
        else:
            loop.run_until_complete(start_client(loop, args.host, args.port))
