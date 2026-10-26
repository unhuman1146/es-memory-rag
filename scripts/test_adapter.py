"""Adapter tests: offline, no network, no API key, no shared state.

The embedding backend is a deterministic stub, the vector store is a temporary
SQLite file and the memory store is a fresh temporary database per test.
"""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

from memory_engine import MemoryEngine, load_config

from adapters import embedding as embedding_mod
from adapters.embedding import (EmbeddingError, HashingEmbedding, OllamaEmbedding,
                                 SiliconFlowEmbedding, build_embedding, read_private_config,
                                 resolve_api_key, tokenize)
from adapters.rag_adapter import RAGAdapter, chunk_text, projection_hash
from adapters.rerank import LexicalReranker
from adapters.sync import sync_once, sync_report_line
from adapters.vector_store import SqliteVectorStore, cosine

NOW = 1_700_000_000.0
DUMMY_KEY = 'sk-test-DUMMY-KEY-must-never-leak-0123456789'


def event(eid='e1', mid='m1', weight=50, visibility='shared', sender='qq:10001',
          profile='standard', content='new fact'):
    return {'id': eid, 'kind': 'human_message', 'peer_id': 'agent:private:%s:sender' % sender.replace(':', ''),
            'sender_id': sender, 'visibility': visibility, 'content': content, 'actions': [{
                'kind': 'add', 'id': mid, 'weight': weight, 'profile': profile, 'body': '原始详细叙述。' * 40,
                'facts': [{'subject': 'qq:10001', 'predicate': 'likes', 'object': '甜奶茶', 'negated': True,
                           'time': None, 'source_ids': [eid], 'core': True}]}]}


class FakeReranker:
    backend = 'fake'

    def __init__(self, order=None):
        self.order = order
        self.calls = 0

    def rerank(self, query, documents):
        self.calls += 1
        idx = list(range(len(documents)))
        if self.order:
            idx = self.order
        return [{'index': i, 'score': 1.0 - 0.1 * n} for n, i in enumerate(idx)]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.environ.get('USERPROFILE')
        os.environ['USERPROFILE'] = self.tmp.name
        os.environ['HOME'] = self.tmp.name
        self.cfg = load_config()
        self.engine = MemoryEngine(Path(self.tmp.name) / 'mem.db', self.cfg)
        self.store = SqliteVectorStore(Path(self.tmp.name) / 'index.db')
        self.emb = HashingEmbedding(dim=64)
        self.adapter = RAGAdapter(self.emb, self.store, engine=self.engine)

    def tearDown(self):
        self.store.close()
        self.engine.close()
        self.tmp.cleanup()
        if self.home is not None:
            os.environ['USERPROFILE'] = self.home
        else:
            os.environ.pop('USERPROFILE', None)

    def add(self, **kwargs):
        e = event(**kwargs)
        self.engine.process_event(e, NOW)
        return e


# ---------------------------------------------------------------- embeddings


