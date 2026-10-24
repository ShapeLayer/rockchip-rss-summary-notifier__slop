"""Live-model pipeline smoke test, using an isolated DB and recording Telegram transport.

Run inside the application container: python /tmp/smoke.py /app/configs/server.toml
No fixture messages are sent to Telegram or production subscribers.
"""
import io
import os
import sys
import tempfile
import time
import tomllib
from contextlib import redirect_stdout
from xml.sax.saxutils import escape

sys.path.insert(0, os.getcwd())

from rss_notifier.core import Engine, Model
from rss_notifier.telegram import Store, bot_key


class RecordingAPI:
    def __init__(self):
        self.messages = []

    def call(self, method, payload):
        assert method == 'sendMessage'
        self.messages.append(payload['text'])
        return {'message_id': len(self.messages)}


def main():
    with open(sys.argv[1], 'rb') as file:
        model = Model(tomllib.load(file)['model'])
    os.environ['SMOKE_TELEGRAM_TOKEN'] = 'isolated-smoke-token'
    bot = bot_key(os.environ['SMOKE_TELEGRAM_TOKEN'])
    title = '검증용 A시 전기버스 20대 도입, 다음 달 3개 노선 투입'
    body = '검증용 자료: A시가 전기버스 20대를 도입했다. 다음 달부터 3개 노선에 투입하며 운임은 유지한다.'
    feeds = {}
    for source in ('a', 'b', 'c', 'weather'):
        t = title if source != 'weather' else '검증용 A시 내일 강풍 예보, 야외 활동 주의'
        text = body if source != 'weather' else '검증용 자료: A시에 내일 강풍이 예보됐다.'
        feeds[source] = f'<rss><channel><item><guid>{source}</guid><title>{escape(t)}</title><description>{escape(text)}</description></item></channel></rss>'.encode()
    preset = {'name': 'smoke', 'min_sources': 3, 'similarity_threshold': .8,
              'feeds': [{'id': source} for source in feeds],
              'dispatchers': [{'id': 'console', 'type': 'stdout'},
                              {'id': 'telegram', 'type': 'telegram', 'token_env': 'SMOKE_TELEGRAM_TOKEN'}]}
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as directory:
        path = directory + '/smoke.db'
        engine = Engine(path, model)
        store = Store(path)
        try:
            store.update(bot, {'update_id': 1, 'message': {'chat': {'id': 1}, 'text': '/start'}}, ['smoke'], 'smokebot')
            with store.db:
                store.db.execute('DELETE FROM tg_jobs WHERE subscription IS NULL')
            with redirect_stdout(io.StringIO()) as output:
                engine.process(preset, feeds)
            assert engine.db.execute('SELECT COUNT(*) FROM articles').fetchone()[0] == 4
            assert engine.db.execute('SELECT COUNT(*) FROM clusters WHERE sent=1').fetchone()[0] == 1
            assert engine.db.execute('SELECT COUNT(*) FROM clusters').fetchone()[0] == 2, 'Unrelated event incorrectly merged'
            api = RecordingAPI()
            for i in range(20):
                store.deliver(bot, api, now=time.time() + i * 2)
                if store.db.execute('SELECT COUNT(*) FROM tg_jobs').fetchone()[0] == 0:
                    break
            assert api.messages and store.db.execute('SELECT COUNT(*) FROM tg_completed').fetchone()[0] == 1
            assert store.db.execute('SELECT COUNT(*) FROM tg_jobs').fetchone()[0] == 0
            assert '\n- ' in output.getvalue()
            assert ''.join(api.messages).strip() == output.getvalue().strip()
            print('PASS: RSS -> candidate retrieval -> live AI -> structured summary -> stdout + recorded Telegram delivery')
            print('elapsed_seconds=', round(time.monotonic() - started, 1))
        finally:
            store.db.close()
            engine.db.close()


if __name__ == '__main__':
    main()
