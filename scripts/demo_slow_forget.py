"""Real-machine acceptance for the "forget less" presets.

Needs a key (SiliconFlow) or a local Ollama, and network. **Not part of CI.**

What this script actually measures, and why it has to be measured:

* The presets change *what the projection looks like*: the default config
  compresses a memory down to its fact atoms, the evergreen preset keeps the
  original text forever. The indexed text is the projection, so the text that
  gets embedded is not the same text under every preset.
* ``min_relevance`` is a threshold on a score produced by embedding that text.
  Change the text, change the score distribution, and a threshold calibrated
  on one projection form is no longer calibrated on the other.
* So this script does not assume ``0.45``. It measures the separation between
  "this memory answers the query" and "this memory is unrelated" under every
  preset, and reports the threshold that actually separates them.

It also checks the two claims the feature makes about the rest of the system:
slow forgetting must not unblock sensitive memories, and it must not push
unrelated memories into the context.

    python scripts/demo_slow_forget.py
    python scripts/demo_slow_forget.py --json report.json
"""
import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'scripts'))

from memory_engine import MemoryEngine, load_config
from adapters.chat_path import ChatPath
from adapters.embedding import EmbeddingError, build_embedding, read_private_config
from adapters.rag_adapter import RAGAdapter
from adapters.rerank import SiliconFlowReranker
from adapters.sync import sync_once
from adapters.vector_store import SqliteVectorStore

NOW = 1_700_000_000.0
DAY = 86400.0
PRESETS = ('default', 'slow-forget', 'evergreen')
DEFAULT_MIN_RELEVANCE = 0.45

