"""Executable SQLite reference for ES-Memory policy. Python 3.11+, stdlib only.

Manages canonical facts, weights, projections, lottery, receipts and RAG outbox.
Embedding/reranking and delivery are adapter contracts, not simulated successes.
"""
import argparse
import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:  # the package is used both as scripts/ on sys.path and as an import
    from presets import apply_preset, list_presets, resolve_preset, title
except ImportError:  # pragma: no cover
    from scripts.presets import apply_preset, list_presets, resolve_preset, title

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

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / 'assets' / 'config.example.json'

# Emotion defaults are engineering starting points, not calibrated constants.
EMOTION_DEFAULTS = {
    'enabled': True,
    'encoding_boost': 0.8,
    'rate_gain': 1.5,
    'charge_half_life_days': 60.0,
    'arousal_half_life_gain': 2.0,
    'negative_valence_half_life_factor': 0.6,
    'remind_recharge': 0.7,
    'reappraisal_charge_relief': 0.25,
    'auto_sensitive_charge': 0.6,
    'mood_weight': 0.1,
}


def load_config(path=None, preset=None):
    """Load the config, optionally with a named preset overlay applied.

    The overlay is merged *before* validation, so every threshold rule below
    is checked against the config that will actually be used.
    """
    cfg = json.loads(Path(path or DEFAULT_CONFIG).read_text(encoding='utf-8'))
    if preset:
        cfg = apply_preset(cfg, resolve_preset(preset))
    emotion = dict(EMOTION_DEFAULTS)
    emotion.update(cfg.get('emotion') or {})
    cfg['emotion'] = emotion
    for key in ('encoding_boost', 'rate_gain', 'arousal_half_life_gain'):
        value = emotion[key]
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError('invalid emotion policy: ' + key)
    for key in ('remind_recharge', 'reappraisal_charge_relief', 'auto_sensitive_charge'):
        value = emotion[key]
        if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError('invalid emotion policy: ' + key)
    if not 0 <= emotion['mood_weight'] < 1:
        raise ValueError('invalid emotion mood_weight')
    if not emotion['charge_half_life_days'] > 0 or not emotion['negative_valence_half_life_factor'] > 0:
        raise ValueError('emotion half-life must be positive')
    cfg.setdefault('outbox_max_attempts', 8)
    cfg.setdefault('outbox_acked_retention_days', 7)
    cfg.setdefault('dedup_normalize', 'whitespace')
    if not isinstance(cfg['outbox_max_attempts'], int) or cfg['outbox_max_attempts'] < 1:
        raise ValueError('invalid outbox_max_attempts')
    if not isinstance(cfg['outbox_acked_retention_days'], (int, float)) or cfg['outbox_acked_retention_days'] < 0:
        raise ValueError('invalid outbox_acked_retention_days')
    if cfg['dedup_normalize'] not in ('whitespace', 'nfkc_casefold'):
        raise ValueError('invalid dedup_normalize')
    for key in ('delete_probability_per_day',):
        if not isinstance(cfg[key], (int, float)) or not math.isfinite(cfg[key]) or not 0 <= cfg[key] <= 1:
            raise ValueError('invalid probability')
    if cfg['formula_mode'] not in ('rate_multiplier', 'legacy_tick'):
        raise ValueError('unknown formula mode')
    if not 0 < cfg['delete_below'] <= cfg['facts_threshold'] <= cfg['full_threshold'] <= cfg['protected_weight'] <= cfg['max_weight']:
        raise ValueError('invalid weight thresholds')
    for rate in cfg['rates_per_hour'].values():
        if not isinstance(rate, (float, int)) or not math.isfinite(rate) or rate < 0:
            raise ValueError('invalid decay rate')
    for profile in cfg['profiles'].values():
        if not all(math.isfinite(profile[k]) and profile[k] >= 0 for k in ('mult', 'floor')):
            raise ValueError('invalid profile')
    for key in ('reminder_boost', 'used_boost', 'dedup_seconds', 'recycle_days'):
        if not isinstance(cfg[key], (float, int)) or not math.isfinite(cfg[key]) or cfg[key] < 0:
            raise ValueError('invalid policy value: ' + key)
    return cfg


def normalize_content(text, mode):
    """Bridge replays often reformat. 'nfkc_casefold' catches full-width/case
    variants; 'whitespace' stays the conservative default."""
    text = ' '.join(text.split())
    if mode == 'nfkc_casefold':
        text = unicodedata.normalize('NFKC', text).casefold()
    return text


