"""Embedding clients.

Three backends, one interface:

* ``SiliconFlowEmbedding``  -- OpenAI-compatible ``POST /v1/embeddings``
* ``OllamaEmbedding``       -- local ``POST /api/embed`` (no key, no egress)
* ``HashingEmbedding``      -- deterministic offline stub for unit tests only

Standard library only. The API key is never logged, never stored in the index,
and never returned in an error message.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from pathlib import Path

DEFAULT_TIMEOUT = 60


class EmbeddingError(RuntimeError):
    """Raised when an embedding backend cannot serve a request."""


def _opener():
    """urllib opener that ignores HTTP_PROXY/HTTPS_PROXY.

    The machine may have proxy env vars pointing at a client that is not
    running; a provider call should not die on a dead local proxy. Pass an
    explicit proxy at the call site if one is genuinely required.
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post_json(url, payload, headers, timeout=DEFAULT_TIMEOUT):
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    try:
        with _opener().open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', 'replace')[:400]
        # Only the provider host and the body are shown; headers (and the key)
        # are deliberately absent.
        raise EmbeddingError('HTTP %s from %s: %s' % (exc.code, url.split('/')[2], body)) from exc
    except urllib.error.URLError as exc:
        raise EmbeddingError('network error: %s' % (exc.reason,)) from exc
    except (TimeoutError, OSError) as exc:
        raise EmbeddingError('transport error: %s' % (exc,)) from exc


def _config_path():
    return Path(os.path.expanduser('~')) / '.es-memory-rag' / 'config.json'


def read_private_config(path=None):
    """Read the private config file. Returns {} when it does not exist."""
    p = Path(path) if path else _config_path()
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def resolve_api_key(explicit=None, env_var='SILICONFLOW_API_KEY', config_key='siliconflow_api_key'):
    """Explicit argument > environment variable > private config file."""
    if explicit:
        return explicit
    env = os.environ.get(env_var)
    if env:
        return env
    return read_private_config().get(config_key) or ''


def tokenize(text):
    """Split text into comparable tokens.

    Latin runs become whole words; CJK characters become one token each so
    that two Chinese sentences about the same topic actually share tokens.
    """
    out = []
    buf = []

    def flush():
        if buf:
            out.append(''.join(buf))
            del buf[:]

    for ch in text or '':
        if '\u4e00' <= ch <= '\u9fff':
            flush()
            out.append(ch)
        elif ch.isalnum():
            buf.append(ch.lower())
        else:
            flush()
    flush()
    return out


class EmbeddingClient:
    """Common interface: a list of texts in, a list of vectors out."""

    backend = 'base'
    model_version = '1'

    def __init__(self, model, dim=1024, timeout=DEFAULT_TIMEOUT):
        self.model = model
        self.dim = dim
        self.timeout = timeout

    def embed(self, texts):
        raise NotImplementedError

    def embed_one(self, text):
        return self.embed([text])[0]

    def cache_identity(self):
        """Non-secret vector-space identity; bump model_version on weight changes.

        Endpoint credentials/query/fragment are excluded. Deployments serving
        different weights under the same model name must set model_version.
        """
        endpoint = urlsplit(getattr(self, 'base_url', ''))
        provider = self.backend
        if endpoint.hostname:
            # Hash endpoint location, never credentials or query parameters.
            location = '%s://%s:%s%s' % (endpoint.scheme, endpoint.hostname,
                                       endpoint.port or '', endpoint.path.rstrip('/'))
            provider += ':' + hashlib.sha256(location.encode()).hexdigest()
        return {'provider': provider, 'model': self.model,
                'version': self.model_version, 'dim': self.dim}


