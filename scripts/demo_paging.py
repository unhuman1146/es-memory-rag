"""Offline disposable-frame demo backed by a real temporary SQLite archive."""
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from adapters.context_pager import ContextPager, HistoryTurn, ToolPage


def main():
    with tempfile.TemporaryDirectory() as directory:
        with closing(sqlite3.connect(str(Path(directory) / 'archive.db'))) as archive:
            archive.execute('CREATE TABLE raw(id INTEGER PRIMARY KEY, body TEXT)')
            archive.executemany('INSERT INTO raw VALUES (?,?)',
                [(i, f'群友 {i % 3} 的原始发言 {i}：讨论不同的话题') for i in range(600)])
            archive.commit()
            def acl(row, viewer):
                return viewer == 'demo' and archive.execute('SELECT 1 FROM raw WHERE id=?',
                                                           (row.message_id,)).fetchone() is not None
            rows = [HistoryTurn(str(i), 'user', body, f'qq:{i % 3}', 'group:demo', True)
                    for i, body in archive.execute('SELECT id,body FROM raw ORDER BY id')]
            pager = ContextPager(acl)
            args = dict(system='固定系统提示与工具定义', ledger='保留来源明确的关系和未完成事项',
                        current='当前提问', requester='demo', scope='group:demo', history=rows,
                        tools=[ToolPage('old-result', '已经读完的旧工具输出' * 1000, False, True)])
            for label, kwargs in [('normal', {}), ('large', {'large_context': True,
                    'large_reason': '用户明确要求核对历史原文'}), ('normal-again', {})]:
                frame = pager.prepare(**args, **kwargs)
                print(json.dumps(dict(step=label, pool=frame.pool_size,
                    resident=len(frame.history_ids), input_estimate=frame.estimated_input_tokens,
                    output_reserve=frame.reserved_output_tokens,
                    unloaded_tools=frame.unloaded_tool_ids), ensure_ascii=False))
            assert archive.execute('SELECT count(*) FROM raw').fetchone()[0] == 600
            print('archive intact; no API calls; no memory deletion')


if __name__ == '__main__':
    main()
