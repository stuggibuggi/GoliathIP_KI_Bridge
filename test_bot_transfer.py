"""Lokale Tests ohne Fritzbox-Anrufe oder OpenAI-Verbindungen."""
import json
import queue
import socket
import struct
import time
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import intercom_client as m


class TransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        with patch.object(m.App, '_load_config'), patch.object(m.AdminServer, 'start'):
            self.app = m.App(self.root)
        self.bot = m.VoiceBotEndpoint({'transfer_enabled': True}, lambda s: None)
        self.bot.running = True
        self.origin = SimpleNamespace(audio=self.bot, call={'state': 'connected'}, cmd_hangup=Mock())
        self.app.sip = self.origin
        self.app.sip_fritz = self.origin
        self.app.var_transfer_enabled.set(True)
        self.app.var_transfer_enabled_fritz.set(True)

    def tearDown(self):
        self.app._finish_bot_transfer()
        for widget in self.root.winfo_children():
            widget.destroy()

    def request(self, source='door', call_id='tool1'):
        self.bot.transfer_pending = True
        self.app._start_bot_transfer({'bot': self.bot, 'source': source, 'tool_call_id': call_id})

    def test_config_roundtrip(self):
        self.app._apply_config({'transfer_target':'**611', 'transfer_timeout':'45',
                               'transfer_target_fritz':'**612', 'transfer_enabled_fritz':False})
        c = self.app._collect_config()
        self.assertEqual(c['transfer_target'], '**611')
        self.assertEqual(c['transfer_timeout'], '45')
        self.assertFalse(c['transfer_enabled_fritz'])
        self.assertTrue(self.app._make_voice_bot().config['transfer_enabled'])

    def prepare_door_wait(self):
        self.app.var_bot_enabled.set(True)
        self.origin.call = {'state':'ringing_in', 'call_id':'door-one'}
        self.origin.cmd_answer = Mock()
        self.app.var_bot_answer_delay.set('10')

    def test_door_delay_then_answer(self):
        self.prepare_door_wait()
        with patch.object(m.time, 'monotonic', return_value=100):
            self.app._schedule_door_bot_answer({'call_id':'door-one'})
        self.origin.cmd_answer.assert_not_called()
        with patch.object(m.time, 'monotonic', return_value=109):
            self.app._check_door_bot_answer()
        self.origin.cmd_answer.assert_not_called()
        with patch.object(m.time, 'monotonic', return_value=110):
            self.app._check_door_bot_answer()
        self.origin.cmd_answer.assert_called_once()
        self.assertEqual(self.origin.cmd_answer.call_args.kwargs['expected_call_id'],'door-one')
        self.assertIsNone(self.app.door_bot_pending)

    def test_door_wait_aborted_by_human_or_new_call(self):
        self.prepare_door_wait()
        for call in (None, {'state':'connected','call_id':'door-one'},
                     {'state':'ringing_in','call_id':'door-new'}):
            self.origin.call={'state':'ringing_in','call_id':'door-one'}
            self.app._schedule_door_bot_answer({'call_id':'door-one'})
            self.origin.call=call
            self.app._check_door_bot_answer()
            self.assertIsNone(self.app.door_bot_pending)
        self.origin.cmd_answer.assert_not_called()

    def test_zero_delay_and_persistence(self):
        self.prepare_door_wait()
        self.app._apply_config({'bot_answer_delay':'0'})
        self.assertEqual(self.app._collect_config()['bot_answer_delay'],'0')
        self.app._schedule_door_bot_answer({'call_id':'door-one'})
        self.origin.cmd_answer.assert_called_once()

    def test_disabling_bot_cancels_wait(self):
        self.prepare_door_wait()
        self.app._schedule_door_bot_answer({'call_id':'door-one'})
        self.app.var_bot_enabled.set(False)
        self.app._check_door_bot_answer()
        self.origin.cmd_answer.assert_not_called()
        self.assertIsNone(self.app.door_bot_pending)

    def test_sip_thread_rejects_stale_delayed_answer(self):
        client=m.SipCallClient.__new__(m.SipCallClient)
        client.commands=queue.Queue()
        client.audio=object()
        old_audio=client.audio
        client.call={'call_id':'new-call', 'state':'ringing_in'}
        client.answer_call=Mock()
        client.cmd_answer('old-call',object())
        client._drain_commands()
        client.answer_call.assert_not_called()
        self.assertIs(client.audio,old_audio)

    def test_self_call_and_invalid_target_blocked(self):
        for target in ('**620', '620', 'sip:evil@example.com', '', '123\r\nHeader:bad'):
            self.app.var_transfer_target.set(target)
            self.request()
            self.assertIsNone(self.app.bot_transfer)
            self.assertFalse(self.bot.transfer_pending)
            self.assertFalse(json.loads(self.bot.control_queue.get()['item']['output'])['connected'])

    def test_disabled_and_invalid_timeout(self):
        self.app.var_transfer_enabled.set(False)
        self.request()
        self.assertIsNone(self.app.bot_transfer)
        self.app.var_transfer_enabled.set(True)
        for timeout in ('NaN', 'bad', '0', '121'):
            self.app.var_transfer_timeout.set(timeout)
            self.request()
            self.assertIsNone(self.app.bot_transfer)

    def test_failed_target_returns_to_same_bot(self):
        self.request('fritz')
        t = self.app.bot_transfer
        self.app._handle_transfer_event({'leg':t['leg'], 'type':'call_failed', 'reason':'486 Busy Here'})
        self.assertIsNone(self.app.bot_transfer)
        self.assertTrue(self.bot.running)
        self.assertIs(self.origin.audio, self.bot)
        self.origin.cmd_hangup.assert_not_called()
        self.assertIn('Busy', self.bot.control_queue.get()['item']['output'])

    def test_dial_waits_for_announcement_and_does_not_register(self):
        self.bot.response_in_progress = True
        self.request()
        self.app._check_bot_transfer()
        self.assertEqual(self.app.bot_transfer['state'], 'announcement')
        self.bot.response_in_progress = False
        with patch.object(m, 'SipCallClient') as cls:
            self.app._check_bot_transfer()
            self.assertFalse(cls.call_args.kwargs['register_on_run'])
            cls.return_value.cmd_call.assert_called_once_with('**610')
        t = self.app.bot_transfer
        t['deadline'] = time.monotonic() - 1
        self.app._check_bot_transfer()
        self.assertIsNone(self.app.bot_transfer)
        self.assertFalse(self.bot.transfer_pending)

    def test_connected_then_destination_hangup(self):
        self.request()
        t = self.app.bot_transfer
        t.update(state='ringing', endpoint=SimpleNamespace(info=('info',), stop=Mock()), peer=Mock())
        self.bot.connect_transfer = Mock()
        self.app._handle_transfer_event({'type':'call_connected', 'leg':t['leg']})
        self.bot.connect_transfer.assert_called_once_with(('info',))
        self.app._handle_transfer_event({'type':'call_ended', 'leg':t['leg']})
        self.origin.cmd_hangup.assert_called_once()
        self.assertIsNone(self.app.bot_transfer)

    def test_duplicate_tool_event(self):
        self.bot.on_transfer = Mock()
        event={'type':'response.function_call_arguments.done', 'name':'transfer_call', 'call_id':'one'}
        self.bot._handle_event(event)
        self.bot._handle_event(event)
        self.bot.on_transfer.assert_called_once_with(self.bot, 'one')

    def test_stale_event_does_not_affect_new_transfer(self):
        self.request()
        t = self.app.bot_transfer
        self.app._handle_transfer_event({'leg':'transfer-old','type':'call_ended'})
        self.assertIs(self.app.bot_transfer,t)
        self.origin.cmd_hangup.assert_not_called()

    def test_source_hangup_cleans_target(self):
        self.request()
        t = self.app.bot_transfer
        t.update(peer=Mock(),endpoint=Mock())
        self.origin.call = None
        self.app._check_bot_transfer()
        t['peer'].cmd_hangup.assert_called_once()
        t['endpoint'].stop.assert_called_once()
        self.assertIsNone(self.app.bot_transfer)

    def test_sip_contact_ack_and_cancel_answer_race(self):
        endpoint = m.TransferEndpoint()
        with patch.object(m,'get_local_ip',return_value='127.0.0.1'):
            client=m.SipCallClient('127.0.0.1',5060,'user','password','127.0.0.1',0,
                                   endpoint.port,queue.Queue(),endpoint,register_on_run=False)
        try:
            self.assertNotEqual(client.local_port,0)
            client.send_raw=Mock()
            client._start_outgoing_call('**610')
            c=client.call
            c['state']='cancelling'
            msg=m.parse_sip_message((f"SIP/2.0 200 OK\r\nCall-ID: {c['call_id']}\r\nCSeq: {c['invite_cseq']} INVITE\r\nTo: <sip:**610@127.0.0.1>;tag=remote\r\nContact: <sip:dialog@127.0.0.1:5060>\r\nContent-Length: 0\r\n\r\n").encode())
            client.on_invite_response(msg)
            sent=[x.args[0] for x in client.send_raw.call_args_list]
            self.assertTrue(any(x.startswith('ACK sip:dialog@') for x in sent))
            self.assertTrue(any(x.startswith('BYE sip:dialog@') for x in sent))
            self.assertIsNone(client.call)
        finally:
            client.sock.close()
            endpoint.stop()

    def test_real_udp_audio_bridge_both_directions(self):
        sockets=[]
        try:
            for _ in range(4):
                s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.bind(('127.0.0.1',0)); s.settimeout(1)
                sockets.append(s)
            a,b,caller,target=sockets
            relay=m.BridgeRelay((a,caller.getsockname(),0),(b,target.getsockname(),8),lambda s:None)
            relay.start()
            pcm=struct.pack('<h',1000)*160
            for sender,receiver,port,pt,out_pt in ((caller,target,a.getsockname(),0,8),(target,caller,b.getsockname(),8,0)):
                packet=struct.pack('!BBHII',0x80,pt,1,160,123)+m.BridgeRelay._encode(pt,pcm)
                sender.sendto(packet,port)
                data,_=receiver.recvfrom(2048)
                self.assertEqual(data[1]&127,out_pt)
                self.assertEqual(len(data),172)
                self.assertGreater(m.audioop.rms(m.BridgeRelay._decode(out_pt,data[12:]),2),500)
            relay.stop()
        finally:
            for s in sockets: s.close()


if __name__ == '__main__':
    unittest.main()
