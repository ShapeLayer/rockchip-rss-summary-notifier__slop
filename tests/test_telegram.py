import tempfile
import unittest
from unittest.mock import Mock

from rss_notifier.telegram import Store, TelegramAPIError


def update(uid, chat, command):
    return {'update_id': uid, 'message': {'chat': {'id': chat}, 'text': command}}


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + '/state.db'
        self.store = Store(self.path)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def command(self, uid, chat, text):
        self.store.update('bot', update(uid, chat, text), ['news', 'tech'], 'newsbot')

    def test_limit_1000_and_existing_subscription(self):
        for i in range(1000):
            self.command(i, i, '/start news')
        self.command(1000, 1000, '/start')
        self.assertEqual(self.store.db.execute('SELECT COUNT(DISTINCT chat) FROM tg_subscriptions').fetchone()[0], 1000)
        self.assertIn('지금은 처리할 수 없습니다', self.store.db.execute('SELECT text FROM tg_jobs ORDER BY id DESC LIMIT 1').fetchone()[0])
        self.command(1001, 0, '/start')
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM tg_subscriptions WHERE chat='0'").fetchone()[0], 2)
        self.command(1002, 0, '/stop')
        self.command(1003, 1000, '/start news')
        self.assertEqual(self.store.db.execute('SELECT COUNT(DISTINCT chat) FROM tg_subscriptions').fetchone()[0], 1000)

    def test_preset_scope_and_replay(self):
        self.command(1, 123, '/start@newsbot news')
        self.command(1, 123, '/start@newsbot news')
        self.command(2, 456, '/start@otherbot')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM tg_subscriptions').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM tg_jobs').fetchone()[0], 1)
        self.store.enqueue('bot', 'tech', 'event', 'tech summary')
        self.store.enqueue('bot', 'news', 'event', 'news summary')
        self.command(3, 123, '/stop news')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM tg_jobs WHERE subscription IS NOT NULL').fetchone()[0], 0)

    def test_restart_resume_and_no_duplicate_enqueue(self):
        self.command(1, 123, '/start news')
        self.store.db.execute('DELETE FROM tg_jobs')
        self.store.db.commit()
        self.store.enqueue('bot', 'news', 'event', 'a' * 7001)
        api = Mock()
        self.assertTrue(self.store.deliver('bot', api, now=100))
        self.store.db.close()
        self.store = Store(self.path)
        self.assertFalse(self.store.deliver('bot', api, now=100.5))
        self.assertTrue(self.store.deliver('bot', api, now=102))
        self.assertTrue(self.store.deliver('bot', api, now=104))
        self.store.enqueue('bot', 'news', 'event', 'a' * 7001)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM tg_jobs').fetchone()[0], 0)
        self.assertEqual(api.call.call_count, 3)

    def test_rate_limit_and_blocked_chat(self):
        self.command(1, 123, '/start news')
        api = Mock()
        api.call.side_effect = TelegramAPIError(429, 10)
        self.assertFalse(self.store.deliver('bot', api, now=100))
        self.assertFalse(self.store.deliver('bot', api, now=101))
        self.assertEqual(api.call.call_count, 1)
        api.call.side_effect = TelegramAPIError(403)
        self.store.deliver('bot', api, now=111)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM tg_subscriptions').fetchone()[0], 0)

    def test_limit_shared_across_bots_and_persistent_offset(self):
        with self.store.db:
            self.store.db.executemany('INSERT INTO tg_subscriptions(bot,chat,preset) VALUES(?,?,?)', [('other', str(i), 'news') for i in range(1000)])
        self.command(1, 2000, '/start')
        self.assertFalse(self.store.db.execute("SELECT 1 FROM tg_subscriptions WHERE chat='2000'").fetchone())
        self.assertEqual(self.store.db.execute("SELECT offset FROM tg_offsets WHERE bot='bot'").fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
