import io
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from rss_notifier.core import Article, Engine, Model, parse_feed, split_message


def feed(title='정부 새 정책 발표', guid='1'):
    return f'<rss><channel><item><title>{title}</title><guid>{guid}</guid><link>https://example.com/{guid}</link><description>기사 내용</description></item></channel></rss>'.encode()


class FakeModel:
    calls = 0
    def same_event(self, a, b):
        return 1 if a.title == b.title else 0
    def summarize(self, articles):
        self.calls += 1
        return '핵심 결론\n- 근거 요약'


class Tests(unittest.TestCase):
    def test_rss_atom(self):
        self.assertEqual(parse_feed(feed(), 'a', 100)[0].published, 100)
        raw = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>x</id><title>T</title><link href="https://example.com"/><summary>S</summary><updated>2026-10-24T00:00:00Z</updated></entry></feed>'
        self.assertEqual(parse_feed(raw, 'a')[0].url, 'https://example.com')
        with self.assertRaises(ValueError):
            parse_feed(b'<!DOCTYPE rss><rss/>', 'a')

    def test_consensus_persistence_and_presets(self):
        with tempfile.TemporaryDirectory() as directory:
            model = FakeModel()
            engine = Engine(directory + '/state.db', model)
            p = {'name': 'one', 'min_sources': 2, 'feeds': [{'id': 'a'}, {'id': 'b'}],
                 'dispatchers': [{'id': 'out', 'type': 'stdout'}]}
            with redirect_stdout(io.StringIO()) as output:
                engine.process(p, {'a': feed(), 'b': feed('다른 사건')}, now=100)
                self.assertEqual(output.getvalue(), '')
                engine.process(p, {'a': feed(), 'b': feed(guid='2')}, now=101)
                self.assertEqual(model.calls, 1)
                engine.process(p, {'a': feed(), 'b': feed(guid='2')}, now=102)
                self.assertEqual(model.calls, 1)
                engine.process({**p, 'name': 'two'}, {'a': feed(), 'b': feed()}, now=103)
                self.assertEqual(model.calls, 2)
            engine.db.close()

    def test_same_source_and_restart_deduplication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + '/state.db'
            p = {'name': 'p', 'min_sources': 2, 'feeds': [{'id': 'a'}, {'id': 'b'}],
                 'dispatchers': [{'id': 'out', 'type': 'stdout'}]}
            model = FakeModel()
            model.calls = 0
            engine = Engine(path, model)
            with redirect_stdout(io.StringIO()):
                engine.process(p, {'a': feed().replace(b'</channel>', feed(guid='2').split(b'<channel>')[1].split(b'</channel>')[0] + b'</channel>'), 'b': b'<rss/>'}, now=100)
                self.assertEqual(model.calls, 0)
                engine.process(p, {'a': feed(), 'b': feed()}, now=101)
                self.assertEqual(model.calls, 1)
                engine.db.close()
                engine = Engine(path, model)
                engine.process(p, {'a': feed(guid='3'), 'b': feed(guid='4')}, now=102)
                self.assertEqual(model.calls, 1)
            engine.db.close()

    def test_retry_only_failed_dispatcher(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory + '/state.db', FakeModel())
            p = {'name': 'p', 'min_sources': 2, 'feeds': [{'id': 'a'}, {'id': 'b'}],
                 'dispatchers': [{'id': 'out', 'type': 'stdout'}, {'id': 'bad', 'type': 'broken'}]}
            class Broken:
                def send(self, *args):
                    raise RuntimeError('failure')
            with patch.dict('rss_notifier.core.DISPATCHERS', {'broken': lambda c: Broken()}):
                with redirect_stdout(io.StringIO()) as output:
                    engine.process(p, {'a': feed(), 'b': feed()}, now=100)
                    engine.process(p, {'a': feed(), 'b': feed()}, now=101)
                    self.assertEqual(output.getvalue().count('핵심 결론'), 1)
            engine.db.close()

    def test_long_unicode_output(self):
        value = '한😀' * 6000
        parts = split_message(value)
        self.assertEqual(''.join(parts), value)
        self.assertTrue(all(len(p.encode('utf-16-le')) // 2 <= 3500 for p in parts))

    def test_hierarchical_input_budget(self):
        model = Model({'context_tokens': 4096, 'max_output_tokens': 1024})
        prompts = []
        def complete(prompt):
            prompts.append(prompt)
            return '압축 근거'
        model.complete = complete
        self.assertEqual(model.reduce('긴 뉴스 자료' * 5000, '요약'), '압축 근거')
        self.assertGreater(len(prompts), 2)
        self.assertTrue(all(len(p.encode()) <= model.budget for p in prompts))

    def test_truncated_model_output_rejected(self):
        model = Model({'model': 'test', 'base_url': 'http://localhost'})
        with patch('rss_notifier.core.request', return_value=b'{"done_reason":"length","message":{"content":"partial"}}'):
            with self.assertRaises(RuntimeError):
                model.complete('test')


if __name__ == '__main__':
    unittest.main()