def local_day(now, cfg):
    try:
        tz = ZoneInfo(cfg['timezone'])
    except ZoneInfoNotFoundError:
        if cfg['timezone'] != 'Asia/Shanghai':
            raise
        tz = timezone(timedelta(hours=8))  # Modern daily jobs, Windows without tzdata.
    return datetime.fromtimestamp(now, tz).date().isoformat()


def empty_emotion():
    """L1 snapshot is frozen history, L2 charge is the only fading quantity,
    L3 appraisal is versioned interpretation. Never merge these three."""
    return {'snapshot': None,
            'affect': {'charge': 0.0, 'updated_at': 0.0, 'last_evoked_at': None},
            'appraisal': {'revision': 0, 'current': None, 'at': None, 'evidence': None, 'history': []},
            'sensitive': False}


def emotion_state(row):
    raw = row.get('emotion') or ''
    state = empty_emotion()
    if raw:
        stored = json.loads(raw)
        state['snapshot'] = stored.get('snapshot')
        state['affect'].update(stored.get('affect') or {})
        state['appraisal'].update(stored.get('appraisal') or {})
        state['sensitive'] = bool(stored.get('sensitive'))
    return state


def dump_emotion(state):
    return json.dumps(state, ensure_ascii=False, separators=(',', ':'))


def new_emotion(payload, event_id, now):
    """Build L1/L2/L3 at ingestion. `strength` = arousal x confidence, so an
    unsupported LLM emotion guess collapses to neutral instead of adding noise."""
    state = empty_emotion()
    if not payload:
        return state
    state['sensitive'] = bool(payload.get('sensitive', False))
    valence = payload.get('valence', 0.0)
    arousal = payload.get('arousal', 0.0)
    confidence = payload.get('confidence', 0.0)
    for name, value, low, high in (('valence', valence, -1, 1), ('arousal', arousal, 0, 1),
                                   ('confidence', confidence, 0, 1)):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) \
                or not low <= value <= high:
            raise ValueError('invalid emotion field: ' + name)
    strength = arousal * confidence
    if strength > 0:
        state['snapshot'] = {'valence': valence, 'arousal': arousal, 'confidence': confidence,
                             'strength': strength, 'at': now, 'evidence': event_id}
        state['affect'].update(charge=strength, updated_at=now)
    appraisal = payload.get('appraisal')
    if appraisal is not None:
        if not isinstance(appraisal, str) or not appraisal.strip():
            raise ValueError('appraisal must be non-empty text')
        state['appraisal'].update(revision=1, current=appraisal.strip(), at=now, evidence=event_id)
    return state


def charge_half_life_hours(state, cfg):
    """L2 fading rate is derived from the frozen L1 snapshot, not from itself."""
    emotion = cfg['emotion']
    snapshot = state.get('snapshot') or {}
    hours = emotion['charge_half_life_days'] * 24.0
    hours *= 1 + emotion['arousal_half_life_gain'] * snapshot.get('strength', 0.0)
    if snapshot.get('valence', 0.0) < 0:
        hours *= emotion['negative_valence_half_life_factor']
    return hours


def charge_at(row, now, cfg):
    state = emotion_state(row)
    affect = state['affect']
    if affect['charge'] <= 0 or not state['snapshot']:
        return 0.0
    half_life = charge_half_life_hours(state, cfg)
    if half_life <= 0:
        return 0.0
    hours = max(0.0, (now - affect['updated_at']) / 3600.0)
    return affect['charge'] * math.exp(-math.log(2) * hours / half_life)


def emotion_time_integral(hours, charge, half_life_hours, rate_gain):
    """Integral of dt/(1 + rate_gain*charge(t)) with charge(t)=charge*2^(-t/H).

    Closed form keeps split ticks exactly equal to one tick:
        I(T) = T + (ln(1 + A*e^(-cT)) - ln(1 + A)) / c,  A = rate_gain*charge
    Splitting at T1 gives I(T1) + I(T2) with A1 = A*e^(-cT1), and the two
    logarithms telescope back to I(T). No emotion -> I(T) = T.
    """
    if hours <= 0 or charge <= 0 or rate_gain <= 0 or half_life_hours <= 0:
        return hours
    c = math.log(2) / half_life_hours
    a = rate_gain * charge
    return hours + (math.log1p(a * math.exp(-c * hours)) - math.log1p(a)) / c