class EmbeddingTests(Base):
    def test_hashing_is_deterministic(self):
        a = self.emb.embed(['用户在杭州做后端开发'])[0]
        b = self.emb.embed(['用户在杭州做后端开发'])[0]
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)

    def test_hashing_dim_is_configurable(self):
        self.assertEqual(len(HashingEmbedding(dim=32).embed(['x'])[0]), 32)

    def test_tokenize_splits_cjk_per_character(self):
        self.assertEqual(tokenize('杭州后端'), ['杭', '州', '后', '端'])
        self.assertEqual(tokenize('hello world'), ['hello', 'world'])

    def test_hashing_separates_related_from_unrelated_chinese(self):
        vecs = self.emb.embed(['用户在杭州做后端开发', '他在杭州写后端服务', '今天午饭吃了拉面'])
        self.assertGreater(cosine(vecs[0], vecs[1]), cosine(vecs[0], vecs[2]))

    def test_missing_key_raises(self):
        with self.assertRaises(EmbeddingError):
            SiliconFlowEmbedding(api_key=None)

    def test_key_comes_from_environment(self):
        os.environ['SILICONFLOW_API_KEY'] = DUMMY_KEY
        try:
            client = SiliconFlowEmbedding()
            self.assertTrue(client._key == DUMMY_KEY)
        finally:
            os.environ.pop('SILICONFLOW_API_KEY', None)

    def test_key_comes_from_private_config_outside_the_repo(self):
        cfg_dir = Path(self.tmp.name) / '.es-memory-rag'
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / 'config.json').write_text(json.dumps({'siliconflow_api_key': DUMMY_KEY}), encoding='utf-8')
        self.assertEqual(resolve_api_key(), DUMMY_KEY)

    def test_private_config_is_not_read_from_the_repository(self):
        self.assertFalse((Path(__file__).resolve().parent.parent / 'config.json').exists())
        self.assertEqual(read_private_config(), {})

    def test_siliconflow_embed_calls_the_endpoint(self):
        seen = {}

        def fake_post(url, payload, headers, timeout=None):
            seen['url'] = url
            seen['payload'] = payload
            seen['headers'] = headers
            return {'data': [{'index': 1, 'embedding': [0.0] * 1024},
                             {'index': 0, 'embedding': [1.0] * 1024}]}

        original = embedding_mod._post_json
        embedding_mod._post_json = fake_post
        try:
            client = SiliconFlowEmbedding(model='BAAI/bge-m3', api_key=DUMMY_KEY)
            vecs = client.embed(['a', 'b'])
        finally:
            embedding_mod._post_json = original
        self.assertEqual(seen['url'], 'https://api.siliconflow.cn/v1/embeddings')
        self.assertEqual(seen['payload']['model'], 'BAAI/bge-m3')
        self.assertEqual(seen['headers']['Authorization'], 'Bearer ' + DUMMY_KEY)
        self.assertEqual(vecs[0][0], 1.0)  # index order respected
        self.assertEqual(vecs[1][0], 0.0)

    def test_siliconflow_rejects_a_wrong_dimension(self):
        original = embedding_mod._post_json
        embedding_mod._post_json = lambda *a, **k: {'data': [{'index': 0, 'embedding': [0.0] * 512}]}
        try:
            client = SiliconFlowEmbedding(api_key=DUMMY_KEY, dim=1024)
            with self.assertRaises(EmbeddingError):
                client.embed(['a'])
        finally:
            embedding_mod._post_json = original

    def test_error_message_never_contains_the_key(self):
        def boom(*args, **kwargs):
            raise urllib.error.HTTPError('https://api.siliconflow.cn/v1/embeddings', 402,
                                         'Payment Required', {}, io.BytesIO(b'{"message":"balance"}'))
        original = embedding_mod._opener
        embedding_mod._opener = boom
        try:
            client = SiliconFlowEmbedding(api_key=DUMMY_KEY)
            with self.assertRaises(EmbeddingError) as ctx:
                client.embed(['a'])
        finally:
            embedding_mod._opener = original
        message = str(ctx.exception)
        self.assertIn('402', message)
        self.assertNotIn(DUMMY_KEY, message)
        self.assertNotIn('sk-test', message)

    def test_ollama_backend_needs_no_key(self):
        client = OllamaEmbedding(model='bge-m3')
        self.assertEqual(client.base_url, 'http://127.0.0.1:11434')
        original = embedding_mod._post_json
        embedding_mod._post_json = lambda *a, **k: {'embeddings': [[0.5] * 1024]}
        try:
            self.assertEqual(len(client.embed(['x'])[0]), 1024)
        finally:
            embedding_mod._post_json = original

    def test_build_embedding_selects_the_backend(self):
        self.assertIsInstance(build_embedding(provider='ollama', config={}), OllamaEmbedding)
        with self.assertRaises(EmbeddingError):
            build_embedding(provider='nope', config={})


# ------------------------------------------------------------- vector store


class VectorStoreTests(Base):
    def test_upsert_and_count(self):
        self.store.upsert('m1', 1, ['hello world'], [self.emb.embed(['hello world'])[0]], {'owner': 'qq:1'}, 'h1')
        self.assertEqual(self.store.count('m1'), 1)

    def test_delete_removes_every_revision(self):
        vec = self.emb.embed(['x'])[0]
        self.store.upsert('m1', 1, ['x'], [vec], {}, 'h1')
        self.store.upsert('m1', 2, ['x'], [vec], {}, 'h2')
        self.store.delete('m1')
        self.assertEqual(self.store.count('m1'), 0)

    def test_search_orders_by_similarity(self):
        vecs = self.emb.embed(['用户在杭州做后端开发', '今天午饭吃了拉面'])
        self.store.upsert('a', 1, ['用户在杭州做后端开发'], [vecs[0]], {}, 'h1')
        self.store.upsert('b', 1, ['今天午饭吃了拉面'], [vecs[1]], {}, 'h2')
        hits = self.store.search(vecs[0], limit=2)
        self.assertEqual(hits[0]['memory_id'], 'a')

    def test_acl_prefilter_drops_other_peoples_private_rows(self):
        vecs = self.emb.embed(['x', 'y'])
        self.store.upsert('p', 1, ['x'], [vecs[0]], {'owner': 'qq:10001', 'visibility': 'private'}, 'h1')
        self.store.upsert('s', 1, ['y'], [vecs[1]], {'owner': 'qq:10002', 'visibility': 'shared'}, 'h2')
        hits = self.store.search(vecs[0], limit=10, acl_filter={'owner': 'qq:10002'})
        self.assertEqual([h['memory_id'] for h in hits], ['s'])

    def test_vector_roundtrip_keeps_precision(self):
        vec = [0.1, -0.2, 0.3]
        self.store.upsert('m', 1, ['t'], [vec], {}, 'h')
        stored = self.store.chunks_of('m')[0]['vector']
        for a, b in zip(stored, vec):
            self.assertAlmostEqual(a, b, places=6)


