"""Dashboard state and compact button alignment, without network calls."""
import time
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import intercom_client as m


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        with patch.object(m.App, '_load_config'), patch.object(m.AdminServer, 'start'):
            self.app = m.App(self.root)

    def tearDown(self):
        for token in self.root.tk.call('after', 'info'):
            self.root.after_cancel(token)
        self.root.destroy()

    def test_dashboard_is_first_and_selected(self):
        tabs = self.app.notebook.tabs()
        self.assertEqual(self.app.notebook.tab(tabs[0], 'text'), 'Dashboard')
        self.assertEqual(self.app.notebook.select(), tabs[0])

    def test_enabled_is_not_active_call(self):
        self.app.var_bot_enabled.set(True)
        self.app._refresh_dashboard()
        self.assertIn('wartet auf Anruf', self.app.dashboard_labels['door_bot']['text'])
        self.assertIn('Nicht verbunden', self.app.dashboard_labels['door']['text'])

    def test_expired_registration_is_not_green(self):
        self.app.sip = SimpleNamespace(registered=True, registered_until=time.time()-5, call=None)
        snapshot = self.app._dashboard_snapshot()
        self.assertIn('abgelaufen', snapshot['door'][0])
        self.assertNotEqual(snapshot['door'][1], 'green')

    def test_live_call_and_bot(self):
        bot = m.VoiceBotEndpoint({}, lambda message: None)
        bot.running = True
        self.app.sip = SimpleNamespace(registered=True, registered_until=time.time()+60,
                                      call={'state': 'connected'}, audio=bot)
        snapshot = self.app._dashboard_snapshot()
        self.assertIn('Im Gespraech', snapshot['door'][0])
        self.assertEqual(snapshot['door_bot'][0], 'Im Gespraech')
        self.assertIn('1 aktive', snapshot['calls'][0])

    def test_model_button_shares_left_aligned_action_row(self):
        self.app.notebook.select(3)
        self.root.update_idletasks()
        button = self.app.btn_refresh_models
        siblings = button.master.winfo_children()
        self.assertEqual(len(siblings), 3)
        self.assertTrue(all(widget.pack_info()['side'] == 'left' for widget in siblings))
        self.assertEqual(len({widget.winfo_y() for widget in siblings}), 1)


if __name__ == '__main__':
    unittest.main()
