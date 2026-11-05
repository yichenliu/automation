"""Serving an app on a port of this computer from a thread, for a while (an eval looking at a try-out copy in a real
browser): `make_server(host, port, app)` then `serve_forever()` in a thread, `server_port`, and `shutdown()`.
Port 0 takes any free one. It's uvicorn underneath, as for the platform itself."""
import socket


class Server:
    def __init__(self, host, port, app):
        import uvicorn
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind((host, port))
        self.server_port = self._socket.getsockname()[1]
        self._server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, lifespan="off"))

    def serve_forever(self):
        self._server.run(sockets=[self._socket])

    def shutdown(self):
        self._server.should_exit = True


def make_server(host, port, app, threaded=True):
    return Server(host, port, app)
