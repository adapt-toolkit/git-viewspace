"""Bounded socket frontend; one worker owns all Git, view and mount state."""

import copy
import selectors
import socket
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .errors import GitViewspaceError
from .protocol import MAX_MESSAGE_BYTES, decode_message, validate_request
from .storage import canonical_json_bytes

MAX_PEERS = 128
MAX_QUEUED = 64
IO_SECONDS = 5.0
OBSERVE_SECONDS = 1.0


@dataclass(eq=False)
class Peer:
    socket: socket.socket
    deadline: float
    state: str = "reading"
    buffer: bytearray = field(default_factory=bytearray)
    output: bytes = b""
    value: dict | None = None
    admitted: float = 0.0


class Control:
    def __init__(self, daemon):
        self.daemon = daemon
        self.selector = selectors.DefaultSelector()
        self.peers = set()
        self.pending = deque()
        self.future = None
        self.current = None
        self.current_kind = None
        self.draining = False
        self.stop_peer = None
        self.finished = False
        self.fatal = False
        self.last_observed = 0.0
        self.last_supervised = time.monotonic()
        self.lock = threading.Lock()
        self.snapshot = None
        self.captured = 0.0
        self.activity = None

    def phase(self, name, duration_ns=None):
        with self.lock:
            if self.activity is not None:
                self.activity["phase"] = name
                if duration_ns is not None:
                    self.activity["steps"].append({"name": name, "duration_ns": duration_ns})

    def _observe(self):
        try:
            self.daemon._validate_durable_state("status", None)
            snapshot = self.daemon.status()
        except GitViewspaceError as error:
            snapshot = self.daemon._error_response("status", error)
        with self.lock:
            self.snapshot = snapshot
            self.captured = time.time()
            self.last_observed = time.monotonic()

    def _work(self, value, admitted):
        started = time.monotonic()
        command = value["command"]
        with self.lock:
            self.activity = {
                "command": command,
                "view": value.get("view"),
                "phase": "dispatch",
                "started": started,
                "steps": [],
            }
        fatal = False
        try:
            if command == "supervise":
                self.daemon.supervise()
                response = {"ok": True}
            else:
                response = self.daemon._dispatch(value)
            if command != "stop" or not response["ok"]:
                self.phase("observe")
                self._observe()
            if command == "supervise":
                # Schedule from completion of the whole cycle, including slow observation.
                self.last_supervised = time.monotonic()
        except Exception as error:
            # Do not hide internal failures or claim that a dispatched mutation did not run.
            print(
                f"git-viewspace operation {command} failed: {type(error).__name__}",
                file=sys.stderr,
                flush=True,
            )
            response = self._error(
                "DAEMON_FAILED", "operation failed; inspect committed state", executed=True
            )
            fatal = True
            with self.lock:
                self.snapshot = self.daemon._error_response(
                    "status",
                    GitViewspaceError(
                        "DAEMON_FAILED", "state observation unavailable after internal failure"
                    ),
                )
                self.captured = time.time()
                self.last_observed = time.monotonic()
        with self.lock:
            timing = {
                "queue_seconds": started - admitted,
                "service_seconds": time.monotonic() - started,
                "steps": self.activity["steps"],
            }
            self.activity = None
        self._metadata(response, timing=timing, executed=True)
        return response, fatal

    @staticmethod
    def _metadata(response, **metadata):
        if response["ok"]:
            response.update(metadata)
        else:
            response["error"].setdefault("data", {}).update(metadata)

    def _error(self, code, message, *, executed=False):
        return self.daemon._error_response(
            None, GitViewspaceError(code, message, data={"executed": executed})
        )

    def _status(self, value):
        now = time.monotonic()
        with self.lock:
            response = copy.deepcopy(self.snapshot)
            observation = {
                "kind": "last_consistent_snapshot",
                "captured_at_unix": self.captured,
                "age_seconds": max(0.0, now - self.last_observed),
            }
            activity = copy.deepcopy(self.activity)
        if activity is not None:
            activity["elapsed_seconds"] = max(0.0, now - activity.pop("started"))
        activity = {"running": activity, "queued": len(self.pending), "stopping": self.draining}
        if response["ok"] and value["command"] == "view-status":
            name = value["view"]
            views = [v for v in response["views"] if name is None or v["name"] == name]
            if name is not None and not views:
                response = self._error("VIEW_NOT_FOUND", f"view is absent from snapshot: {name}")
            elif (
                value.get("ensure_view_id") is not None
                and views[0]["view_id"] != value["ensure_view_id"]
            ):
                response = self._error("VIEW_ID_MISMATCH", "snapshot view identity differs")
            else:
                response = {
                    "ok": True,
                    "command": "view status",
                    "views": views,
                    "view": views[0] if name is not None else None,
                }
        self._metadata(response, observation=observation, activity=activity)
        return response

    def _close(self, peer):
        self.peers.discard(peer)
        try:
            self.pending.remove(peer)
        except ValueError:
            pass
        try:
            self.selector.unregister(peer.socket)
        except (KeyError, ValueError):
            pass
        peer.socket.close()

    def _reply(self, peer, response):
        if peer not in self.peers:
            return
        if not response["ok"]:
            data = response["error"].setdefault("data", {})
            data.setdefault("executed", False)
            data.setdefault(
                "timing",
                {
                    "queue_seconds": max(0.0, time.monotonic() - peer.admitted)
                    if peer.admitted
                    else 0.0,
                    "service_seconds": 0.0,
                },
            )
        response.setdefault("root", str(self.daemon.context.root))
        response.setdefault("root_uuid", self.daemon.context.root_uuid)
        payload = canonical_json_bytes(response)
        if len(payload) > MAX_MESSAGE_BYTES:
            response = self._error(
                "PROTOCOL_ERROR", "response exceeds control message limit", executed=True
            )
            response.update(
                root=str(self.daemon.context.root), root_uuid=self.daemon.context.root_uuid
            )
            payload = canonical_json_bytes(response)
        peer.output = payload
        peer.buffer.clear()
        peer.value = None
        peer.state = "writing"
        peer.deadline = time.monotonic() + IO_SECONDS
        self.selector.modify(peer.socket, selectors.EVENT_WRITE, peer)

    def _drain(self, peer=None):
        if self.draining:
            if peer is not None:
                self._reply(peer, self._error("DAEMON_STOPPING", "shutdown already initiated"))
            return
        self.draining = True
        self.stop_peer = peer
        if peer is not None:
            peer.state = "running"
        while self.pending:
            waiting = self.pending.popleft()
            self._reply(
                waiting, self._error("DAEMON_STOPPING", "request abandoned before dispatch")
            )

    def _read(self, peer):
        try:
            part = peer.socket.recv(min(65536, MAX_MESSAGE_BYTES + 1 - len(peer.buffer)))
            if not part:
                self._close(peer)
                return
            if peer.state != "reading":
                self._close(peer)  # one request per connection
                return
            peer.buffer.extend(part)
            if len(peer.buffer) > MAX_MESSAGE_BYTES:
                raise GitViewspaceError("PROTOCOL_ERROR", "request exceeds message limit")
            newline = peer.buffer.find(b"\n")
            if newline < 0:
                return
            if newline != len(peer.buffer) - 1:
                raise GitViewspaceError("PROTOCOL_ERROR", "trailing request data")
            value = validate_request(decode_message(bytes(peer.buffer[:-1]), "daemon request"))
            peer.buffer.clear()
            if value["command"] in {"status", "view-status"}:
                self._reply(peer, self._status(value))
                return
            if value["command"] == "stop":
                self._drain(peer)
                return
            if self.draining:
                self._reply(peer, self._error("DAEMON_STOPPING", "request not admitted"))
                return
            if len(self.pending) >= MAX_QUEUED:
                self._reply(peer, self._error("DAEMON_BUSY", "mutation queue is full"))
                return
            peer.value = value
            peer.admitted = time.monotonic()
            peer.deadline = peer.admitted + value.get("queue_timeout_seconds", 300.0)
            peer.state = "queued"
            self.pending.append(peer)
        except GitViewspaceError as error:
            response = self.daemon._error_response(None, error)
            self._metadata(response, executed=False)
            self._reply(peer, response)
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            self._close(peer)

    def _write(self, peer):
        try:
            size = peer.socket.send(peer.output)
            peer.output = peer.output[size:]
            if not peer.output:
                self._close(peer)
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            self._close(peer)

    def _completed(self):
        if self.future is None or not self.future.done():
            return
        response, fatal = self.future.result()
        if self.current is not None:
            self._reply(self.current, response)
        kind = self.current_kind
        self.future = self.current = self.current_kind = None
        self.fatal |= fatal
        if fatal:
            self._drain()
        if kind == "stop":
            if response["ok"] or self.fatal or self.daemon.signal_stop:
                self.finished = True
            else:
                # Public stop failed visibly; retain status and operator retry access.
                self.draining = False
                self.stop_peer = None

    def _schedule(self, executor):
        if self.future is not None or self.finished:
            return
        now = time.monotonic()
        if self.draining:
            value, peer, admitted = {"command": "stop"}, self.stop_peer, now
        elif now - self.last_supervised >= OBSERVE_SECONDS:
            value, peer, admitted = {"command": "supervise"}, None, now
        elif self.pending:
            peer = self.pending.popleft()
            # A disconnected/expired peer is never handed to the worker.
            if peer not in self.peers:
                return
            if peer.deadline <= now:
                self._reply(
                    peer, self._error("REQUEST_EXPIRED", "queue budget expired before dispatch")
                )
                return
            value, admitted = peer.value, peer.admitted
            peer.state = "running"
        else:
            return
        self.current, self.current_kind = peer, value["command"]
        self.future = executor.submit(self._work, value, admitted)

    def run(self):
        listener = self.daemon.server
        listener.setblocking(False)
        self.selector.register(listener, selectors.EVENT_READ, None)
        self.daemon._control = self
        try:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="git-viewspace-writer") as executor:
                executor.submit(self._observe).result()
                while True:
                    if self.daemon.signal_stop and not self.finished:
                        self._drain()
                    self._completed()
                    if self.finished and not any(p.state == "writing" for p in self.peers):
                        break
                    for key, mask in self.selector.select(0.02):
                        if key.data is None:
                            if self.finished:
                                continue
                            try:
                                connection, _ = listener.accept()
                                if len(self.peers) >= MAX_PEERS:
                                    connection.close()
                                    continue
                                connection.setblocking(False)
                                peer = Peer(connection, time.monotonic() + IO_SECONDS)
                                self.peers.add(peer)
                                self.selector.register(connection, selectors.EVENT_READ, peer)
                            except (BlockingIOError, OSError):
                                continue
                        elif mask & selectors.EVENT_WRITE:
                            self._write(key.data)
                        else:
                            self._read(key.data)
                    now = time.monotonic()
                    for peer in tuple(self.peers):
                        if peer.state == "running" or now < peer.deadline:
                            continue
                        if peer.state == "queued":
                            self.pending.remove(peer)
                            error = self._error(
                                "REQUEST_EXPIRED", "queue budget expired before dispatch"
                            )
                            self._metadata(
                                error,
                                timing={"queue_seconds": now - peer.admitted, "service_seconds": 0},
                            )
                            self._reply(peer, error)
                        else:
                            self._close(peer)
                    self._schedule(executor)
        finally:
            # Executor's context joined any dispatched operation before state-owner teardown.
            for peer in tuple(self.peers):
                self._close(peer)
            self.selector.close()
            self.daemon._control = None
        if self.fatal:
            raise GitViewspaceError("DAEMON_FAILED", "control worker failed; inspect committed state")
