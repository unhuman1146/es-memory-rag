"""1.4.0a2 offline demo: does the memory block read like memory?

Four scenarios, no API key, no network:

1. the same facts rendered in both styles (``archive`` vs ``recall``);
2. a one-hop neighbour that works  -- 猫粮 -> 养了只猫;
3. a one-hop neighbour that fails  -- 蛋糕 -/-> 生日;
4. a reasoning-triggered second injection for the case 3 missed.

Scenario 3 is the interesting one: it is here to show the ceiling of
structural edges, not to sell the feature. A birthday and a cake have no edge
in the store; the link lives in the model's world knowledge.

    python scripts/demo_140_alpha.py
"""
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

from memory_engine import MemoryEngine, load_config

from adapters.assoc import neighbors
from adapters.chat_path import ChatPath
from adapters.embedding import HashingEmbedding
from adapters.rag_adapter import RAGAdapter
from adapters.vector_store import SqliteVectorStore

TZ = timezone(timedelta(hours=8))
VIEWER = 'qq:10001'
NAMES = {'qq:20002': '张三'}


def at(y, m, d, hour=20):
    return datetime(y, m, d, hour, tzinfo=TZ).timestamp()


NOW = at(2026, 10, 27, 21)

# (event id, peer, when, content, memory id, weight, fact)
WORLD = [
    ('evt-birthday', 'qq:10001:main:qq10001', at(2026, 8, 27), '我下个月生日',
     'mem_birthday', 70, ('生日', '10-15')),
    ('evt-sleep', 'qq:10001:main:qq10001', at(2026, 10, 20), '最近总睡不着',
     'mem_sleep', 60, ('最近', '总睡不着')),
    ('evt-cake', 'qq:10001:main:qq10001', at(2026, 10, 27), '我想吃蛋糕',
     'mem_cake', 60, ('想吃', '蛋糕')),
    ('evt-cat', 'qq:10001:life:qq10001', at(2026, 7, 1), '我养了只猫',
     'mem_cat', 70, ('养了', '猫')),
    ('evt-catfood', 'qq:10001:life:qq10001', at(2026, 10, 18), '猫粮买哪个牌子好',
     'mem_catfood', 65, ('在挑', '猫粮')),
    ('evt-job', 'qq:10001:work:qq10001', at(2026, 10, 5), '我换了工作，现在做后端',
     'mem_job', 65, ('现在做', '后端开发')),
]


class KeywordReranker:
    """Offline stand-in. A real deployment wants a real reranker; this one at
    least reports ``ok`` honestly instead of pretending to be semantic."""

    backend = 'demo-keyword'

    def rerank(self, query, documents):
        terms = [ch for ch in query if not ch.isspace()]
        scored = []
        for index, doc in enumerate(documents):
            hits = sum(1 for ch in terms if ch in doc)
            scored.append({'index': index, 'score': hits / max(1, len(terms))})
        scored.sort(key=lambda item: (-item['score'], item['index']))
        return scored


def build():
    """A throwaway database. Nothing is written inside the package: an
    installed skill directory may well be read-only."""
    tmp = Path(tempfile.mkdtemp(prefix='esm-140-alpha-'))
    engine = MemoryEngine(tmp / 'memory.db', load_config())
    store = SqliteVectorStore(tmp / 'index.db')
    adapter = RAGAdapter(HashingEmbedding(dim=256), store, engine=engine,
                         reranker=KeywordReranker(), min_relevance=0.30)
    for eid, peer, when, content, mid, weight, (predicate, obj) in WORLD:
        engine.process_event({
            'id': eid, 'kind': 'human_message', 'peer_id': peer, 'sender_id': VIEWER,
            'visibility': 'shared', 'content': content,
            'actions': [{'kind': 'add', 'id': mid, 'weight': weight, 'body': content * 20,
                         'facts': [{'subject': VIEWER, 'predicate': predicate, 'object': obj,
                                    'time': None, 'negated': False,
                                    'source_ids': [eid], 'core': True}]}]}, when)
    from adapters.sync import sync_once
    sync_once(engine, adapter)
    return engine, store, adapter, tmp


def rule(title):
    print('\n' + '=' * 72)
    print(title)
    print('=' * 72)


def show(report, label='块'):
    print('  %s:' % label)
    for line in (report['block'] or '(空)').splitlines():
        print('    ' + line)
    stats = report.get('stats') or {}
    print('    [tokens=%s injected=%s assoc=%s/%s gated=%s]' % (
        report.get('tokens'), stats.get('injected'), stats.get('assoc_injected'),
        stats.get('assoc_candidates'), stats.get('gated')))