def decayed(row, now, cfg):
    weight = float(row['weight'])
    if row['pinned'] or weight >= cfg['protected_weight']:
        return weight
    if row['profile'] == 'permanent':
        return cfg['max_weight']
    hours = max(0, (now - row['last_decay']) / 3600)
    if not hours:
        return weight
    profile = cfg['profiles'][row['profile']]
    rate = cfg['rates_per_hour'][row['curve']]
    if cfg['formula_mode'] == 'legacy_tick':
        value = weight * math.exp(-rate * hours) * profile['mult']
    else:
        # Preserve ESDM D(t), move profile multiplier into rate to make splitting
        # an interval equivalent to one tick. Emotion modulates the RATE through
        # the same closed-form integral, never a per-tick multiplier on weight.
        integral = hours
        if cfg['emotion']['enabled']:
            state = emotion_state(row)
            if state['snapshot']:
                integral = emotion_time_integral(hours, charge_at(row, row['last_decay'], cfg),
                                                 charge_half_life_hours(state, cfg), cfg['emotion']['rate_gain'])
        value = weight * math.exp(-rate * profile['mult'] * integral)
    return min(cfg['max_weight'], max(profile['floor'], value))


def tier_for(weight, cfg, pinned=False):
    if pinned or weight >= cfg['full_threshold']:
        return 'full'
    return 'facts' if weight >= cfg['facts_threshold'] else 'core'


def projection(row, tier, cfg, now):
    facts = json.loads(row['facts'])
    if tier == 'core':
        core = [fact for fact in facts if fact.get('core')]
        facts = core or facts  # Unknown priority must not silently discard facts.
    selected = [{key: fact[key] for key in ('subject', 'predicate', 'object', 'time', 'negated', 'source_ids')}
                for fact in facts]
    payload = {'facts': selected}
    if tier == 'full':
        payload['detail'] = row['body']
    state = emotion_state(row)
    if state['snapshot'] or state['appraisal']['current']:
        # L1 history and L3 current interpretation stay retrievable even when
        # L2 charge has faded to zero. Appraisal history never enters RAG.
        # `charge` is deliberately NOT projected: it fades continuously and would
        # force a nightly resync of every emotional memory. Injection decisions
        # read the live charge from the authority DB via injection_allowed().
        snapshot = state['snapshot'] or {}
        payload['emotion'] = {
            'valence_at_event': snapshot.get('valence'),
            'arousal_at_event': snapshot.get('arousal'),
            'at': snapshot.get('at'),
            'appraisal': state['appraisal']['current'],
            'sensitive': state['sensitive'],
        }
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':'))


