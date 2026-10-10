"""Konfigurationstests ohne Netzwerk und echte Zugangsdaten."""
import tkinter as tk
import unittest
from unittest.mock import patch
import intercom_client as m


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        with patch.object(m.App, '_load_config'), patch.object(m.AdminServer, 'start'):
            self.app = m.App(self.root)

    def tearDown(self):
        for token in self.root.tk.call('after', 'info'):
            self.root.after_cancel(token)
        self.root.destroy()

    def test_neutral_defaults(self):
        config = self.app._collect_config()
        for key in ('server', 'user', 'pass', 'fritz_server', 'fritz_user', 'fritz_pass', 'rtsp', 'bot_key'):
            self.assertEqual(config[key], '')
        self.assertIn('Konfiguration', [self.app.notebook.tab(tab, 'text') for tab in self.app.notebook.tabs()])

    def test_legacy_camera_and_special_password_roundtrip(self):
        url = 'rtsp://camera:abc%23%40%3A@camera.example:8554/cam/realmonitor?channel=2&subtype=0'
        self.app._apply_config({'rtsp': url, 'pass': 'door-test', 'fritz_pass': 'fritz-test'})
        self.assertEqual(self.app.camera_fields['password'].get(), 'abc#@:')
        self.app._apply_camera_fields()
        self.assertEqual(self.app._collect_config()['rtsp'], url)
        self.assertEqual(self.app._collect_config()['pass'], 'door-test')

    def test_browser_never_returns_secrets(self):
        self.app._apply_config({'rtsp': 'rtsp://u:camera-secret@camera.example/video',
                                'pass': 'door-secret', 'fritz_pass': 'fritz-secret', 'bot_key': 'api-secret'})
        client = self.app.admin_server.flask_app.test_client()
        config = client.get('/api/config').get_json()
        for key in ('pass', 'fritz_pass', 'bot_key', 'rtsp'):
            self.assertEqual(config[key], '')


if __name__ == '__main__':
    unittest.main()