# ------------------------------------------------------------------ adapter


class AdapterTests(Base):
    def test_upsert_writes_chunks(self):
        out = self.adapter.upsert('m1', 1, '用户在杭州做后端开发', {'weight': 50})
        self.assertTrue(out['ok'])
        self.assertEqual(self.adapter.count('m1'), out['chunks'])

    def test_weight_only_change_reuses_the_embedding(self):
        self.adapter.upsert('m1', 1, '同一段投影文本', {'weight': 50})
        calls = self.emb.calls
        out = self.adapter.upsert('m1', 2, '同一段投影文本', {'weight': 80})
        self.assertTrue(out['reused_embedding'])
        self.assertEqual(self.emb.calls, calls)

    def test_changed_projection_re_embeds(self):
        self.adapter.upsert('m1', 1, '第一版投影', {'weight': 50})
        calls = self.emb.calls
        out = self.adapter.upsert('m1', 2, '第二版投影', {'weight': 50})
        self.assertFalse(out['reused_embedding'])
        self.assertGreater(self.emb.calls, calls)

    def test_delete_removes_the_vectors(self):
        self.adapter.upsert('m1', 1, 'x', {})
        self.adapter.delete('m1')
        self.assertEqual(self.adapter.count('m1'), 0)

    def test_search_drops_deleted_memories(self):
        self.add(mid='m1')
        self.adapter.upsert('m1', 1, self.engine.row('m1')['text'], {})
        self.engine.db.execute("UPDATE memories SET deleted_at=? WHERE id='m1'", (NOW,))
        self.engine.db.commit()
        self.assertEqual(self.adapter.search('原始详细叙述', {'requester': 'qq:10001'}, 5), [])

    def test_search_drops_purged_memories(self):
        self.add(mid='m1')
        self.adapter.upsert('m1', 1, self.engine.row('m1')['text'], {})
        self.engine.db.execute("UPDATE memories SET purged=1 WHERE id='m1'")
        self.engine.db.commit()
        self.assertEqual(self.adapter.search('原始详细叙述', {'requester': 'qq:10001'}, 5), [])

    def test_search_drops_archived_memories(self):
        self.add(mid='m1')
        self.adapter.upsert('m1', 1, self.engine.row('m1')['text'], {})
        self.engine.db.execute("UPDATE memories SET profile='archived' WHERE id='m1'")
        self.engine.db.commit()
        self.assertEqual(self.adapter.search('原始详细叙述', {'requester': 'qq:10001'}, 5), [])

    def test_search_drops_superseded_revisions(self):
        self.add(mid='m1')
        self.adapter.upsert('m1', 1, self.engine.row('m1')['text'], {})
        self.engine.db.execute("UPDATE memories SET revision=revision+1 WHERE id='m1'")
        self.engine.db.commit()
        self.assertEqual(self.adapter.search('原始详细叙述', {'requester': 'qq:10001'}, 5), [])

    def test_search_drops_other_peoples_private_memories(self):
        self.add(mid='m1', visibility='private', sender='qq:10001')
        self.adapter.upsert('m1', 1, self.engine.row('m1')['text'], {'visibility': 'private', 'owner': 'qq:10001'})
        self.assertEqual(self.adapter.search('原始详细叙述', {'requester': 'qq:10002'}, 5), [])
        hits = self.adapter.search('原始详细叙述', {'requester': 'qq:10001'}, 5)
        self.assertEqual(len(hits), 1)

    def test_search_blends_relevance_and_weight(self):
        self.add(mid='m1', weight=50)
        row = self.engine.row('m1')
        self.adapter.upsert('m1', 1, row['text'], {})
        hit = self.adapter.search(row['text'], {'requester': 'qq:10001'}, 1)[0]
        self.assertAlmostEqual(hit['score'], 0.8 * hit['relevance'] + 0.2 * (50.0 / 101.0), places=6)

    def test_rerank_without_a_model_reports_degraded(self):
        self.add(mid='m1')
        row = self.engine.row('m1')
        self.adapter.upsert('m1', 1, row['text'], {})
        cands = self.adapter.search(row['text'], {'requester': 'qq:10001'}, 3)
        out = self.adapter.rerank('查询', cands)
        self.assertEqual(out['status'], 'degraded')
        self.assertIn('reason', out)

    def test_rerank_with_a_model_reports_ok(self):
        self.add(mid='m1')
        row = self.engine.row('m1')
        self.adapter.upsert('m1', 1, row['text'], {})
        cands = self.adapter.search(row['text'], {'requester': 'qq:10001'}, 3)
        self.adapter.reranker = FakeReranker()
        out = self.adapter.rerank('查询', cands)
        self.assertEqual(out['status'], 'ok')
        self.assertEqual(len(out['candidates']), len(cands))

    def test_rerank_failure_is_reported_not_swallowed(self):
        class Boom:
            backend = 'boom'

            def rerank(self, query, documents):
                raise RuntimeError('provider down')
        self.add(mid='m1')
        row = self.engine.row('m1')
        self.adapter.upsert('m1', 1, row['text'], {})
        cands = self.adapter.search(row['text'], {'requester': 'qq:10001'}, 3)
        self.adapter.reranker = Boom()
        out = self.adapter.rerank('查询', cands)
        self.assertEqual(out['status'], 'degraded')
        self.assertIn('provider down', out['reason'])

    def test_lexical_reranker_is_marked_degraded(self):
        self.add(mid='m1')
        row = self.engine.row('m1')
        self.adapter.upsert('m1', 1, row['text'], {})
        cands = self.adapter.search(row['text'], {'requester': 'qq:10001'}, 3)
        self.adapter.reranker = LexicalReranker()
        out = self.adapter.rerank('查询', cands)
        self.assertEqual(out['status'], 'degraded')
        self.assertEqual(out['reranker_backend'], 'lexical')

    def test_chunking_splits_long_text(self):
        chunks = chunk_text('句子。' * 400, chunk_chars=100)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 100 for c in chunks))

    def test_projection_hash_ignores_weight(self):
        self.assertEqual(projection_hash('文本'), projection_hash('文本'))
        self.assertNotEqual(projection_hash('文本'), projection_hash('别的文本'))


