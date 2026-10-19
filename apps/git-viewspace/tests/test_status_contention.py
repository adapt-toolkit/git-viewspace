"""Busy live control service is not absent; synthetic sockets, no mounts."""

import errno
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from git_viewspace import protocol, service
from git_viewspace.errors import GitViewspaceError
from git_viewspace.storage import exclusive_lock


class StatusContentionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="vm-status-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "state").mkdir()
        self.lock = self.root / "state/lock"
        self.lock.touch(mode=0o600)
        self.context = SimpleNamespace(
            root=self.root,
            root_uuid="fixture",
            socket_path=self.root / "control.sock",
            root_lock=self.lock,
        )

    def test_readonly_deadline_bounds_admission_and_fragmented_response(self):
        for phase in ("connect", "response"):
            with self.subTest(phase=phase):
                clock = [0.0]
                transport = mock.MagicMock()

                def connect(_path, phase=phase, clock=clock):
                    if phase == "connect":
                        clock[0] += 2
                        raise BlockingIOError(errno.EAGAIN, "busy")

                def receive(_size, clock=clock):
                    clock[0] += 3
                    return b" "

                transport.connect.side_effect = connect
                transport.recv.side_effect = receive
                with (
                    mock.patch("git_viewspace.protocol.socket.socket", return_value=transport),
                    mock.patch(
                        "git_viewspace.protocol.time.monotonic", side_effect=lambda clock=clock: clock[0]
                    ),
                    mock.patch(
                        "git_viewspace.protocol.time.sleep",
                        side_effect=lambda n, clock=clock: clock.__setitem__(0, clock[0] + n),
                    ),
                    self.assertRaises(GitViewspaceError) as caught,
                ):
                    protocol.call(self.context.socket_path, "status", timeout=5)
                self.assertEqual(caught.exception.code, "DAEMON_TIMEOUT")
                self.assertEqual(transport.sendall.call_count, 0 if phase == "connect" else 1)
                self.assertEqual(transport.recv.call_count, 0 if phase == "connect" else 2)
                transport.close.assert_called_once()

    def test_startup_probe_uses_bounded_status_wait(self):
        self.context.socket_path.touch()
        response = {
            "ok": True,
            "root": str(self.root),
            "root_uuid": "fixture",
            "daemon": {},
            "viewfs": {},
        }
        with mock.patch.object(service, "call", return_value=response) as call:
            self.assertEqual(service._running_status(self.context), response)
        call.assert_called_once_with(self.context.socket_path, "status", timeout=30.0)

    def test_delayed_response_is_not_reported_as_absence(self):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.context.socket_path))
        server.listen(1)
        server.settimeout(2)
        release = threading.Event()
        received = threading.Event()
        errors = []

        def peer():
            try:
                connection, _ = server.accept()
                with connection:
                    connection.settimeout(2)
                    protocol.receive_message(connection, "test request")
                    received.set()
                    release.wait(2)
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=peer)
        thread.start()
        try:
            with self.assertRaises(GitViewspaceError) as caught:
                protocol.call(self.context.socket_path, "status", timeout=0.1)
            self.assertTrue(received.wait(1), "test peer must actually receive the request")
            self.assertEqual(caught.exception.code, "DAEMON_TIMEOUT")
        finally:
            release.set()
            thread.join(3)
            server.close()
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_transport_timeout_cannot_spawn_or_read_offline(self):
        self.context.socket_path.touch()
        # Exercise the real protocol exception translation, not an invented service error.
        transport = mock.MagicMock()
        transport.recv.side_effect = TimeoutError("timed out")
        with (
            mock.patch("git_viewspace.protocol.socket.socket", return_value=transport),
            mock.patch.object(service.OnlineRoot, "load", return_value=self.context),
            mock.patch.object(service.RootContext, "load") as offline,
            mock.patch.object(service, "offline_status") as report,
        ):
            launch = mock.Mock(side_effect=OSError("must not launch replacement"))
            for name, operation in (
                ("start", lambda: service.start_background(self.root, popen=launch)),
                ("status", lambda: service.status(self.root)),
                ("view_status", lambda: service.view_status(self.root)),
                ("stop", lambda: service.stop(self.root)),
                ("mutation", lambda: service.view_init_readonly(self.root, "test", "a" * 40)),
            ):
                with self.subTest(operation=name):
                    transport.reset_mock()
                    with self.assertRaises(GitViewspaceError) as caught:
                        operation()
                    transport.sendall.assert_called_once()
                    self.assertEqual(caught.exception.code, "DAEMON_TIMEOUT")
            launch.assert_not_called()
            offline.assert_not_called()
            report.assert_not_called()

    def test_confirmed_absence_still_allows_startup(self):
        # The real connect must distinguish ENOENT from a response timeout.
        with self.assertRaises(GitViewspaceError) as absent:
            protocol.call(self.context.socket_path, "status", timeout=0.1)
        self.assertEqual(absent.exception.code, "DAEMON_UNAVAILABLE")
        # Use OSError so the existing launcher closes its pipe descriptors.
        launch = mock.Mock(side_effect=OSError("synthetic launch boundary"))
        with mock.patch.object(service.OnlineRoot, "load", return_value=self.context):
            with self.assertRaises(GitViewspaceError) as caught:
                service.start_background(self.root, popen=launch)
        self.assertEqual(caught.exception.code, "DAEMON_START_FAILED")
        launch.assert_called_once()

    def test_missing_socket_with_live_root_owner_cannot_read_offline(self):
        with (
            exclusive_lock(self.lock),
            mock.patch.object(service.OnlineRoot, "load", return_value=self.context),
            mock.patch.object(service.RootContext, "load") as offline,
            mock.patch.object(service, "offline_status") as report,
        ):
            for name, operation in (
                ("status", service.status),
                ("view_status", service.view_status),
                ("stop", service.stop),
            ):
                with self.subTest(operation=name):
                    with self.assertRaises(GitViewspaceError) as caught:
                        operation(self.root)
                    self.assertEqual(caught.exception.code, "ROOT_BUSY")
            offline.assert_not_called()
            report.assert_not_called()