# Ten memories on distinct topics. The bodies are long on purpose: a body that
# fits in one chunk would hide the chunking difference between the projection
# forms, and chunking is exactly what makes the full-text form behave
# differently under embedding.
MEMORIES = [
    ('m_city', 80, '我常住杭州，西湖边租房，通勤方便。',
     '二〇二三年底搬进的房子，在文三路和古翠路之间，阳台朝南能看到一点远山。'
     '楼下有一家面馆和一个总在排队的水果摊，房租四千二，押一付三，房东人很好，'
     '说如果要养猫得提前说一声。',
     [('qq:10001', '常住', '杭州'), ('qq:10001', '租房', '西湖边')]),
    ('m_milk', 80, '我爱喝甜奶茶，芋泥波波，每天一杯已经成习惯。',
     '公司楼下那家三点下班就开始排队，我一般排在第一个，能选到芋泥波波加芋圆，'
     '少冰半糖。换成别家就总差一口气，后来试过自己做，用料理机打不开芋头，'
     '最后还是老实去排队。',
     [('qq:10001', '喝', '甜奶茶'), ('qq:10001', '频率', '每天一杯')]),
    ('m_lang', 80, '我写代码主要用 Python 和 Go，偶尔写点 TypeScript。',
     'Python 用来做数据处理和自动化脚本，写得最多的是那些定时抓取和清洗的活。'
     'Go 是因为并发写起来省心，服务端的小工具都拿它写。TypeScript 只在不得不'
     '碰前端的时候写，写完就忘，两个月后自己都看不懂。',
     [('qq:10001', '常用语言', 'Python 和 Go'), ('qq:10001', '偶尔写', 'TypeScript')]),
    ('m_salary', 80, '我月薪两万，年终看项目情况浮动。',
     '工资是二〇二四年谈的，当时手上有一个还不错的 offer，HR 说不给包机票，'
     '但可以加到两万。年终一般是一到两个月，浮动不大，主要看部门当年的预算，'
     '前年拿过两个月，去年只有一个半。',
     [('qq:10001', '薪资', '两万'), ('qq:10001', '构成', '月薪加年终')]),
    ('m_dog', 80, '我养了一只柴犬，叫豆豆，每天遛两次。',
     '豆豆是二〇二四年五月接回来的，当时才两个月大，窝在纸箱里睡觉。'
     '欢追小区里那只灰色的大狗，欢被自行车吓到，见人就摇尾巴。'
     '早上六点半遛一次，晚上吃完饭再遛一次，下雨天改成室内陪它玩球。',
     [('qq:10001', '养', '柴犬'), ('qq:10001', '宠物名字', '豆豆')]),
    ('m_project', 80, '我在做一个面向 Agent 的向量记忆引擎项目，叫 ES-Memory。',
     '起因是我自己用了几个月的记忆方案都不太行，要么把上下文塞满，要么记了就忘。'
     '这个项目想做的是给 Agent 一套衰减曲线加主动注入，'
     '凌晨两点的仓管负责压缩，白天由主模型按相关性取用，两边各管各的。',
     [('qq:10001', '在做', 'Agent 向量记忆引擎'), ('qq:10001', '项目名', 'ES-Memory')]),
    ('m_birthday', 80, '我的生日是 3 月 14 号，白羊座。',
     '每年这个时候同事会订个蛋糕，公司福利里有一张生日券可以用。'
     '小时候家里不过生日，长大后才开始认真过，去年朋友们在一家日料店定的位，'
     '个人都来了，还有人专门从深圳飞过来。',
     [('qq:10001', '生日', '3 月 14 号'), ('qq:10001', '星座', '白羊座')]),
    ('m_allergy', 80, '我对花生过敏，吃含花生的东西会起疹子。',
     '小时候吃过一次花生酱饼干，脖子上起了一片红，吓坏了。'
     '后来测过过敏原，花生属于四级，别的都还行。'
     '出去吃饭要特意问一句，菜里偶尔会放花生油做香，酱料也算。',
     [('qq:10001', '对', '花生过敏'), ('qq:10001', '过敏表现', '起疹子')]),
    ('m_commute', 80, '我坐地铁一号线通勤，单程四十分钟。',
     '早上七点二十分那班车人少，能有座，站到底再换乘一次。'
     '晚上六点以后基本没位子，只能站着刷手机。'
     '下雨天公司会提前发通知让晚点到，那种时候地铁站空得有点吓人。',
     [('qq:10001', '通勤方式', '地铁一号线'), ('qq:10001', '单程耗时', '四十分钟')]),
    ('m_book', 80, '我最近在重读《三体》，觉得黑暗森林那一段最好。',
     '第一次读是很多年前，只记得震撼。这次重读才看懂面壁计划和猜疑链的部分，'
     '晚上躺床上常常读着读着就忘时间。'
     '打算读完再找叶文洁那条线重看一遍，之前一直没想明白她最后到底做了什么。',
     [('qq:10001', '在读', '三体'), ('qq:10001', '最喜欢', '黑暗森林')]),
]


def build_body(description, detail, pairs):
    """A realistic long memory body: the topic, the context, and the facts
    repeated the way a person would actually write them down.

    Long enough to be split into several chunks by the indexer -- that is the
    whole point: the full-text projection is chunked, the fact-only
    projection is not, and chunking changes what a query matches.
    """
    parts = [description + '。', detail]
    for subject, predicate, obj in pairs:
        parts.append('%s这条是%s%s：%s。' % (subject, predicate, obj, obj))
        parts.append('当时为什么会记下%s，我记得原因是%s跟%s有关。' % (predicate, obj, subject))
        parts.append('这条%s的信息在后来的对话里又提过一次，内容还是%s%s。'
                     % (predicate, subject, obj))
    return ''.join(parts)

# One query per memory, phrased differently from the stored fact, plus three
# queries that have nothing to do with the corpus.
QUERIES = [
    ('m_city', '我现在住在哪个城市呀'),
    ('m_milk', '我平时爱喝点什么'),
    ('m_lang', '写代码我主要用哪些语言'),
    ('m_salary', '我的工资收入大概是什么水平'),
    ('m_dog', '我家里养的小动物叫什么名字'),
    ('m_project', '我最近在折腾什么项目'),
    ('m_birthday', '我的生日是哪天'),
    ('m_allergy', '我有什么东西是不能吃的'),
    ('m_commute', '我每天怎么去公司'),
    ('m_book', '我在看哪本书'),
    (None, '今天晚饭吃点什么好呢'),
    (None, '明天出门要不要带伞'),
    (None, '帮我推荐一个适合周末去的地方'),
]

