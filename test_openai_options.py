"""No OpenAI requests: discovery, compatibility probes and UI use synthetic mocks."""
import asyncio
import io
import json
import tkinter as tk
import threading
import time
import unittest
import urllib.error
from unittest.mock import AsyncMock, Mock, patch
import openai_options as options
import intercom_client as m


class CatalogTests(unittest.TestCase):
    def test_filter_and_deduplicate(self):
        ids = ['gpt-realtime', 'gpt-realtime', 'gpt-realtime-translate',
               'gpt-realtime-whisper', 'gpt-4.1-mini', 'gpt-4o-audio-preview', 'gpt-5-codex', 'text-embedding-3-small']
        with patch.object(options, 'api_json', return_value={'data': [{'id': value} for value in ids]}):
            result = options.fetch_catalog('fake-key')
        self.assertEqual(result['realtime_models'], ['gpt-realtime'])
        self.assertEqual(result['vision_models'], ['gpt-4.1-mini'])
        self.assertIn('cedar', result['voices'])
        self.assertIn('marin', result['voices'])

    def test_invalid_response_and_empty_key(self):
        with patch.object(options, 'api_json', return_value={}):
            with self.assertRaises(options.OptionsError):
                options.fetch_catalog('fake-key')
        with self.assertRaises(options.OptionsError):
            options.fetch_catalog('')

    def test_http_error_does_not_expose_key_or_provider_body(self):
        error = urllib.error.HTTPError('https://api.openai.com', 401, 'fake-secret', {}, io.BytesIO(b'fake-secret'))
        with patch.object(options.urllib.request, 'urlopen', side_effect=error):
            with self.assertRaises(options.OptionsError) as context:
                options.fetch_catalog('fake-secret')
        self.assertNotIn('fake-secret', str(context.exception))
        error.close()

    def test_realtime_waits_for_confirmation_without_generating_audio(self):
        ws = Mock()
        ws.send = AsyncMock()
        ws.recv = AsyncMock(side_effect=[json.dumps({'type': 'session.created'}), json.dumps({
            'type': 'session.updated', 'session': {'audio': {'input': {'format': {'type': 'audio/pcmu'}},
             'output': {'voice': 'cedar', 'format': {'type': 'audio/pcmu'}}}}})])
        context = Mock()
        context.__aenter__ = AsyncMock(return_value=ws)
        context.__aexit__ = AsyncMock(return_value=None)
        with patch.object(options.websockets, 'connect', return_value=context):
            result = options.test_realtime('fake-key', 'gpt-realtime', 'cedar')
        self.assertIn('bestaetigt', result)
        self.assertEqual(json.loads(ws.send.call_args.args[0])['type'], 'session.update')
        self.assertEqual(ws.send.call_count, 1)

    def test_realtime_rejection(self):
        ws = Mock(send=AsyncMock(), recv=AsyncMock(return_value=json.dumps({'type': 'error'})))
        context = Mock()
        context.__aenter__ = AsyncMock(return_value=ws)
        context.__aexit__ = AsyncMock(return_value=None)
        with patch.object(options.websockets, 'connect', return_value=context):
            with self.assertRaises(options.OptionsError):
                options.test_realtime('fake-key', 'gpt-realtime', 'alloy')

    def test_vision_probe_is_synthetic_and_not_stored(self):
        with patch.object(options, 'api_json', return_value={'output': [{'content': [{'type': 'output_text', 'text': 'Blue'}]}]}) as api:
            result = options.test_vision('fake-key', 'gpt-4.1-mini')
        payload = api.call_args.args[2]
        self.assertFalse(payload['store'])
        self.assertTrue(payload['input'][0]['content'][1]['image_url'].startswith('data:image/png;base64,'))
        self.assertIn('erfolgreich', result)


class OptionsUiTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        with patch.object(m.App, '_load_config'), patch.object(m.AdminServer, 'start'):
            self.app = m.App(self.root)
        self.client = self.app.admin_server.flask_app.test_client()
        self.app.var_bot_key.set('fake-secret')

    def tearDown(self):
        for token in self.root.tk.call('after', 'info'):
            self.root.after_cancel(token)
        self.root.destroy()

    def test_refresh_preserves_current_and_manual_models(self):
        self.app.var_bot_model.set('future-realtime')
        self.app.var_vision_model.set('custom-vision')
        catalog = {'all_models': ['new-realtime'], 'realtime_models': ['new-realtime'], 'vision_models': ['gpt-4.1-mini']}
        with patch.object(m, 'fetch_catalog', return_value=catalog):
            response = self.client.post('/api/openai/models')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.app.var_bot_model.get(), 'future-realtime')
        self.assertIn('new-realtime', self.app.cmb_bot_model['values'])
        self.assertIn('custom-vision', self.app.cmb_vision_model['values'])
        self.assertNotIn('fake-secret', response.get_data(as_text=True))

    def test_failed_refresh_does_not_clear_catalog(self):
        self.app.model_catalog['realtime_models'] = ['previous-model']
        with patch.object(m, 'fetch_catalog', side_effect=options.OptionsError('offline')):
            response = self.client.post('/api/openai/models')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.app.model_catalog['realtime_models'], ['previous-model'])

    def test_voices_and_image_model_persist(self):
        with patch.object(self.app, '_save_config'):
            response = self.client.post('/api/config', json={'bot_voice': 'marin', 'bot_voice_fritz': 'cedar', 'vision_model': 'custom-model'})
        self.assertEqual(response.status_code, 200)
        config = self.client.get('/api/config').get_json()
        self.assertEqual(config['vision_model'], 'custom-model')
        self.assertEqual(config['bot_voice_fritz'], 'cedar')
        self.assertEqual(config['bot_voice'], 'marin')
        self.assertEqual(config['bot_key'], '')

    def test_browser_model_probe(self):
        with patch.object(m, 'test_realtime', return_value='confirmed') as probe:
            response = self.client.post('/api/openai/test', json={'kind': 'fritz', 'model': 'gpt-realtime', 'voice': 'cedar'})
        self.assertEqual(response.status_code, 200)
        probe.assert_called_once_with('fake-secret', 'gpt-realtime', 'cedar')
        self.assertEqual(self.client.post('/api/openai/test', json={'kind': 'bad'}).status_code, 400)

    def test_http_thread_reads_tk_through_queue(self):
        self.app.admin_server.port = 0
        self.app.admin_server.start()
        url = 'http://127.0.0.1:' + str(self.app.admin_server.server.server_port) + '/api/openai/models'
        result = {}
        def request():
            try:
                req = options.urllib.request.Request(url, data=b'', method='POST')
                with options.urllib.request.urlopen(req, timeout=4) as response:
                    result['body'] = json.load(response)
            except Exception as error:
                result['error'] = type(error).__name__
        catalog = {'all_models': ['gpt-realtime'], 'realtime_models': ['gpt-realtime'], 'vision_models': []}
        try:
            with patch.object(m, 'fetch_catalog', return_value=catalog):
                worker = threading.Thread(target=request)
                worker.start()
                deadline = time.monotonic() + 4
                while worker.is_alive() and time.monotonic() < deadline:
                    self.root.update()
                    time.sleep(0.01)
                worker.join(timeout=1)
            self.assertNotIn('error', result)
            self.assertEqual(result['body']['realtime_models'], ['gpt-realtime'])
        finally:
            self.app.admin_server.stop()
            self.app.admin_server.server.server_close()


if __name__ == '__main__':
    unittest.main()
