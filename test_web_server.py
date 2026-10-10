"""Regression: an idle connection must not block browser administration."""
import socket
import threading
import urllib.request
import unittest
from unittest.mock import Mock
import intercom_client as m


class WebServerTests(unittest.TestCase):
    def test_idle_connection_does_not_block_index(self):
        server = m.AdminServer(Mock(), port=0)
        server.start()
        self.assertIsNotNone(server.server)
        idle = socket.create_connection(('127.0.0.1', server.server.server_port), timeout=2)
        try:
            threading.Event().wait(0.1)
            url = 'http://127.0.0.1:' + str(server.server.server_port) + '/'
            with urllib.request.urlopen(url, timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b'Web-Admin', response.read())
        finally:
            idle.close()
            server.stop()
            server.server.server_close()


if __name__ == '__main__':
    unittest.main()
