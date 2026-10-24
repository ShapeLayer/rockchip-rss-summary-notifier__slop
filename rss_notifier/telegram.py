"""Persistent Telegram subscriptions and an independently serviced delivery queue."""
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.error

from .core import request, split_message

log = logging.getLogger(__name__)
LIMIT = 1000
SCHEMA = '''
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS tg_subscriptions(
 id INTEGER PRIMARY KEY AUTOINCREMENT, bot TEXT, chat TEXT, preset TEXT,
 UNIQUE(bot,chat,preset));
CREATE TABLE IF NOT EXISTS tg_offsets(bot TEXT PRIMARY KEY, offset INTEGER);
CREATE TABLE IF NOT EXISTS tg_jobs(
 id INTEGER PRIMARY KEY, bot TEXT, chat TEXT, subscription INTEGER,
 event TEXT, text TEXT, offset INTEGER DEFAULT 0, next_attempt REAL DEFAULT 0,
 FOREIGN KEY(subscription) REFERENCES tg_subscriptions(id) ON DELETE CASCADE,
 UNIQUE(bot,chat,subscription,event));
CREATE TABLE IF NOT EXISTS tg_completed(
 subscription INTEGER, event TEXT, PRIMARY KEY(subscription,event),
 FOREIGN KEY(subscription) REFERENCES tg_subscriptions(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS tg_cooldown(bot TEXT PRIMARY KEY,until REAL);
CREATE TABLE IF NOT EXISTS tg_last_send(bot TEXT,chat TEXT,at REAL,PRIMARY KEY(bot,chat));
'''


def connect(path):
    db = sqlite3.connect(path, timeout=30)
    db.executescript(SCHEMA)
    return db


def bot_key(token):
    return hashlib.sha256(token.encode()).hexdigest()


class TelegramAPIError(Exception):
    def __init__(self, code, retry_after=30):
        self.code, self.retry_after = code, retry_after
        super().__init__('Telegram API request failed')


class API:
    def __init__(self, token):
        self.token = token

    def call(self, method, payload):
        try:
            result = json.loads(request(f'https://api.telegram.org/bot{self.token}/{method}', payload, timeout=35))
        except urllib.error.HTTPError as exc:
            try:
                result = json.loads(exc.read())
            except (ValueError, OSError):
                raise TelegramAPIError(exc.code) from None
        if not result.get('ok'):
            raise TelegramAPIError(result.get('error_code', 500), result.get('parameters', {}).get('retry_after', 30))
        return result['result']


