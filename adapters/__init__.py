"""Reference adapters that wire the memory engine to a real RAG index.

Nothing in this package is installed automatically. The modules here implement
the ``RAGAdapter`` contract from ``references/integration.md``:

    upsert(memory_id, revision, text, metadata)
    delete(memory_id)
    search(query, acl_filter, limit)
    rerank(query, candidates)
    count(memory_id)

Standard library only, like the rest of the package. Credentials are read from
the environment or from a private config file *outside* the repository and are
never written into the index, the logs, or a commit.
"""

__all__ = ['embedding', 'vector_store', 'rerank', 'rag_adapter', 'sync', 'context_pager']
