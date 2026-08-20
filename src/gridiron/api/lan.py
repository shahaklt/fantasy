"""Putting the panel on the network without restarting the process.

`serve --lan` binds every interface at startup, which is the right thing when
you already know you want it. When you don't find out until the draft is
starting and your laptop is across the room, restarting the server means losing
the warm simulation cache and the current draft state.

So the panel can also open a *second* uvicorn on the same application object,
bound to every interface, in a background thread. The token gate is middleware
on that shared app, so the new listener is behind exactly the same rules as the
first one — there is no second, weaker door.
"""
from __future__ import annotations

import logging
import socket
import threading

log = logging.getLogger(__name__)

PORT_SEARCH = 10


def port_is_free(port: int, host: str = "0.0.0.0") -> bool:
    """Whether uvicorn could bind here.

    Checked by actually binding, because the failure modes differ per platform:
    Linux refuses 0.0.0.0:8000 while 127.0.0.1:8000 is held, Windows does not.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def pick_port(preferred: int) -> int | None:
    """The preferred port, or the first free one just above it."""
    for candidate in range(preferred, preferred + PORT_SEARCH):
        if port_is_free(candidate):
            return candidate
    return None


class LanListener:
    """A second uvicorn on the shared app, started and stopped on demand."""

    def __init__(self):
        self._server = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None

    @property
    def active(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self.port)

    def start(self, app, preferred_port: int, timeout: float = 5.0) -> int:
        """Bind every interface and return the port it landed on."""
        if self.active:
            return self.port

        import uvicorn

        port = pick_port(preferred_port)
        if port is None:
            raise RuntimeError(
                f"no free port between {preferred_port} and {preferred_port + PORT_SEARCH - 1}")

        # Signals are only listenable from the main thread; uvicorn already
        # skips them off-thread, so nothing else is needed here.
        config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, name="gridiron-lan", daemon=True)
        thread.start()

        deadline = threading.Event()
        waited = 0.0
        while waited < timeout and not server.started:
            if not thread.is_alive():
                raise RuntimeError("the network listener stopped before it finished starting")
            deadline.wait(0.05)
            waited += 0.05
        if not server.started:
            server.should_exit = True
            raise RuntimeError(f"the network listener did not come up on port {port}")

        self._server, self._thread, self.port = server, thread, port
        log.info("phone access listening on 0.0.0.0:%d", port)
        return port

    def stop(self, timeout: float = 5.0) -> None:
        """Close the extra socket. The main server is untouched."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                log.warning("the network listener did not shut down within %.1fs", timeout)
        self._server = self._thread = self.port = None


listener = LanListener()