class Store:
    def __init__(self, path):
        self.db = connect(path)

    def enqueue(self, bot, preset, event, text):
        with self.db:
            for sid, chat in self.db.execute('SELECT id,chat FROM tg_subscriptions WHERE bot=? AND preset=?', (bot, preset)).fetchall():
                if not self.db.execute('SELECT 1 FROM tg_completed WHERE subscription=? AND event=?', (sid, event)).fetchone():
                    self.db.execute('INSERT OR IGNORE INTO tg_jobs(bot,chat,subscription,event,text) VALUES(?,?,?,?,?)',
                                    (bot, chat, sid, event, text))

    def update(self, bot, update, presets, username):
        uid = update['update_id']
        # Admission, subscription changes, response and update offset are one transaction.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            offset = self.db.execute('SELECT offset FROM tg_offsets WHERE bot=?', (bot,)).fetchone()
            if offset and uid < offset[0]:
                self.db.rollback()
                return
            msg = update.get('message', {})
            text = msg.get('text', '').split()
            chat = msg.get('chat', {}).get('id')
            response = None
            if chat is not None and text:
                chat = str(chat)
                command, _, target = text[0].partition('@')
                if target and target.lower() != username.lower():
                    command = ''
                chosen = presets if len(text) == 1 else [text[1]]
                if command in ('/start', '/stop'):
                    if len(text) > 2 or any(p not in presets for p in chosen):
                        response = '사용 가능한 프리셋: ' + ', '.join(presets)
                    elif command == '/stop':
                        for preset in chosen:
                            self.db.execute('DELETE FROM tg_subscriptions WHERE bot=? AND chat=? AND preset=?', (bot, chat, preset))
                        response = '구독을 해제했습니다.'
                    else:
                        exists = self.db.execute('SELECT 1 FROM tg_subscriptions WHERE chat=?', (chat,)).fetchone()
                        count = self.db.execute('SELECT COUNT(DISTINCT chat) FROM tg_subscriptions').fetchone()[0]
                        if not exists and count >= LIMIT:
                            response = '지금은 처리할 수 없습니다. 구독자 정원(1,000개 채팅)이 찼습니다.'
                        else:
                            for preset in chosen:
                                self.db.execute('INSERT OR IGNORE INTO tg_subscriptions(bot,chat,preset) VALUES(?,?,?)', (bot, chat, preset))
                            response = '구독했습니다: ' + ', '.join(chosen) + '\n해제: /stop'
                if response:
                    self.db.execute('INSERT INTO tg_jobs(bot,chat,event,text) VALUES(?,?,?,?)', (bot, chat, f'command:{uid}', response))
            self.db.execute('INSERT OR REPLACE INTO tg_offsets VALUES(?,?)', (bot, uid + 1))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def deliver(self, bot, api, now=None):
        now = time.time() if now is None else now
        cooldown = self.db.execute('SELECT until FROM tg_cooldown WHERE bot=?', (bot,)).fetchone()
        if cooldown and cooldown[0] > now:
            return False
        row = self.db.execute('''SELECT j.id,j.chat,j.subscription,j.event,j.text,j.offset
          FROM tg_jobs j LEFT JOIN tg_last_send l ON l.bot=j.bot AND l.chat=j.chat
          WHERE j.bot=? AND j.next_attempt<=? AND (l.at IS NULL OR l.at<=?)
          ORDER BY j.id LIMIT 1''', (bot, now, now - 1.1)).fetchone()
        if not row:
            return False
        jid, chat, sid, event, text, offset = row
        parts = split_message(text)
        try:
            api.call('sendMessage', {'chat_id': chat, 'text': parts[offset]})
        except TelegramAPIError as exc:
            with self.db:
                if exc.code == 403:
                    self.db.execute('DELETE FROM tg_subscriptions WHERE bot=? AND chat=?', (bot, chat))
                    self.db.execute('DELETE FROM tg_jobs WHERE bot=? AND chat=?', (bot, chat))
                else:
                    delay = max(1, exc.retry_after) if exc.code == 429 else 60
                    if exc.code == 429:
                        self.db.execute('INSERT OR REPLACE INTO tg_cooldown VALUES(?,?)', (bot, now + delay))
                    else:
                        self.db.execute('UPDATE tg_jobs SET next_attempt=? WHERE id=?', (now + delay, jid))
            return False
        except Exception as exc:
            log.warning('Telegram transport failed (%s)', type(exc).__name__)
            with self.db:
                self.db.execute('UPDATE tg_jobs SET next_attempt=? WHERE id=?', (now + 60, jid))
            return False
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO tg_last_send VALUES(?,?,?)', (bot, chat, now))
            if offset + 1 == len(parts):
                if sid is not None:
                    self.db.execute('INSERT OR IGNORE INTO tg_completed VALUES(?,?)', (sid, event))
                self.db.execute('DELETE FROM tg_jobs WHERE id=?', (jid,))
            else:
                self.db.execute('UPDATE tg_jobs SET offset=? WHERE id=?', (offset + 1, jid))
        return True


class SubscriptionDispatcher:
    def __init__(self, config):
        self.config = config
        self.bot = bot_key(os.environ[config.get('token_env', 'TELEGRAM_BOT_TOKEN')])

    def send(self, text, offset=0, checkpoint=None):
        c = self.config
        store = Store(c['_database'])
        try:
            store.enqueue(self.bot, c['_preset'], c['id'] + ':' + c['_event'], text)
        finally:
            store.db.close()
        if checkpoint:
            checkpoint(1)


class Worker:
    def __init__(self, path, presets):
        self.path, self.bots = path, {}
        for preset in presets:
            for config in preset['dispatchers']:
                if config['type'] == 'telegram':
                    token = os.environ[config.get('token_env', 'TELEGRAM_BOT_TOKEN')]
                    key = bot_key(token)
                    bot = self.bots.setdefault(key, {'api': API(token), 'presets': set(), 'username': None})
                    bot['presets'].add(preset['name'])
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name='telegram', daemon=True)

    def run(self):
        store = Store(self.path)
        next_poll = {}
        try:
            while not self.stop.is_set():
                for key, bot in self.bots.items():
                    if self.stop.is_set():
                        break
                    if time.monotonic() >= next_poll.get(key, 0):
                        try:
                            if bot['username'] is None:
                                bot['username'] = bot['api'].call('getMe', {})['username']
                            saved = store.db.execute('SELECT offset FROM tg_offsets WHERE bot=?', (key,)).fetchone()
                            updates = bot['api'].call('getUpdates', {'offset': saved[0] if saved else 0, 'timeout': 0, 'allowed_updates': ['message']})
                            for update in updates:
                                store.update(key, update, sorted(bot['presets']), bot['username'])
                            next_poll[key] = time.monotonic() + 2
                        except Exception as exc:
                            log.warning('Telegram polling failed (%s)', type(exc).__name__)
                            next_poll[key] = time.monotonic() + 30
                    try:
                        store.deliver(key, bot['api'])
                    except Exception as exc:
                        log.warning('Telegram queue failed (%s)', type(exc).__name__)
                self.stop.wait(.06)
        finally:
            store.db.close()
