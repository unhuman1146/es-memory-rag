"""Live acceptance demo for the proactive injection path (GUIDE.md step S4).

This script is **not** part of CI -- it needs a provider key and network access.
It walks the S4 acceptance gates that unit tests cannot cover:

* a paraphrase chat turn produces the block, with **no retrieval tool-call**;
* the reranker really answers (``status`` is ``ok``, not ``degraded``);
* a high-weight but unrelated memory is not injected;
* a sensitive memory is gated, and released when the user brings it up;
* a private memory of somebody else never reaches the context;
* the block declares what it left out and never leaks an internal record id.

Run it from the package root::

    python scripts/demo_live_injection.py

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

from adapters.chat_path import ChatPath
from adapters.embedding import build_embedding, read_private_config
from adapters.rag_adapter import RAGAdapter
from adapters.rerank import SiliconFlowReranker
from adapters.sync import sync_once, sync_report_line
from adapters.vector_store import SqliteVectorStore

NOW = 1_700_000_000.0
# Calibrated for bge-m3 on this kind of short Chinese fact projection, on the
# query shape the chat path builds (topic + messages, no stable id).
# Different models need their own value -- do not copy it blindly.
MIN_RELEVANCE = 0.45

MEMORIES = [
    {'id': 'm_hangzhou', 'src': 'evt-1', 'weight': 60, 'visibility': 'shared',
     'facts': [{'subject': 'qq:10001', 'predicate': '常住', 'object': '杭州', 'negated': False,
                'time': None, 'source_ids': ['evt-1'], 'core': True},
               {'subject': 'qq:10001', 'predicate': '职业', 'object': '后端开发工程师',
                'negated': False, 'time': None, 'source_ids': ['evt-1'], 'core': True}]},
    # High weight, completely different topic: it must not be injected for a
    # question about where the user lives.
    {'id': 'm_milktea', 'src': 'evt-2', 'weight': 95, 'visibility': 'shared',
     'facts': [{'subject': 'qq:10001', 'predicate': '喝', 'object': '甜奶茶', 'negated': True,
                'time': None, 'source_ids': ['evt-2'], 'core': True}]},
    # Private to somebody else: it must never reach this user's context.
    {'id': 'm_private', 'src': 'evt-3', 'weight': 80, 'visibility': 'private',
     'sender': 'qq:20002',
     'facts': [{'subject': 'qq:20002', 'predicate': '欠款', 'object': '五千元', 'negated': False,
                'time': None, 'source_ids': ['evt-3'], 'core': True}]},
    # Sensitive: never injected proactively, released when the user asks.
    {'id': 'm_sensitive', 'src': 'evt-4', 'weight': 70, 'visibility': 'shared',
     'facts': [{'subject': 'qq:10001', 'predicate': '害怕', 'object': '公开谈论病情',
                'negated': False, 'time': None, 'source_ids': ['evt-4'], 'core': True}],
     'emotion': {'valence': -0.8, 'arousal': 0.6, 'confidence': 0.9, 'sensitive': True}},
]


def event(eid, memory):
    facts = [dict(f, source_ids=[eid]) for f in memory['facts']]
    action = {'kind': 'add', 'id': memory['id'], 'weight': memory['weight'],
              'body': '用户相关的一条事实记录。' * 20, 'facts': facts}
    if memory.get('emotion'):
        action['emotion'] = memory['emotion']
    sender = memory.get('sender', 'qq:10001')
    return {'id': eid, 'kind': 'human_message',
            'peer_id': 'agent:private:%s:sender' % sender.replace(':', ''),
            'sender_id': sender,
            'visibility': memory['visibility'], 'content': '记录 ' + eid,
            'actions': [action]}


def show(result):
    print('    --- what the main model would receive ---')
    for line in (result['block'] or '(empty)').splitlines():
        print('    ' + line)
    print('    --------------------------------------------')
    print('    tokens=%d  candidates=%d  injected=%d  gated=%d' % (
        result['tokens'], result['stats']['candidates'], result['stats']['injected'],
        result['stats']['gated']))


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
    chat = ChatPath(engine, adapter)

    failures = []

    def check(name, ok, detail=''):
        print('  [%s] %s %s' % ('PASS' if ok else 'FAIL', name, detail))
        if not ok:
            failures.append(name)

    try:
        print('=== 1. ingest + sync into the real index')
        for memory in MEMORIES:
            engine.process_event(event(memory['src'], memory), NOW)
        pending = len(engine.outbox())
        report = sync_once(engine, adapter)
        print('    ' + sync_report_line(report))
        check('outbox drained', pending == len(MEMORIES) and report['acked'] == len(MEMORIES),
              'pending=%d acked=%d' % (pending, report['acked']))
        check('index has chunks', store.total() >= len(MEMORIES), 'chunks=%d' % store.total())

        print('=== 2. a plain chat turn gets the block, with no retrieval tool-call')
        result = chat.inject([{'role': 'user', 'content': '我平时在哪个城市生活'}],
                             requester='qq:10001', speaker='qq:10001')
        show(result)
        check('the block is not empty', bool(result['block']))
        check('the right memory was injected', '杭州' in result['block'])
        check('the reranker really answered', result['rerank']['status'] == 'ok',
              'status=%s backend=%s' % (result['rerank']['status'], result['rerank']['backend']))

        print('=== 3. a high-weight but unrelated memory is not injected')
        check('the unrelated memory is absent', '甜奶茶' not in result['block'])
        check('it is not in the injected set',
              all(m != 'm_milktea' for m in [c['memory_id'] for c in result['injected']]),
              'ids=%s' % [c['memory_id'] for c in result['injected']])

        print('=== 4. a private memory of somebody else never arrives')
        check('the private memory is absent', '五千元' not in result['block'])

        print('=== 5. a sensitive memory is gated until the user raises it')
        gated = chat.inject([{'role': 'user', 'content': '我之前说的公开谈论病情那件事'}],
                            requester='qq:10001', speaker='qq:10001')
        show(gated)
        check('the sensitive memory was gated', gated['stats']['gated'] >= 1,
              'gated=%d' % gated['stats']['gated'])
        check('it is not in the block', '病情' not in gated['block'])
        released = chat.inject([{'role': 'user', 'content': '我之前说的公开谈论病情那件事'}],
                               requester='qq:10001', speaker='qq:10001',
                               user_initiated=True)
        check('the user may still reach it', '病情' in released['block'],
              'gated=%d injected=%d' % (released['stats']['gated'],
                                         released['stats']['injected']))

        print('=== 6. the block declares what it left out and hides internal ids')
        check('the block declares its completeness',
              '未加载' in result['missing_notice'] or '无遗漏' in result['missing_notice'],
              repr(result['missing_notice']))
        check('no internal record id in the block',
              all(mid not in result['block'] for mid in [m['id'] for m in MEMORIES]),
              'block=%r' % result['block'][:60])
        check('the source of a fact is shown', 'evt-1' in result['block'])

        print('=== 7. the report explains every omitted candidate')
        for entry in result['omitted']:
            print('    omitted %s  reason=%s' % (entry['memory_id'], entry['reason']))
        check('every omission has a reason',
              all(e.get('reason') for e in result['omitted']))

        print('=== 8. no credential leaks into the block or the report')
        key = os.environ.get('SILICONFLOW_API_KEY') or (cfg.get('siliconflow_api_key') or '')
        blob = json.dumps(result, ensure_ascii=False, default=str)
        check('the api key is not in the output', (not key) or (key not in blob))
    finally:
        store.close()
        engine.close()
        tmp.cleanup()

    print()
    if failures:
        print('DEMO FAILED: %s' % ', '.join(failures))
        return 1
    print('DEMO PASSED: a plain chat turn carries the projection, the reranker really '
          'answered, and the unrelated / private / sensitive memories stayed out.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