class MemoryEngine:
    def __init__(self, path, cfg=None):
        self.cfg = cfg or load_config()
        self.db = sqlite3.connect(path, timeout=15)
        self.db.row_factory = sqlite3.Row
        # WAL lets the chat path keep reading while the nightly warehouse writes;
        # busy_timeout gives short lock contention a bounded wait instead of an error.
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.execute('PRAGMA busy_timeout=15000')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memories(
 id TEXT PRIMARY KEY,body TEXT NOT NULL,facts TEXT NOT NULL,source_ids TEXT NOT NULL,
 weight REAL NOT NULL,curve TEXT NOT NULL,profile TEXT NOT NULL,pinned INTEGER NOT NULL DEFAULT 0,
 visibility TEXT NOT NULL,owner TEXT NOT NULL,created_at REAL NOT NULL,last_decay REAL NOT NULL,
 revision INTEGER NOT NULL,tier TEXT NOT NULL,text TEXT NOT NULL,maintenance_day TEXT,
 deleted_at REAL,purged INTEGER NOT NULL DEFAULT 0,emotion TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS events(
 id TEXT PRIMARY KEY,peer TEXT NOT NULL,hash TEXT NOT NULL,at REAL NOT NULL,kind TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_transport ON events(peer,hash,at);
CREATE TABLE IF NOT EXISTS daily_draws(
 memory_id TEXT NOT NULL,day TEXT NOT NULL,draw REAL NOT NULL,deleted INTEGER NOT NULL,
 PRIMARY KEY(memory_id,day));
CREATE TABLE IF NOT EXISTS outbox(
 id INTEGER PRIMARY KEY AUTOINCREMENT,memory_id TEXT NOT NULL,revision INTEGER NOT NULL,
 operation TEXT NOT NULL,payload TEXT NOT NULL,ack INTEGER NOT NULL DEFAULT 0,
 attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT NOT NULL DEFAULT '',
 dead INTEGER NOT NULL DEFAULT 0,created_at REAL NOT NULL DEFAULT 0,
 UNIQUE(memory_id,revision));
CREATE TABLE IF NOT EXISTS audit(
 id INTEGER PRIMARY KEY AUTOINCREMENT,memory_id TEXT,action TEXT NOT NULL,at REAL NOT NULL,detail TEXT NOT NULL);
''')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO meta VALUES(?,?)', ('lottery_seed', secrets.token_hex(32)))
            # Plan signing gets its own seed; one key must not serve two purposes.
            self.db.execute('INSERT OR IGNORE INTO meta VALUES(?,?)', ('plan_seed', secrets.token_hex(32)))
        columns = [r[1] for r in self.db.execute('PRAGMA table_info(memories)')]
        if 'emotion' not in columns:  # In-place upgrade of pre-emotion reference DBs.
            with self.db:
                self.db.execute("ALTER TABLE memories ADD COLUMN emotion TEXT NOT NULL DEFAULT ''")
        outbox_columns = [r[1] for r in self.db.execute('PRAGMA table_info(outbox)')]
        for column, ddl in (('attempts', "INTEGER NOT NULL DEFAULT 0"),
                            ('last_error', "TEXT NOT NULL DEFAULT ''"),
                            ('dead', "INTEGER NOT NULL DEFAULT 0"),
                            ('created_at', "REAL NOT NULL DEFAULT 0")):
            if column not in outbox_columns:  # In-place upgrade of pre-v3 outbox.
                with self.db:
                    self.db.execute(f'ALTER TABLE outbox ADD COLUMN {column} {ddl}')

    def close(self):
        self.db.close()

    def row(self, mid):
        row = self.db.execute('SELECT * FROM memories WHERE id=?', (mid,)).fetchone()
        return dict(row) if row else None

    def _audit(self, mid, action, now, **details):
        self.db.execute('INSERT INTO audit(memory_id,action,at,detail) VALUES(?,?,?,?)',
                        (mid, action, now, json.dumps(details)))

    def _queue(self, mid):
        row = self.row(mid)
        operation = 'delete' if row['deleted_at'] is not None or row['profile'] == 'archived' else 'upsert'
        # No body in delete jobs. Upserts carry ACL and revision for the adapter.
        payload = {'id': mid, 'revision': row['revision']}
        if operation == 'upsert':
            payload.update(text=row['text'], weight=row['weight'], visibility=row['visibility'], owner=row['owner'],
                           source_ids=json.loads(row['source_ids']))
        self.db.execute('INSERT INTO outbox(memory_id,revision,operation,payload,created_at) VALUES(?,?,?,?,?)',
                        (mid, row['revision'], operation, json.dumps(payload, ensure_ascii=False), time.time()))

    def process_event(self, event, now=None):
        """Trusted intake envelope; LLM proposals must be validated before entry."""
        now = time.time() if now is None else now
        if event['kind'] not in ('human_message', 'delivery') or not event['id'] or not event['peer_id']:
            raise ValueError('invalid source event')
        if event.get('visibility', 'shared') not in ('shared', 'private'):
            raise ValueError('invalid visibility')
        digest = hashlib.sha256(normalize_content(event['content'], self.cfg['dedup_normalize']).encode()).hexdigest()
        # Explicit transaction also protects same-message replay and concurrent
        # reminders. Every proposal from the same message is handled together.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            duplicate = self.db.execute('SELECT 1 FROM events WHERE id=?', (event['id'],)).fetchone()
            if event['kind'] == 'human_message':
                duplicate = duplicate or self.db.execute('SELECT 1 FROM events WHERE peer=? AND hash=? AND at>=? AND kind=?',
                    (event['peer_id'], digest, now-self.cfg['dedup_seconds'], 'human_message')).fetchone()
            if duplicate:
                self.db.rollback()
                return {'status': 'duplicate', 'changed': 0}
            self.db.execute('INSERT INTO events VALUES(?,?,?,?,?)', (event['id'], event['peer_id'], digest, now, event['kind']))
            touched = set()
            for action in event.get('actions', []):
                mid = action['id']
                if mid in touched:
                    raise ValueError('one message may affect each memory only once')
                touched.add(mid)
                if action['kind'] == 'add':
                    if event['kind'] != 'human_message' or self.row(mid):
                        raise ValueError('add requires a new memory and human source')
                    facts = action['facts']
                    if not facts or not isinstance(facts, list):
                        raise ValueError('atomic facts are required')
                    for fact in facts:
                        if not all(k in fact for k in ('subject', 'predicate', 'object', 'time', 'negated', 'source_ids')):
                            raise ValueError('missing fact provenance/identity fields')
                        if not all(isinstance(fact[k],str) and fact[k] for k in ('subject','predicate','object')):
                            raise ValueError('invalid atomic fact')
                        if type(fact['negated']) is not bool or not isinstance(fact['source_ids'],list) or not fact['source_ids']:
                            raise ValueError('invalid fact provenance')
                        if event['id'] not in fact['source_ids'] or not all(isinstance(s,str) for s in fact['source_ids']):
                            raise ValueError('new facts must cite this verified input event')
                    weight = action.get('weight', self.cfg['initial_weight'])
                    if type(weight) not in (int,float) or not math.isfinite(weight) or not 0 <= weight <= self.cfg['max_weight']:
                        raise ValueError('invalid weight')
                    curve, profile = action.get('curve','fact'), action.get('profile','standard')
                    if curve not in self.cfg['rates_per_hour'] or profile not in self.cfg['profiles']:
                        raise ValueError('unknown curve/profile')
                    visibility = event.get('visibility', 'shared')
                    owner = event.get('sender_id', '')
                    if visibility == 'private' and not owner:
                        raise ValueError('private memory needs a stable owner ID')
                    emotion = new_emotion(action.get('emotion'), event['id'], now)
                    if emotion['snapshot'] and self.cfg['emotion']['enabled']:
                        # Encoding boost must never accidentally grant the W>=90
                        # permanent protection: cap below protected_weight and
                        # never lower an explicitly trusted higher weight.
                        boosted = weight * (1 + self.cfg['emotion']['encoding_boost']
                                            * emotion['snapshot']['strength'])
                        weight = max(weight, min(boosted, self.cfg['protected_weight'] - 1))
                    self.db.execute('''INSERT INTO memories(id,body,facts,source_ids,weight,curve,profile,
                        visibility,owner,created_at,last_decay,revision,tier,text,emotion) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (mid, action.get('body',''), json.dumps(facts,ensure_ascii=False), json.dumps([event['id']]),
                         weight, curve, profile, visibility, owner, now, now, 1, tier_for(weight,self.cfg), '',
                         dump_emotion(emotion)))
                elif action['kind'] in ('remind','used'):
                    row = self.row(mid)
                    if not row or row['purged']:
                        raise ValueError('facts unavailable; relearn from fresh evidence')
                    requester=event.get('sender_id') if event['kind']=='human_message' else event.get('requester_id')
                    if row['visibility'] == 'private' and row['owner'] != requester:
                        raise ValueError('private memory access denied')
                    if action['kind'] == 'remind':
                        if event['kind'] != 'human_message':
                            raise ValueError('reminder requires fresh human evidence')
                        boost = self.cfg['reminder_boost']
                    else:
                        if (event['kind'] != 'delivery' or not event.get('delivered') or
                                row['deleted_at'] is not None or action.get('revision') != row['revision']):
                            raise ValueError('use requires confirmed delivery of current live revision')
                        boost = self.cfg['used_boost']
                    sources = json.loads(row['source_ids'])
                    if action['kind'] == 'remind':sources.append(event['id'])
                    settle_at = max(now, row['last_decay'])
                    emotion = emotion_state(row)
                    charge = charge_at(row, settle_at, self.cfg)
                    if action['kind'] == 'remind' and emotion['snapshot']:
                        # Genuine human re-evocation recharges L2 toward the L1
                        # snapshot. `used` (our own retrieval) never recharges.
                        original = emotion['snapshot']['strength']
                        charge += self.cfg['emotion']['remind_recharge'] * (original - charge)
                        emotion['affect']['last_evoked_at'] = settle_at
                    emotion['affect'].update(charge=min(1.0, max(0.0, charge)), updated_at=settle_at)
                    self.db.execute('''UPDATE memories SET weight=?,last_decay=?,revision=revision+1,
                        deleted_at=NULL,source_ids=?,emotion=? WHERE id=?''',
                        (min(self.cfg['max_weight'], decayed(row,now,self.cfg)+boost), settle_at,
                         json.dumps(sources), dump_emotion(emotion), mid))
                elif action['kind'] == 'appraise':
                    # L3: a new human reinterpretation. It creates a revision and
                    # may relieve L2, but it never rewrites L1 and never boosts W.
                    row = self.row(mid)
                    if not row or row['purged']:
                        raise ValueError('facts unavailable; relearn from fresh evidence')
                    if event['kind'] != 'human_message':
                        raise ValueError('appraisal requires fresh human evidence')
                    if row['visibility'] == 'private' and row['owner'] != event.get('sender_id'):
                        raise ValueError('private memory access denied')
                    text = action.get('text')
                    if not isinstance(text, str) or not text.strip():
                        raise ValueError('appraisal text required')
                    emotion = emotion_state(row)
                    appraisal = emotion['appraisal']
                    if appraisal['current']:
                        appraisal['history'].append({'revision': appraisal['revision'], 'text': appraisal['current'],
                                                     'at': appraisal['at'], 'evidence': appraisal['evidence']})
                    appraisal.update(revision=appraisal['revision'] + 1, current=text.strip(),
                                     at=now, evidence=event['id'])
                    settle_at = max(now, row['last_decay'])
                    charge = charge_at(row, settle_at, self.cfg) * (1 - self.cfg['emotion']['reappraisal_charge_relief'])
                    emotion['affect'].update(charge=charge, updated_at=settle_at)
                    self.db.execute('UPDATE memories SET weight=?,last_decay=?,emotion=?,revision=revision+1 WHERE id=?',
                                    (decayed(row, now, self.cfg), settle_at, dump_emotion(emotion), mid))
                else:
                    raise ValueError('unknown action')
                row = self.row(mid)
                tier = tier_for(row['weight'], self.cfg, row['pinned'])
                self.db.execute('UPDATE memories SET tier=?,text=? WHERE id=?', (tier,projection(row,tier,self.cfg,now),mid))
                self._queue(mid)
                self._audit(mid,action['kind'],now,event=event['id'])
            self.db.commit()
            return {'status':'ok','changed':len(touched)}
        except BaseException:
            self.db.rollback()
            raise

    def plan(self, now=None):
        now = time.time() if now is None else now
        day = local_day(now,self.cfg)
        seed = bytes.fromhex(self.db.execute('SELECT value FROM meta WHERE key=?',('lottery_seed',)).fetchone()[0])
        actions=[]
        for raw in self.db.execute('SELECT * FROM memories WHERE deleted_at IS NULL AND purged=0'):
            row=dict(raw)
            if row['maintenance_day']==day or now<row['last_decay']:continue
            weight=decayed(row,now,self.cfg)
            tier=tier_for(weight,self.cfg,row['pinned'])
            draw=None;delete=False
            if weight < self.cfg['delete_below'] and not row['pinned'] and row['profile'] not in ('permanent','archived'):
                # Stable private random draw: restart/dry-run/retry never rerolls.
                draw=(int.from_bytes(hmac.new(seed,(day+'|'+row['id']).encode(),hashlib.sha256).digest()[:8],'big') >> 11)/2**53
                delete=draw < self.cfg['delete_probability_per_day']
            actions.append({'id':row['id'],'expected_revision':row['revision'],'weight':weight,
                'tier':tier,'text':projection(row,tier,self.cfg,now),'delete':delete,'draw':draw})
        plan={'day':day,'at':now,'actions':actions}
        plan['signature']=self._sign_plan(plan)
        return plan

    def _sign_plan(self, plan):
        seed=bytes.fromhex(self.db.execute('SELECT value FROM meta WHERE key=?',('plan_seed',)).fetchone()[0])
        canonical=json.dumps({key:plan[key] for key in ('day','at','actions')},ensure_ascii=False,
                             sort_keys=True,separators=(',',':')).encode()
        return hmac.new(seed,canonical,hashlib.sha256).hexdigest()

    def apply_plan(self, plan):
        if not hmac.compare_digest(str(plan.get('signature','')),self._sign_plan(plan)):
            raise ValueError('maintenance plans must come unchanged from this deterministic engine')
        counts={'processed':0,'compressed':0,'deleted':0,'skipped_changed':0,'unchanged':0}
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for action in plan['actions']:
                row=self.row(action['id'])
                if (not row or row['revision']!=action['expected_revision'] or row['deleted_at'] is not None
                        or row['maintenance_day']==plan['day']):
                    counts['skipped_changed']+=1;continue
                if (not action['delete'] and action['weight']==row['weight'] and action['tier']==row['tier']
                        and action['text']==row['text']):
                    # State already settled (protected/pinned/floor/archived). Mark the
                    # day but do not churn revision or the outbox for a no-op.
                    self.db.execute('UPDATE memories SET maintenance_day=? WHERE id=?',(plan['day'],row['id']))
                    counts['unchanged']+=1;continue
                self.db.execute('''UPDATE memories SET weight=?,tier=?,text=?,last_decay=?,maintenance_day=?,
                    revision=revision+1,deleted_at=? WHERE id=? AND revision=?''',
                    (action['weight'],action['tier'],'' if action['delete'] else action['text'],
                     max(plan['at'],row['last_decay']),plan['day'],plan['at'] if action['delete'] else None,
                     row['id'],row['revision']))
                emotion = emotion_state(row)
                if emotion['snapshot']:
                    # L2 fades on the same settlement timestamp as the weight, so
                    # charge stored in the DB always means "charge at last_decay".
                    settle_at = max(plan['at'], row['last_decay'])
                    emotion['affect'].update(charge=charge_at(row, settle_at, self.cfg), updated_at=settle_at)
                    self.db.execute('UPDATE memories SET emotion=? WHERE id=?', (dump_emotion(emotion), row['id']))
                if action['draw'] is not None:
                    self.db.execute('INSERT OR IGNORE INTO daily_draws VALUES(?,?,?,?)',
                        (row['id'],plan['day'],action['draw'],int(action['delete'])))
                self._queue(row['id'])
                self._audit(row['id'],'nightly',plan['at'],weight_before=row['weight'],weight_after=action['weight'],
                            tier=action['tier'],deleted=action['delete'],draw=action['draw'])
                counts['processed']+=1
                counts['compressed']+=int(len(action['text'])<len(row['text']) and not action['delete'])
                counts['deleted']+=int(action['delete'])
        return counts

    def active(self, requester=''):
        return [dict(r) for r in self.db.execute('''SELECT id,text,weight,revision,visibility,owner FROM memories
            WHERE deleted_at IS NULL AND purged=0 AND profile!='archived' AND
            (visibility='shared' OR owner=?)''',(requester,))]

    def injection_allowed(self, mid, now=None, user_initiated=False):
        """L2 gates PROACTIVE injection only. Faded charge never blocks recall:
        a user asking about their own past always gets L1 back."""
        now = time.time() if now is None else now
        row = self.row(mid)
        if not row:
            return False
        state = emotion_state(row)
        snapshot = state['snapshot'] or {}
        sensitive = state['sensitive']
        if snapshot and snapshot.get('valence', 0) < 0 and \
                charge_at(row, now, self.cfg) >= self.cfg['emotion']['auto_sensitive_charge']:
            sensitive = True
        return bool(user_initiated) if sensitive else True

    def rank_score(self, relevance, weight, mood_similarity=None):
        """Mood-congruent retrieval is an ordering signal, never a truth signal."""
        rag = self.cfg['rag']
        base = rag['relevance_weight'] * relevance + rag['memory_weight'] * (weight / self.cfg['max_weight'])
        if mood_similarity is None:
            return base
        mood_weight = self.cfg['emotion']['mood_weight']
        return (1 - mood_weight) * base + mood_weight * max(-1.0, min(1.0, mood_similarity))

    def emotion_view(self, mid, now=None):
        now = time.time() if now is None else now
        row = self.row(mid)
        if not row:
            return None
        state = emotion_state(row)
        return {'snapshot': state['snapshot'], 'charge': charge_at(row, now, self.cfg),
                'affect': state['affect'], 'appraisal': state['appraisal'], 'sensitive': state['sensitive']}

    def outbox(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM outbox WHERE ack=0 AND dead=0 ORDER BY id')]

    def fail(self, event_id, error):
        """Adapters call this for a real failed sync attempt. Reaching
        outbox_max_attempts dead-letters the event so one poisoned event
        cannot stall the queue forever."""
        with self.db:
            row = self.db.execute('SELECT attempts FROM outbox WHERE id=? AND ack=0', (event_id,)).fetchone()
            if not row:
                raise ValueError('unknown or acknowledged outbox event')
            attempts = row[0] + 1
            dead = 1 if attempts >= self.cfg['outbox_max_attempts'] else 0
            self.db.execute('UPDATE outbox SET attempts=?,last_error=?,dead=? WHERE id=?',
                            (attempts, str(error)[:500], dead, event_id))
        return {'attempts': attempts, 'dead': bool(dead)}

    def dead_letters(self):
        """Dead letters block purge for their memory on purpose: an unconfirmed
        delete must receive human attention, not silent expiry."""
        return [dict(r) for r in self.db.execute('SELECT * FROM outbox WHERE dead=1 AND ack=0 ORDER BY id')]

    def retry_dead(self, event_id):
        with self.db:
            cursor = self.db.execute("UPDATE outbox SET dead=0,attempts=0,last_error='' WHERE id=? AND ack=0 AND dead=1",
                                     (event_id,))
            if cursor.rowcount == 0:
                raise ValueError('no such dead-lettered event')

    def sweep_acked(self, now=None, dry_run=True):
        """Acked rows are receipts, not storage; sweep after retention. Unacked
        and dead-lettered rows are never swept."""
        now = time.time() if now is None else now
        cutoff = now - self.cfg['outbox_acked_retention_days'] * 86400
        eligible = self.db.execute('SELECT COUNT(*) FROM outbox WHERE ack=1 AND created_at<?', (cutoff,)).fetchone()[0]
        if not dry_run and eligible:
            with self.db:
                self.db.execute('DELETE FROM outbox WHERE ack=1 AND created_at<?', (cutoff,))
        return eligible

    def acknowledge(self, event_id):
        """Call only after actual index acknowledgment or proven supersession."""
        with self.db:self.db.execute('UPDATE outbox SET ack=1 WHERE id=?',(event_id,))

    def purge(self, now=None):
        now=time.time() if now is None else now
        with self.db:
            rows=self.db.execute('''SELECT id FROM memories WHERE purged=0 AND deleted_at<=?
                AND NOT EXISTS(SELECT 1 FROM outbox WHERE memory_id=memories.id AND ack=0)''',
                (now-self.cfg['recycle_days']*86400,)).fetchall()
            for row in rows:
                mid=row['id']
                self.db.execute("UPDATE memories SET body='',facts='[]',text='',source_ids='[]',purged=1 WHERE id=?",(mid,))
                self.db.execute('DELETE FROM outbox WHERE memory_id=? AND ack=1',(mid,))
                self._audit(mid,'purge',now)
        return len(rows)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db')
    parser.add_argument('--config')
    parser.add_argument('--preset',help='preset name (e.g. slow-forget, evergreen) or a path to a preset overlay')
    parser.add_argument('--now',type=float,help='Unix time override for isolated replay only')
    subs=parser.add_subparsers(dest='command',required=True)
    subs.add_parser('init')
    subs.add_parser('presets',help='list the presets shipped with this package')
    ingest=subs.add_parser('event');ingest.add_argument('file')
    maintain=subs.add_parser('maintain');maintain.add_argument('--apply',action='store_true')
    subs.add_parser('outbox')
    subs.add_parser('dead')
    retry=subs.add_parser('retry');retry.add_argument('id',type=int)
    sweep=subs.add_parser('sweep');sweep.add_argument('--apply',action='store_true')
    active=subs.add_parser('active');active.add_argument('--requester',default='')
    purge=subs.add_parser('purge');purge.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if args.command=='presets':
        for preset_id in list_presets():
            preset=resolve_preset(preset_id)
            print('%-12s %s' % (preset_id,title(preset)))
            if preset.get('note'):
                print('             %s' % preset['note'])
        return
    if not args.db:
        parser.error('--db is required for ' + args.command)
    if args.now is not None and os.environ.get('ES_MEMORY_REPLAY') != '1':
        parser.error('--now is for isolated replay only; set ES_MEMORY_REPLAY=1 to acknowledge')
    engine=MemoryEngine(args.db,load_config(args.config,args.preset))
    try:
        if args.command=='init':result={'status':'ready'}
        elif args.command=='event':result=engine.process_event(json.loads(Path(args.file).read_text(encoding='utf-8-sig')),args.now)
        elif args.command=='maintain':
            plan=engine.plan(args.now)
            result=engine.apply_plan(plan) if args.apply else {'dry_run':True,'date':plan['day'],
                'changes':[{k:v for k,v in action.items() if k!='text'} for action in plan['actions']]}
        elif args.command=='outbox':result=engine.outbox()
        elif args.command=='dead':result=engine.dead_letters()
        elif args.command=='retry':engine.retry_dead(args.id);result={'status':'requeued','id':args.id}
        elif args.command=='sweep':result=({'swept':engine.sweep_acked(args.now,dry_run=False)} if args.apply
            else {'dry_run':True,'eligible':engine.sweep_acked(args.now)})
        elif args.command=='active':result=engine.active(args.requester)
        elif args.command=='purge':result={'purged':engine.purge(args.now)} if args.apply else {'dry_run':True,'message':'purge requires --apply; only acknowledged tombstones past recycle TTL are eligible'}
        print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:engine.close()


if __name__=='__main__':main()