# --------------------------------------------------------------------- sync


class SyncTests(Base):
    def test_sync_upserts_and_acknowledges(self):
        self.add(mid='m1')
        self.assertEqual(len(self.engine.outbox()), 1)
        report = sync_once(self.engine, self.adapter)
        self.assertEqual(report['acked'], 1)
        self.assertEqual(report['failed'], 0)
        self.assertEqual(self.engine.outbox(), [])
        self.assertGreater(self.adapter.count('m1'), 0)

    def test_sync_never_acknowledges_without_execution(self):
        self.add(mid='m1')

        class Failing:
            def upsert(self, *a, **k):
                raise RuntimeError('index unavailable')

            def delete(self, *a, **k):
                raise RuntimeError('index unavailable')
        report = sync_once(self.engine, Failing())
        self.assertEqual(report['failed'], 1)
        self.assertEqual(report['acked'], 0)
        row = self.engine.outbox()[0]
        self.assertEqual(row['ack'], 0)
        self.assertEqual(row['attempts'], 1)

    def test_failed_event_dead_letters_after_max_attempts(self):
        self.cfg['outbox_max_attempts'] = 1
        self.add(mid='m1')

        class Failing:
            def upsert(self, *a, **k):
                raise RuntimeError('index unavailable')

            def delete(self, *a, **k):
                raise RuntimeError('index unavailable')
        report = sync_once(self.engine, Failing())
        self.assertEqual(report['dead'], 1)
        self.assertEqual(len(self.engine.dead_letters()), 1)

    def test_superseded_event_is_acknowledged_as_skipped(self):
        self.add(mid='m1')
        self.engine.db.execute("UPDATE memories SET revision=revision+1 WHERE id='m1'")
        self.engine.db.commit()
        report = sync_once(self.engine, self.adapter)
        self.assertEqual(report['skipped'], 1)
        self.assertEqual(report['acked'], 1)
        self.assertEqual(self.adapter.count('m1'), 0)

    def test_delete_event_removes_the_vectors(self):
        self.add(mid='m1')
        sync_once(self.engine, self.adapter)
        self.assertGreater(self.adapter.count('m1'), 0)
        self.engine.db.execute("UPDATE memories SET profile='archived', revision=revision+1 WHERE id='m1'")
        self.engine.db.commit()
        self.engine._queue('m1')
        self.engine.db.commit()
        report = sync_once(self.engine, self.adapter)
        self.assertEqual(report['acked'], 1)
        self.assertEqual(self.adapter.count('m1'), 0)

    def test_report_line_is_readable(self):
        self.assertIn('processed=', sync_report_line({'processed': 1, 'acked': 1, 'skipped': 0,
                                                       'failed': 0, 'dead': 0, 'items': []}))


if __name__ == '__main__':
    unittest.main(verbosity=2)