def main():
    engine, store, adapter, tmp = build()
    try:
        # ---------------------------------------------------------------- 1
        rule('1. 同一条记忆，两种渲染（不经检索，直接渲染真实投影）')
        rows = ['mem_cake', 'mem_cat', 'mem_birthday']
        cands = [{'memory_id': mid, 'text': engine.row(mid)['text']} for mid in rows]
        archive = ChatPath(engine, adapter, style='archive', viewer=VIEWER,
                           person_names=NAMES, require_rerank=False)
        recall = ChatPath(engine, adapter, style='recall', viewer=VIEWER,
                          person_names=NAMES, require_rerank=False)
        fake = {'missing_notice': archive._missing(2), 'rerank': {'status': 'ok'}}
        print('  archive（默认，可审计，像记录）:')
        for line in archive.render(cands, fake).splitlines():
            print('    ' + line)
        fake_recall = {'missing_notice': recall._missing(2), 'rerank': {'status': 'ok'}}
        print('  recall（1.4.0a2，像记忆）:')
        for line in recall.render(cands, fake_recall).splitlines():
            print('    ' + line)
        print('    （括注表示事实发生时间；archive 风格中的记录日期是系统收录时间）')

        # ---------------------------------------------------------------- 2
        rule('2. 弱相关预注入 · 成功例：猫粮 -> 养了只猫（跨对话、间隔 109 天，只能靠共享词元）')
        path = ChatPath(engine, adapter, style='recall', viewer=VIEWER,
                        person_names=NAMES, require_rerank=False, assoc=True, top_k=3)
        report = path.inject([{'role': 'user', 'content': '猫粮哪个牌子好'}],
                             requester=VIEWER, now=NOW)
        show(report)
        print('    邻居依据:')
        for item in neighbors(engine, ['mem_catfood'], requester=VIEWER, limit=3):
            print('      %-14s via=%-12s strength=%.3f  %s' % (
                item['memory_id'], item['via'], item['strength'], item['evidence']))

        # ---------------------------------------------------------------- 3
        rule('3. 弱相关预注入 · 失败例：蛋糕 -/-> 生日')
        path = ChatPath(engine, adapter, style='recall', viewer=VIEWER,
                        person_names=NAMES, require_rerank=False, assoc=True, top_k=3)
        report = path.inject([{'role': 'user', 'content': '我想吃蛋糕'}],
                             requester=VIEWER, now=NOW)
        show(report, 'assoc=True')
        print('    全部邻居候选:')
        for item in neighbors(engine, ['mem_cake'], requester=VIEWER, limit=5):
            print('      %-14s via=%-12s strength=%.3f  %s' % (
                item['memory_id'], item['via'], item['strength'], item['evidence']))
        gap = (datetime(2026, 10, 27, tzinfo=TZ) - datetime(2026, 8, 27, tzinfo=TZ)).days
        print('    -> 生日那条与蛋糕相隔 %d 天，超出 45 天窗口，且没有共享词元：' % gap)
        print('       结构边抓不到它。抓到的 %s 也只是「同一段对话」，属于噪声。' %
              ', '.join(c['memory_id'] for c in report['injected']
                        if c.get('assoc')) or '（无）')

        # ---------------------------------------------------------------- 4
        rule('4. 结构化补充检索：宿主收到「生日」话题请求后调用 recall()')
        turn_budget = path.new_turn_budget()
        base = path.inject([{'role': 'user', 'content': '我想吃蛋糕'}],
                           requester=VIEWER, now=NOW, turn_budget=turn_budget)
        already = [c['memory_id'] for c in base['injected']]
        print('  首轮已注入: %s' % (already or '(无)'))
        print('  宿主收到的结构化补充请求: {"need_more": true, "terms": ["生日"]}')
        supplement = path.recall('生日', why='生日场景下蛋糕有意义',
                                 requester=VIEWER, now=NOW, exclude_ids=already, turn_budget=turn_budget)
        for line in (supplement['block'] or '(空)').splitlines():
            print('    ' + line)
        print('    [tokens=%s injected=%s]' % (supplement['tokens'],
                                               supplement['stats']['injected']))

        # ---------------------------------------------------------------- 5
        rule('5. 这份 demo 能证明什么、不能证明什么')
        print('  能证明:')
        print('    · 同一批事实可以渲染成两种语气，archive 仍是默认渲染；本版另有正确性修复')
        print('    · 结构边能从库里派生出一跳邻居，且邻居有独立预算与「顺带想到」标记')
        print('    · 二次注入能带来源标记，不会被当成当前问题的直接命中')
        print('  不能证明（需要真机与标注样本）:')
        print('    · 「像记忆」是否真的降低了出戏感 —— 这要人来评，不能自证')
        print('    · 预注入的噪声率 —— 本 demo 里 1 条有用 1 条噪声，样本量 1，不作结论')
        print('    · 语义跳跃的联想 —— 蛋糕→生日这条永远抓不到，由宿主校验后通过结构化补充请求触发')
    finally:
        store.close()
        engine.close()
        shutil.rmtree(str(tmp), ignore_errors=True)


if __name__ == '__main__':
    main()
