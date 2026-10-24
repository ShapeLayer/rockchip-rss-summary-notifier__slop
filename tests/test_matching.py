import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock

from rss_notifier.core import Article, Engine, Model
from rss_notifier.matching import Candidate, CandidateIndex
from test_notifier import FakeModel, feed


def article(key, title, source='s'):
    return Article(key, source, title, 'https://example.com/' + key, '', 100)


class MatchingTests(unittest.TestCase):
    def test_korean_paraphrase_and_distinct_event_cap(self):
        index = CandidateIndex()
        anchor = article('a', '서울 지하철 2호선 정전으로 열차 운행 중단')
        for i in range(30):
            index.add(article(str(i), anchor.title), 'same', anchor)
        index.add(article('unrelated', '제주 내일 강풍 예보'), 'weather', article('weather', '제주 강풍'))
        found = index.search(article('new', '정전 발생한 서울 지하철 2호선…운행 멈춰'))
        self.assertEqual([c.cluster for c in found], ['same'])

    def test_bounded_candidates_on_large_backlog(self):
        index = CandidateIndex()
        for i in range(300):
            a = article(str(i), f'서울 지하철 {i}호선 운행 중단')
            index.add(a, a.id, a)
        self.assertEqual(len(index.search(article('new', '서울 지하철 운행 차질'), top_k=6)), 6)
        self.assertEqual(index.search(article('z', '화성 탐사선 착륙 성공')), [])

    def test_cross_language_normalization_retains_original_evidence(self):
        index = CandidateIndex()
        jp = article('jp', 'ソウル地下鉄の停電で運転中断')
        index.add(jp, 'event', jp, '서울 지하철 정전 운행 중단')
        found = index.search(article('ko', '서울 지하철 정전으로 운행 중단'))
        self.assertEqual(found[0].anchor.title, jp.title)
        self.assertEqual(found[0].cluster, 'event')

    def test_batched_model_validation_and_budget(self):
        model = Model({'context_tokens': 4096, 'max_output_tokens': 1024})
        anchor = article('a', '정부 새 정책 발표')
        candidates = [Candidate(str(i), anchor, anchor, .9) for i in range(6)]
        calls = []
        def complete(prompt, json_mode=False, max_output_tokens=None):
            calls.append(prompt)
            ids = json.loads(prompt.rsplit('\n', 1)[1])
            return json.dumps({'scores': [{'id': c['id'], 'score': .9} for c in ids]})
        model.complete = complete
        self.assertEqual(model.match_events(anchor, candidates), [.9] * 6)
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(len(p.encode()) <= model.budget for p in calls))
        model.complete = Mock(return_value='{"scores":[{"id":0,"score":true}]}')
        with self.assertRaises(ValueError):
            model.match_events(anchor, candidates[:1])

    def test_prompt_splits_with_small_budget_and_rejects_duplicates(self):
        model = Model({'context_tokens': 4096, 'max_output_tokens': 1024})
        a = article('a', '서울 지하철 정전' * 30)
        candidates = [Candidate(str(i), a, a, .9) for i in range(6)]
        calls = []
        def complete(prompt, json_mode=False, max_output_tokens=None):
            calls.append(prompt)
            rows = json.loads(prompt.rsplit('\n', 1)[1])
            return json.dumps({'scores': [{'id': row['id'], 'score': .1} for row in rows]})
        model.complete = complete
        self.assertEqual(model.match_events(a, candidates), [.1] * 6)
        self.assertGreater(len(calls), 1)
        self.assertTrue(all(len(p.encode()) <= model.budget for p in calls))
        model.complete = Mock(return_value='{"scores":[{"id":0,"score":0.9},{"id":0,"score":0.8}]}')
        short = article('b', '짧은 제목')
        with self.assertRaises(ValueError):
            model.match_events(short, [Candidate(str(i), short, short, .8) for i in range(2)])

    def test_ingest_is_durable_before_inference_and_failure_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + '/state.db'
            engine = Engine(path, FakeModel())
            preset = {'name': 'p', 'min_sources': 2, 'feeds': [{'id': 'a'}, {'id': 'b'}],
                      'max_articles_per_pass': 1, 'dispatchers': [{'id': 'out', 'type': 'stdout'}]}
            engine.collect(preset, {'a': feed(), 'b': feed()}, now=100)
            self.assertEqual(engine.db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0], 2)
            with redirect_stdout(io.StringIO()):
                engine.process(preset, now=100, collect_feeds=False)
                self.assertEqual(engine.db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0], 1)
                engine.db.close()
                engine = Engine(path, FakeModel())
                engine.process(preset, now=101, collect_feeds=False)
                self.assertEqual(engine.db.execute('SELECT sent FROM clusters').fetchone()[0], 1)
            engine.db.close()

    def test_ai_failure_preserves_pending_and_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            model = FakeModel()
            engine = Engine(directory + '/state.db', model)
            p = {'name': 'p', 'min_sources': 2, 'feeds': [{'id': 'a'}, {'id': 'b'}], 'dispatchers': [{'id': 'out', 'type': 'stdout'}]}
            model.same_event = Mock(side_effect=RuntimeError('offline'))
            with redirect_stdout(io.StringIO()):
                engine.process(p, {'a': feed(), 'b': feed()}, now=100)
                self.assertEqual(engine.db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0], 1)
                engine.process(p, now=101, collect_feeds=False)
                self.assertEqual(model.same_event.call_count, 1)
                model.same_event = Mock(return_value=1)
                engine.process(p, now=161, collect_feeds=False)
                self.assertEqual(engine.db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0], 0)
            engine.db.close()