def build_corpus():
    events = []
    for memory_id, weight, description, detail, fact_pairs in MEMORIES:
        eid = 'evt-' + memory_id
        facts = []
        for subject, predicate, obj in fact_pairs:
            facts.append({'subject': subject, 'predicate': predicate, 'object': obj,
                          'negated': False, 'time': None, 'source_ids': [eid],
                          'core': True})
        events.append({'id': eid, 'kind': 'human_message',
                       'peer_id': 'agent:private:qq10001:sender', 'sender_id': 'qq:10001',
                       'visibility': 'shared', 'content': '记录 ' + eid,
                       'actions': [{'kind': 'add', 'id': memory_id, 'weight': weight,
                                    'body': build_body(description, detail, fact_pairs),
                                    'facts': facts}]})
    return events


def run_preset(tmp, preset, horizon_days, embedding, reranker, use_rerank):
    """One preset, one age. Returns the measured facts about that world."""
    cfg = load_config() if preset == 'default' else load_config(preset=preset)
    engine = MemoryEngine(Path(tmp.name) / (preset + '.db'), cfg)
    store = SqliteVectorStore(Path(tmp.name) / (preset + '.index.db'))
    adapter = RAGAdapter(embedding, store, engine=engine,
                          reranker=reranker if use_rerank else None)

    for event in build_corpus():
        engine.process_event(event, NOW)

    horizon = NOW + horizon_days * DAY
    engine.apply_plan(engine.plan(horizon))
    sync_once(engine, adapter)  # index the *settled* projection

    rows = [engine.row(m[0]) for m in MEMORIES]
    alive = [r for r in rows if r and r['deleted_at'] is None and not r['purged']]
    tiers = {}
    for row in alive:
        tiers[row['tier']] = tiers.get(row['tier'], 0) + 1
    lengths = [len(r['text']) for r in alive]
    chunks = {r['id']: store.count(r['id']) for r in alive}
    detail_kept = sum(1 for r in alive if 'detail' in json.loads(r['text'] or '{}'))

    # ---- relevance: raw scores, no threshold in the way ----
    positives, negatives = [], []
    per_query = []
    for target, query in QUERIES:
        hits = adapter.search(query, {'requester': 'qq:10001'}, limit=100)
        best = {}
        for hit in hits:
            mid = hit['memory_id']
            best[mid] = max(best.get(mid, 0.0), float(hit['relevance']))
        for mid, score in best.items():
            (positives if mid == target else negatives).append(score)
        if target:
            per_query.append((target, query, best.get(target, 0.0)))

    def threshold_report(pos, neg):
        """The smallest threshold that keeps every hit and admits no miss."""
        if not pos:
            return None
        best = None
        for step in range(5, 96):
            t = step / 100.0
            kept = sum(1 for p in pos if p >= t)
            admitted = sum(1 for n in neg if n >= t)
            if kept == len(pos) and admitted == 0:
                best = t
                break
        return best

    recommended = threshold_report(positives, negatives)
    margin = None
    if positives and negatives:
        margin = round(min(positives) - max(negatives), 4)
    at_default = {
        'hits_kept': sum(1 for p in positives if p >= DEFAULT_MIN_RELEVANCE),
        'hits_total': len(positives),
        'misses_admitted': sum(1 for n in negatives if n >= DEFAULT_MIN_RELEVANCE),
        'misses_total': len(negatives),
    }

    # ---- the injection gate must not open up ----
    injection = {'injected': [], 'omitted_reasons': {}}
    try:
        chat = ChatPath(engine, adapter, min_relevance=DEFAULT_MIN_RELEVANCE)
        report = chat.inject([{'role': 'user', 'content': '我现在住在哪个城市呀'}],
                             requester='qq:10001')
        injection['injected'] = [c.get('memory_id') for c in report.get('injected', [])]
        for item in report.get('omitted', []):
            reason = item.get('reason', 'unknown')
            injection['omitted_reasons'][reason] = injection['omitted_reasons'].get(reason, 0) + 1
    except Exception as exc:  # noqa: BLE001 - a demo must report, not crash
        injection['error'] = str(exc)[:200]

    result = {
        'preset': preset,
        'horizon_days': horizon_days,
        'alive': len(alive),
        'deleted': len(rows) - len(alive),
        'tiers': tiers,
        'detail_kept': detail_kept,
        'mean_projection_chars': round(sum(lengths) / len(lengths), 1) if lengths else 0,
        'total_chunks': sum(chunks.values()),
        'positives': {'count': len(positives),
                      'min': round(min(positives), 4) if positives else None,
                      'mean': round(sum(positives) / len(positives), 4) if positives else None},
        'negatives': {'count': len(negatives),
                      'max': round(max(negatives), 4) if negatives else None,
                      'mean': round(sum(negatives) / len(negatives), 4) if negatives else None},
        'recommended_min_relevance': recommended,
        'separation_margin': margin,
        'at_0_45': at_default,
        'injection': injection,
        'per_query': [{'target': t, 'query': q, 'relevance': round(s, 4)}
                      for t, q, s in per_query],
    }
    store.close()
    engine.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--horizons', default='30,365',
                        help='comma separated ages in days (default: 30,365)')
    parser.add_argument('--json', help='write the full report to this path')
    parser.add_argument('--no-rerank', action='store_true',
                        help='skip the reranker (faster, but not the real pipeline)')
    args = parser.parse_args()
    horizons = [int(h) for h in args.horizons.split(',') if h.strip()]

    private = read_private_config()
    section = private.get('embedding') or {}
    try:
        embedding = build_embedding(provider=section.get('provider') or 'siliconflow',
                                    model=section.get('model') or 'BAAI/bge-m3',
                                    dim=int(section.get('dim') or 1024))
    except EmbeddingError as exc:
        print('embedding unavailable: %s' % exc)
        return 2
    reranker = None
    if not args.no_rerank:
        try:
            reranker = SiliconFlowReranker(model=section.get('rerank_model')
                                           or 'BAAI/bge-reranker-v2-m3')
        except Exception as exc:  # noqa: BLE001
            print('reranker unavailable (%s); continuing vector-only' % str(exc)[:120])

    print('embedding backend: %s' % getattr(embedding, 'backend', '?'))
    results = []
    tmp = tempfile.TemporaryDirectory()
    try:
        for horizon in horizons:
            for preset in PRESETS:
                started = time.time()
                result = run_preset(tmp, preset, horizon, embedding, reranker,
                                    not args.no_rerank)
                result['seconds'] = round(time.time() - started, 1)
                results.append(result)
                pos = result['positives']
                neg = result['negatives']
                print('\n=== %-11s | %d days | %.1fs ===' % (preset, horizon, result['seconds']))
                print('  alive %d/%d  deleted %d  tiers %s  detail kept %d'
                      % (result['alive'], len(MEMORIES), result['deleted'],
                         result['tiers'], result['detail_kept']))
                print('  projection %d chars avg, %d chunks indexed'
                      % (result['mean_projection_chars'], result['total_chunks']))
                print('  hits   n=%s min=%s mean=%s' % (pos['count'], pos['min'], pos['mean']))
                print('  misses n=%s max=%s mean=%s' % (neg['count'], neg['max'], neg['mean']))
                print('  recommended min_relevance: %s (margin %s)  | at 0.45: %s'
                      % (result['recommended_min_relevance'], result['separation_margin'],
                         result['at_0_45']))
                print('  injection: %s' % json.dumps(result['injection'], ensure_ascii=False))
    finally:
        tmp.cleanup()

    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2),
                                   encoding='utf-8')
        print('\nwrote %s' % args.json)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
