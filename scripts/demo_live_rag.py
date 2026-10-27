"""Live acceptance demo: a real provider, a real index, a real reranker.

This script is **not** part of CI -- it needs a provider key and network access.
It walks the acceptance gates from ``references/integration.md`` that unit
tests cannot cover:

* a paraphrase query hits the right memory;
* a high-weight but unrelated memory is not injected;
* the reranker really answers (``status`` is ``ok``, not ``degraded``);
* after a forced delete the index count is zero and the source-side filter
  stops returning the old hit immediately.

Run it from the package root::

    python scripts/demo_live_rag.py

The key is read from ``SILICONFLOW_API_KEY`` or from
``~/.es-memory-rag/config.json``. It is never printed and never stored in the
index.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

from memory_engine import MemoryEngine, load_config

from adapters.embedding import build_embedding, read_private_config
from adapters.rag_adapter import RAGAdapter
from adapters.rerank import SiliconFlowReranker
from adapters.sync import sync_once, sync_report_line
from adapters.vector_store import SqliteVectorStore

NOW = 1_700_000_000.0
# Calibrated for bge-m3 on this kind of short Chinese fact projection.
# Different models need their own value -- do not copy it blindly.
MIN_RELEVANCE = 0.45

MEMORIES = [
    {'id': 'm_hangzhou', 'weight': 60, 'visibility': 'shared',
     'facts': [{'subject': '用户', 'predicate': '常住', 'object': '杭州', 'negated': False,
                'time': None, 'source_ids': ['e1'], 'core': True},
               {'subject': '用户', 'predicate': '职业', 'object': '后端开发工程师', 'negated': False,
                'time': None, 'source_ids': ['e1'], 'core': True}]},
    # High weight, completely different topic: it must not be injected for a
    # question about where the user works. 89 is the highest weight the intake
    # will accept -- W>=90 belongs to trusted admin operations, so a fixture
    # cannot (and should not) get there through an event.
    {'id': 'm_milktea', 'weight': 89, 'visibility': 'shared',
     'facts': [{'subject': '用户', 'predicate': '不喝', 'object': '甜奶茶', 'negated': True,
                'time': None, 'source_ids': ['e2'], 'core': True}]},
    {'id': 'm_stack', 'weight': 70, 'visibility': 'shared',
     'facts': [{'subject': '用户', 'predicate': '常用语言', 'object': 'Python 和 Go', 'negated': False,
                'time': None, 'source_ids': ['e3'], 'core': True}]},
]


def event(eid, memory):
    facts = [dict(f, source_ids=[eid]) for f in memory['facts']]
    return {'id': eid, 'kind': 'human_message',
            'peer_id': 'agent:private:qq10001:sender10001', 'sender_id': 'qq:10001',
            'visibility': memory['visibility'], 'content': '记录 ' + eid,
            'actions': [{'kind': 'add', 'id': memory['id'], 'weight': memory['weight'],
                         'body': '用户相关的一条事实记录。' * 20, 'facts': facts}]}


def main():
    cfg = read_private_config()
    section = cfg.get('embedding') or {}
    provider = section.get('provider') or 'siliconflow'
    model = section.get('model') or 'BAAI/bge-m3'
    dim = int(section.get('dim') or 1024)
    print('provider = %s  model = %s  dim = %d' % (provider, model, dim))

    embedding = build_embedding(provider=provider, model=model, dim=dim)
    reranker = SiliconFlowReranker(model=section.get('rerank_model') or 'BAAI/bge-reranker-v2-m3')

    tmp = tempfile.TemporaryDirectory()
    engine = MemoryEngine(Path(tmp.name) / 'memory.db', load_config())
    store = SqliteVectorStore(Path(tmp.name) / 'index.db')
    adapter = RAGAdapter(embedding, store, engine=engine, reranker=reranker,
                          min_relevance=MIN_RELEVANCE)

    failures = []

    def check(name, ok, detail=''):
        print('  [%s] %s %s' % ('PASS' if ok else 'FAIL', name, detail))
        if not ok:
            failures.append(name)

    try:
        print('=== 1. ingest + sync into the real index')
        for memory in MEMORIES:
            engine.process_event(event('e_' + memory['id'], memory), NOW)
        pending = len(engine.outbox())
        report = sync_once(engine, adapter)
        print('    ' + sync_report_line(report))
        check('outbox drained', pending == 3 and report['acked'] == 3,
              'pending=%d acked=%d' % (pending, report['acked']))
        check('index has chunks', store.total() >= 3, 'chunks=%d' % store.total())

        print('=== 2. paraphrase query hits the right memory')
        query = '那个人平时在哪个城市工作'
        hits = adapter.search(query, {'requester': 'qq:10001'}, limit=5)
        for hit in hits:
            print('    %.4f  score=%.4f  %s  %s' % (hit['relevance'], hit['score'],
                                                   hit['memory_id'], hit['text'][:40]))
        check('top hit is the Hangzhou memory', bool(hits) and hits[0]['memory_id'] == 'm_hangzhou',
              'top=%s' % (hits[0]['memory_id'] if hits else 'none'))
        check('unrelated high-weight memory is not injected',
              all(h['memory_id'] != 'm_milktea' for h in hits),
              'ids=%s' % [h['memory_id'] for h in hits])

        print('=== 3. the reranker really answers')
        ranked = adapter.rerank(query, hits)
        print('    status=%s backend=%s' % (ranked['status'], ranked.get('reranker_backend')))
        check('rerank status is ok', ranked['status'] == 'ok')

        print('=== 4. forced delete clears the index immediately')
        target = 'm_hangzhou'
        before = adapter.count(target)
        engine.db.execute("UPDATE memories SET profile='archived', revision=revision+1 WHERE id=?", (target,))
        engine.db.commit()
        engine._queue(target)
        engine.db.commit()
        delete_report = sync_once(engine, adapter)
        after = adapter.count(target)
        hits_after = adapter.search(query, {'requester': 'qq:10001'}, limit=5)
        check('count is zero after delete', after == 0, 'before=%d after=%d' % (before, after))
        check('the deleted memory is no longer returned',
              all(h['memory_id'] != target for h in hits_after),
              'ids=%s' % [h['memory_id'] for h in hits_after])
        check('delete event acknowledged', delete_report['acked'] == 1)

        print('=== 5. the source-side filter drops the old hit too')
        row = engine.row(target)
        check('authoritative row is archived', row['profile'] == 'archived')
    finally:
        store.close()
        engine.close()
        tmp.cleanup()

    print()
    if failures:
        print('DEMO FAILED: %s' % ', '.join(failures))
        return 1
    print('DEMO PASSED: the index really answers paraphrases, the reranker really answers,'
          ' and a forced delete really clears the vectors.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
