"""Named configuration presets.

The mode these presets implement is called **请你慢一点遗忘我** -- "please
forget me a little more slowly". Two levels ship with it:

``slow-forget`` (请你慢一点遗忘我)
    The standard level. Decay is 20x slower, the profile floors keep memories
    above the deletion threshold, so the nightly lottery effectively never
    fires. Facts are kept for decades; the full text still ages out of the
    projection eventually.

``evergreen`` (请你慢一点遗忘我 · 硅基长生)
    The extreme level. Decay is 100x slower, the floors hold every memory at
    the "important" line, the compression thresholds drop to 1 so the
    projection keeps the original text forever, and random deletion is
    switched off. Storage and embedding cost then only ever grow.

A preset is an **overlay**, not a copy: it states only what it changes, and the
base is always the current default config. That distinction matters. A full
copy of the config would silently rot the moment the default gains a key, and
a preset that rots is worse than no preset at all -- it looks like it works.

    load_config(preset='slow-forget')              # by id
    load_config(preset='请你慢一点遗忘我')            # by display name
    load_config(preset='D:/tmp/any.json')           # an absolute path

The overlay is deep-merged: nested objects merge key by key, everything else is
replaced. So ``{"profiles": {"standard": {"floor": 30}}}`` raises the standard
profile's floor and leaves its ``mult`` alone.

Both presets change *storage and decay*. Neither touches the injection gate:
sensitive or high-negative-load memories are still not brought up
proactively, and the injection token budget is unchanged.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PRESET_DIR = PACKAGE_ROOT / 'assets' / 'presets'


def merge(base, overlay):
    """Deep merge two config fragments. Dicts merge key by key."""
    if isinstance(base, dict) and isinstance(overlay, dict):
        out = dict(base)
        for key, value in overlay.items():
            out[key] = merge(base[key], value) if key in base else value
        return out
    return overlay


def _read(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('config'), dict):
        raise ValueError('preset %s has no "config" object' % path)
    data.setdefault('id', path.stem)
    data.setdefault('name', path.stem)
    data.setdefault('label', '')
    data['path'] = str(path)
    return data


def list_presets():
    """Ids of the presets shipped with the package."""
    if not PRESET_DIR.is_dir():
        return []
    return sorted(p.stem for p in PRESET_DIR.glob('*.json'))


def resolve_preset(name_or_path):
    """Return the preset dict for an id, a display name, or a JSON file.

    An id without a path separator is looked up in ``assets/presets``. A
    display name (e.g. ``请你慢一点遗忘我``) is matched against the ``name``
    field of every shipped preset. Anything that looks like a file is read
    from disk.
    """
    if not name_or_path:
        raise ValueError('preset name is empty')
    text = str(name_or_path)
    looks_like_path = text.endswith('.json') or os.sep in text or '/' in text
    if looks_like_path:
        path = Path(text)
        if not path.exists():
            raise ValueError('no such preset file: %s' % path)
        return _read(path)

    direct = PRESET_DIR / (text + '.json')
    if direct.exists():
        return _read(direct)

    for candidate in sorted(PRESET_DIR.glob('*.json')) if PRESET_DIR.is_dir() else []:
        try:
            data = _read(candidate)
        except ValueError:
            continue
        if text in (data.get('name'), data.get('id')):
            return data

    available = ', '.join('%s (%s)' % (title(_read(p)), p.stem)
                          for p in sorted(PRESET_DIR.glob('*.json'))) or 'none'
    raise ValueError('unknown preset %r (available: %s)' % (text, available))


def apply_preset(cfg, preset):
    """Return a new config with the preset overlay applied to it."""
    return merge(cfg, preset['config'])


def title(preset):
    """The user-facing name of a preset, e.g. 请你慢一点遗忘我（标准档）."""
    name = preset.get('name', '')
    label = preset.get('label', '')
    if not name:
        return label
    return '%s（%s）' % (name, label) if label else name


def describe(preset):
    """One-line summary of a preset, for CLI output."""
    note = preset.get('note', '')
    return '%s -- %s' % (title(preset), note) if note else title(preset)
