"""Context budget assembler: HARD ceiling stops overflow, SOFT line stops rot.

Pure stdlib. Token counts are estimates (CJK char ~= 1 token, ASCII ~= 4 chars
per token); calibrate against the provider tokenizer before trusting the numbers.
"""
import math
import re
import sys

# --- UTF-8 stdout/stderr (Windows) ---
# On Windows the default stdout encoding is the locale code page (cp936/GBK),
# so any consumer decoding this CLI's output as UTF-8 raises UnicodeDecodeError
# and silently loses the tail of the output. Force UTF-8 on every platform.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        try:
            _stream.reconfigure(encoding='utf-8')
        except (ValueError, OSError):
            pass

CJK = re.compile(r'[^\x00-\x7f]')

DEFAULT_ALLOC = {
    'system_tools': 20_000,
    'ledger': 2_000,
    'summary': 2_000,
    'history': 60_000,
    'memory': 8_000,
    'media': 16_000,
    'reserve': 16_000,
}

# Peak ceilings; the steady-state total sits under the soft line on purpose.
PEAK_CAPS = {'history': 120_000, 'memory': 24_000, 'media': 64_000}


def estimate_tokens(text):
    if not text:
        return 0
    wide = len(CJK.findall(text))
    return wide + math.ceil((len(text) - wide) / 4)


class BudgetError(Exception):
    def __init__(self, committed, hard, over_by, suggestion):
        self.committed, self.hard, self.over_by = committed, hard, over_by
        self.suggestion = suggestion
        super().__init__(f'context over hard cap by {over_by} tokens '
                         f'({committed}/{hard}); shrink {suggestion}')


class Assembly:
    def __init__(self, segments, committed, hard, soft, stats, cache_prefix_tokens,
                 cache_reset=False, history_evicted=0):
        self.segments = segments                  # [(name, text, tokens)] in send order
        self.committed = committed
        self.hard, self.soft = hard, soft
        self.stats = stats
        self.cache_prefix_tokens = cache_prefix_tokens
        self.cache_reset = cache_reset
        # Two separate signals, because per-block caps mean the total seldom grows
        # into the soft line on its own:
        #   should_reset  = we hit the soft line (256K) -> stop now and hand over
        #   needs_handoff = raw history has started being evicted -> schedule it
        self.should_reset = committed > soft
        self.needs_handoff = history_evicted > 0

    @property
    def total(self):
        return sum(tokens for _, _, tokens in self.segments)

    def render(self, separator='\n\n'):
        return separator.join(text for _, text, _ in self.segments)

    def __repr__(self):
        return (f'<Assembly total={self.total} committed={self.committed} '
                f'should_reset={self.should_reset} needs_handoff={self.needs_handoff} '
                f'blocks={list(self.stats)}>')


class ContextBudget:
    """Hard ceiling = overflow guard; soft line = quality guard (reset trigger)."""

    def __init__(self, hard=512_000, soft=256_000, alloc=None, evict_fraction=0.25):
        self.hard, self.soft = hard, soft
        self.alloc = dict(DEFAULT_ALLOC)
        self.alloc.update(alloc or {})
        if not 0 < evict_fraction <= 1:
            raise ValueError('evict_fraction must be in (0, 1]')
        self.evict_fraction = evict_fraction

    def fit_history(self, turns, budget=None):
        """LIFO fill from the newest turn. When it overflows, drop the oldest
        slice in ONE batch so the cache prefix is rebuilt once, not every turn."""
        budget = self.alloc['history'] if budget is None else budget
        kept, used = [], 0
        for turn in reversed(turns or []):
            cost = estimate_tokens(turn.get('content', '') if isinstance(turn, dict) else str(turn))
            if used + cost > budget:
                break
            kept.append(turn)
            used += cost
        kept.reverse()
        evicted = len(turns or []) - len(kept)
        if evicted > 0 and len(kept) > 1:
            # Leave headroom so the next few appends do not re-trigger eviction.
            trim = max(1, int(len(kept) * self.evict_fraction))
            if len(kept) - trim >= 1:
                kept = kept[trim:]
                evicted += trim
                used = sum(estimate_tokens(t.get('content', '') if isinstance(t, dict) else str(t))
                           for t in kept)
        return kept, used, evicted

    @staticmethod
    def _fit_items(items, budget, cap=None):
        kept, used = [], 0
        for item in items or []:
            cost = item.get('tokens')
            if cost is None:
                cost = estimate_tokens(item.get('text', ''))
            if used + cost > budget:
                continue          # skip this one, a smaller item may still fit
            if cap is not None and len(kept) >= cap:
                break
            kept.append(dict(item, tokens=cost))
            used += cost
        return kept, used, len(items or []) - len(kept)

    def fit_memory(self, hits, budget=None):
        return self._fit_items(hits, self.alloc['memory'] if budget is None else budget)

    def fit_media(self, items, budget=None, max_items=4):
        return self._fit_items(items, self.alloc['media'] if budget is None else budget, cap=max_items)

    def assemble(self, system='', tools='', ledger='', summary='', history=None,
                 memory=None, media=None, reserve=None):
        reserve = self.alloc['reserve'] if reserve is None else reserve
        kept_history, history_tokens, history_evicted = self.fit_history(history)
        kept_memory, memory_tokens, memory_dropped = self.fit_memory(memory)
        kept_media, media_tokens, media_dropped = self.fit_media(media)

        def block(name, text, cap):
            tokens = estimate_tokens(text)
            if tokens > cap:
                raise BudgetError(tokens, cap, tokens - cap, name)
            return (name, text, tokens) if text else None

        history_text = '\n'.join(t.get('content', '') if isinstance(t, dict) else str(t)
                                 for t in kept_history)
        media_text = '\n'.join(f"[{m.get('kind', 'media')}] {m.get('ref', '')} "
                               f"({m['tokens']} tokens)" for m in kept_media)
        memory_text = '\n'.join(f"[{m.get('at', '')}] {m.get('text', '')}" for m in kept_memory)

        planned = [block('system_tools', (system + '\n' + tools).strip(), self.alloc['system_tools']),
                   block('ledger', ledger, self.alloc['ledger']),
                   block('summary', summary, self.alloc['summary']),
                   ('history', history_text, history_tokens) if history_text else None,
                   ('memory', memory_text, memory_tokens) if memory_text else None,
                   ('media', media_text, media_tokens) if media_text else None]
        segments = [seg for seg in planned if seg]

        total = sum(tokens for _, _, tokens in segments)
        committed = total + reserve
        stats = {name: tokens for name, _, tokens in segments}
        stats.update(reserve=reserve, history_turns=len(kept_history),
                     history_evicted=history_evicted, memory_dropped=memory_dropped,
                     media_dropped=media_dropped)
        cache_prefix = sum(tokens for name, _, tokens in segments
                           if name in ('system_tools', 'ledger', 'summary', 'history'))
        if committed > self.hard:
            shaveable = max(('history', 'media', 'memory', 'summary'),
                            key=lambda k: stats.get(k, 0))
            raise BudgetError(committed, self.hard, committed - self.hard, shaveable)
        return Assembly(segments, committed, self.hard, self.soft, stats, cache_prefix,
                        cache_reset=history_evicted > 0, history_evicted=history_evicted)
