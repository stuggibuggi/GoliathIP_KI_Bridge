"""Tests mit synthetischen Daten, ohne echte Kamerabilder an OpenAI zu senden."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import door_vision as v
import intercom_client as m
from test_bot_transfer import TransferTests


class VisionStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.vision=v.DoorVision(self.tmp.name)
        self.settings={'rtsp':'rtsp://camera', 'key':'test-key', 'model':'gpt-4.1-mini',
                       'save_images':False, 'retention_days':7}

    def tearDown(self):
        self.tmp.cleanup()

    def test_optional_image_and_record(self):
        with patch.object(v.DoorVision,'capture',return_value=b'fake-jpeg'), patch.object(v.DoorVision,'analyse',return_value='Eine Person mit einem Paket.'):
            record=self.vision.process(self.settings,'one')
        self.assertEqual(record['status'],'ok')
        self.assertIsNone(record['image'])
        self.assertEqual(len(list(Path(self.tmp.name).glob('*.jpg'))),0)
        self.assertEqual(self.vision.recent()[0]['identity'],'nicht bestimmt')
        self.settings['save_images']=True
        with patch.object(v.DoorVision,'capture',return_value=b'fake-jpeg'), patch.object(v.DoorVision,'analyse',return_value='Ein Paket ist sichtbar.'):
            record=self.vision.process(self.settings,'two')
        self.assertEqual((Path(self.tmp.name)/record['image']).read_bytes(),b'fake-jpeg')

    def test_failure_still_logged_without_credentials(self):
        with patch.object(v.DoorVision,'capture',side_effect=OSError('secret-credential')):
            record=self.vision.process(self.settings,'one')
        self.assertEqual(record['status'],'error')
        self.assertNotIn('secret',json.dumps(record))
        self.assertEqual(len(self.vision.recent()),1)

    def test_retention_only_removes_owned_expired_files(self):
        root=Path(self.tmp.name)
        old=root/('klingel_20200101_000000_'+'a'*32+'.jpg')
        fresh=root/('klingel_20200101_000000_'+'b'*32+'.json')
        unrelated=root/'private-photo.jpg'
        for path in (old,fresh,unrelated): path.write_bytes(b'data')
        for path in (old,unrelated): os.utime(path,(time.time()-10*86400,)*2)
        self.assertEqual(self.vision.cleanup(7),1)
        self.assertFalse(old.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(fresh.exists())

    def test_responses_payload_and_text_extraction(self):
        response=Mock()
        response.read.return_value=json.dumps({'output':[{'content':[{'type':'output_text','text':'Eine blaue Jacke.'}]}]}).encode()
        manager=Mock()
        manager.__enter__=Mock(return_value=response)
        manager.__exit__=Mock(return_value=False)
        with patch.object(v.urllib.request,'urlopen',return_value=manager) as request:
            text=self.vision.analyse(b'fake-jpeg','test-key','gpt-4.1-mini')
        body=json.loads(request.call_args.args[0].data)
        self.assertFalse(body['store'])
        self.assertEqual(body['input'][0]['content'][1]['type'],'input_image')
        self.assertIn('Keine Gesichtserkennung',body['instructions'])
        self.assertEqual(text,'Eine blaue Jacke.')


class VisionAppTests(TransferTests):
    def test_vision_config_roundtrip(self):
        self.app._apply_config({'vision_enabled':True,'vision_save_images':False,'vision_retention':'3'})
        config=self.app._collect_config()
        self.assertTrue(config['vision_enabled'])
        self.assertFalse(config['vision_save_images'])
        self.assertEqual(config['vision_retention'],'3')

    def test_old_call_context_not_injected(self):
        self.origin.call={'call_id':'new','state':'connected'}
        self.origin.cmd_camera_context=Mock()
        self.app._handle_door_vision_result({'call_id':'old','status':'ok','description':'Ein Paket.'})
        self.origin.cmd_camera_context.assert_not_called()
        self.app._handle_door_vision_result({'call_id':'new','status':'ok','description':'Eine rote Jacke.'})
        self.origin.cmd_camera_context.assert_called_once_with('new','Eine rote Jacke.')

    def test_initial_context_only_current_door_call(self):
        self.origin.call={'call_id':'one','state':'ringing_in'}
        self.app.door_vision_results['one']={'status':'ok','description':'Ein gelbes Paket.'}
        self.assertIn('gelbes Paket',self.app._make_voice_bot().config['instructions'])
        self.assertNotIn('gelbes Paket',self.app._make_voice_bot('fritz').config['instructions'])

    def test_indoor_calls_do_not_start_analysis(self):
        self.app.var_vision_enabled.set(True)
        with patch.object(m.threading,'Thread') as thread:
            self.app._start_door_vision({'caller':'<sip:9901%230@host>','call_id':'one'})
            thread.assert_not_called()
            self.app._start_door_vision({'caller':'<sip:8001@host>','call_id':'two'})
            thread.assert_called_once()

    def test_camera_context_does_not_trigger_response_or_untrusted_actions(self):
        self.bot.add_camera_context('Eine Jacke.')
        event=self.bot.control_queue.get_nowait()
        self.assertEqual(event['item']['role'],'system')
        self.assertEqual(event['item']['type'],'message')
        self.assertIn('keine Anweisungen',event['item']['content'][0]['text'])


if __name__=='__main__': unittest.main()