class SiliconFlowEmbedding(EmbeddingClient):
    """OpenAI-compatible embeddings endpoint (SiliconFlow and friends).

    The key is resolved from the argument, then ``SILICONFLOW_API_KEY``, then
    ``~/.es-memory-rag/config.json`` -- a file outside the repository. It is
    only ever placed in an Authorization header.
    """

    backend = 'siliconflow'

    def __init__(self, model='BAAI/bge-m3', api_key=None, base_url=None, dim=1024, timeout=DEFAULT_TIMEOUT):
        key = resolve_api_key(api_key)
        if not key:
            raise EmbeddingError(
                'no API key: pass api_key=, set SILICONFLOW_API_KEY, or create %s' % _config_path())
        self._key = key
        self.base_url = (base_url or 'https://api.siliconflow.cn/v1').rstrip('/')
        super().__init__(model, dim=dim, timeout=timeout)

    def embed(self, texts):
        texts = list(texts)
        if not texts:
            return []
        payload = {'model': self.model, 'input': texts, 'encoding_format': 'float'}
        headers = {'Authorization': 'Bearer ' + self._key, 'Content-Type': 'application/json'}
        body = _post_json(self.base_url + '/embeddings', payload, headers, self.timeout)
        data = body.get('data') or []
        if len(data) != len(texts):
            raise EmbeddingError('provider returned %d vectors for %d inputs' % (len(data), len(texts)))
        data = sorted(data, key=lambda d: d.get('index', 0))
        vectors = [list(map(float, d.get('embedding') or [])) for d in data]
        for vec in vectors:
            if len(vec) != self.dim:
                raise EmbeddingError('expected dim %d, provider returned %d' % (self.dim, len(vec)))
        return vectors


class OllamaEmbedding(EmbeddingClient):
    """Local Ollama embeddings. No API key, no network egress."""

    backend = 'ollama'

    def __init__(self, model='bge-m3', host=None, dim=1024, timeout=120):
        host = host or os.environ.get('OLLAMA_HOST') or '127.0.0.1:11434'
        if not host.startswith('http'):
            host = 'http://' + host
        self.base_url = host.rstrip('/')
        super().__init__(model, dim=dim, timeout=timeout)

    def embed(self, texts):
        texts = list(texts)
        if not texts:
            return []
        payload = {'model': self.model, 'input': texts}
        body = _post_json(self.base_url + '/api/embed', payload, {'Content-Type': 'application/json'}, self.timeout)
        vectors = [list(map(float, v)) for v in (body.get('embeddings') or [])]
        if len(vectors) != len(texts):
            raise EmbeddingError('ollama returned %d vectors for %d inputs' % (len(vectors), len(texts)))
        for vec in vectors:
            if len(vec) != self.dim:
                raise EmbeddingError('expected dim %d, got %d' % (self.dim, len(vec)))
        return vectors


class HashingEmbedding(EmbeddingClient):
    """Deterministic token-hashing vectorizer for offline tests.

    It is **not** semantic. It exists so unit tests can assert ranking,
    reuse and delete behaviour without a network call or a paid key.
    """

    backend = 'hashing'

    def __init__(self, dim=64):
        super().__init__(model='hashing', dim=dim, timeout=5)
        self.calls = 0
        self.embedded_texts = []

    @staticmethod
    def _tokens(text):
        return tokenize(text)

    def embed(self, texts):
        texts = list(texts)
        self.calls += 1
        self.embedded_texts.extend(texts)
        vectors = []
        for text in texts:
            vec = [0.0] * self.dim
            for token in self._tokens(text):
                slot = int(hashlib.md5(token.encode('utf-8')).hexdigest()[:8], 16) % self.dim
                vec[slot] += 1.0
            norm = sum(v * v for v in vec) ** 0.5
            if norm:
                vec = [v / norm for v in vec]
            vectors.append(vec)
        return vectors


def build_embedding(provider=None, model=None, api_key=None, dim=1024, config=None):
    """Build a client from the private config file.

    Config shape (all keys optional)::

        {
          "embedding": {"provider": "siliconflow", "model": "BAAI/bge-m3", "dim": 1024},
          "siliconflow_api_key": "sk-..."
        }

    ``provider`` may be ``siliconflow`` or ``ollama``. Anything else is a
    configuration error rather than a silent fallback.
    """
    cfg = config if config is not None else read_private_config()
    section = cfg.get('embedding') or {}
    provider = (provider or section.get('provider') or 'siliconflow').lower()
    model = model or section.get('model')
    dim = int(section.get('dim') or dim)
    if provider == 'siliconflow':
        client = SiliconFlowEmbedding(model=model or 'BAAI/bge-m3', api_key=api_key, dim=dim)
    elif provider == 'ollama':
        client = OllamaEmbedding(model=model or 'bge-m3', dim=dim)
    else:
        raise EmbeddingError('unknown embedding provider: %s' % provider)
    client.model_version = str(section.get('version') or '1')
    return client
