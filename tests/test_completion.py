import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from rss_notifier.core import Engine, Model
from rss_notifier.status import snapshot
from rss_notifier.matching import Candidate
from test_matching import article
from test_notifier import FakeModel, feed


class CompletionTests(unittest.TestCase):
    def test_translation_is_batched_and_ids_validated(self):
        model = Model({'context_tokens': 8192, 'max_output_tokens': 2048})
        calls = []
        def complete(prompt, json_mode=False, max_output_tokens=None):
            calls.append(prompt)
            if 'Return JSON {"title":' in prompt:
                return json.dumps({'title': '서울 지하철 정전'})
            rows = json.loads(prompt.rsplit('\n', 1)[1])
            return json.dumps({'titles': [{'id': row['id'], 'title': '서울 지하철 정전'} for row in rows]})
        model.complete = complete
        result = model.retrieval_titles(['ソウル地下鉄の停電で運転中断'] * 7 + ['한국어 제목'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(result[-1], '한국어 제목')
        self.assertEqual(result[:7], ['서울 지하철 정전'] * 7)
        model.complete = Mock(return_value='{"titles":[{"id":0,"title":"가"},{"id":0,"title":"나"}]}')
        with self.assertRaises((ValueError, KeyError)):
            model.retrieval_titles(['ソウル地下鉄の停電'] * 2)

    def test_bad_translation_batch_splits_to_safe_single_requests(self):
        model = Model({'context_tokens': 8192, 'max_output_tokens': 2048})
        def complete(prompt, json_mode=False, max_output_tokens=None):
            if 'Return JSON {"title":' in prompt:
                return '{"title":"서울 지하철 정전"}'
            return '{"titles":[]}'
        model.complete = Mock(side_effect=complete)
        self.assertEqual(model.retrieval_titles(['ソウル地下鉄の停電'] * 2), ['서울 지하철 정전'] * 2)
        self.assertEqual(model.complete.call_count, 3)

    def test_bad_event_batch_splits_without_accepting_missing_ids(self):
        model = Model({'context_tokens': 8192, 'max_output_tokens': 2048})
        a = article('a', '서울 지하철 정전')
        def complete(prompt, json_mode=False, max_output_tokens=None):
            rows = json.loads(prompt.rsplit('\n', 1)[1])
            if len(rows) > 1:
                return '{"scores":[]}'
            return json.dumps({'scores': [{'id': rows[0]['id'], 'score': .9}]})
        model.complete = Mock(side_effect=complete)
        self.assertEqual(model.match_events(a, [Candidate(str(i), a, a, .9) for i in range(2)]), [.9, .9])
        self.assertEqual(model.complete.call_count, 3)

    def test_ollama_receives_summary_schema(self):
        model = Model({'model': 'test', 'base_url': 'http://localhost'})
        # Return a valid outer API response; inspect the outgoing schema separately.
        response = json.dumps({'done_reason': 'stop', 'message': {'content': json.dumps({'lead': 'done', 'bullets': ['fact']})}}).encode()
        with patch('rss_notifier.core.request', return_value=response) as transport:
            model.reduce('facts', 'Return summary JSON', structured=True)
        payload = transport.call_args.args[1]
        self.assertEqual(payload['format']['required'], ['lead', 'bullets'])
        self.assertEqual(payload['format']['properties']['bullets']['minItems'], 1)

    def test_native_score_map_schema_and_compact_prompt(self):
        model = Model({'context_tokens': 8192, 'max_output_tokens': 2048})
        a = article('a', '서울 지하철 정전')
        model.complete = Mock(return_value='{"scores":{"0":0.9,"1":0.1}}')
        self.assertEqual(model.match_events(a, [Candidate(str(i), a, a, .9) for i in range(2)]), [.9, .1])
        prompt, schema = model.complete.call_args.args
        self.assertEqual(schema['properties']['scores']['required'], ['0', '1'])
        self.assertNotIn('EVIDENCE', prompt.rsplit('\n', 1)[1])
        self.assertLessEqual(model.complete.call_args.kwargs['max_output_tokens'], 128)

    def test_summary_format_is_enforced(self):
        model = Model({'context_tokens': 8192, 'max_output_tokens': 2048})
        model.reduce = Mock(side_effect=['기사 근거', '{"lead":"핵심 결론","bullets":["사실 하나","사실 둘"]}'])
        text = model.summarize([article('a', '검증 기사')])
        self.assertTrue(text.startswith('핵심 결론\n- 사실 하나\n- 사실 둘'))
        self.assertTrue(model.reduce.call_args.kwargs['structured'])
        model.reduce = Mock(side_effect=['기사 근거', '{"lead":"핵심","bullets":[]}'])
        with self.assertRaises(ValueError):
            model.summarize([article('a', '검증 기사')])

    def test_structured_reduction_keeps_input_budget(self):
        model = Model({'context_tokens': 4096, 'max_output_tokens': 1024})
        calls = []
        def complete(prompt, json_mode=False):
            calls.append((prompt, json_mode))
            return '{"lead":"핵심","bullets":["근거"]}' if json_mode else '압축 사실'
        model.complete = complete
        raw = model.reduce('긴 한국어 자료' * 1000, 'JSON 요약', structured=True)
        self.assertEqual(json.loads(raw)['lead'], '핵심')
        self.assertTrue(calls[-1][1])
        self.assertTrue(all(len(prompt.encode()) <= model.budget for prompt, _ in calls))

    def test_collection_does_not_wait_for_inference(self):
        with tempfile.TemporaryDirectory() as folder:
            path = folder + '/state.db'
            model = FakeModel()
            engine = Engine(path, model)
            p = {'name': 'p', 'min_sources': 2, 'feeds': [{'id': 'a'}, {'id': 'b'}], 'dispatchers': [{'id': 'out', 'type': 'stdout'}]}
            errors = []
            def collect():
                other = Engine(path, None)
                try:
                    other.collect(p, {'a': feed('새 독립 기사', 'later'), 'b': b'<rss/>'}, now=101)
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    other.db.close()
            def infer(a, b):
                thread = threading.Thread(target=collect)
                thread.start()
                thread.join(3)
                self.assertFalse(thread.is_alive(), 'Collection blocked behind inference')
                return 1
            model.same_event = infer
            with redirect_stdout(io.StringIO()):
                engine.process(p, {'a': feed(), 'b': feed()}, now=100)
            self.assertEqual(errors, [])
            self.assertEqual(engine.db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0], 1)
            status = snapshot(path)
            self.assertEqual(status['counts']['inbox'], 1)
            self.assertTrue(any(s['stage'] == 'collection' for s in status['stages']))
            self.assertNotIn('chat', json.dumps(status))
            engine.db.close()
