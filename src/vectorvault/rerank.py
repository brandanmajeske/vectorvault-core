"""Opt-in Cohere Rerank via Bedrock agent-runtime (V-51)."""

from __future__ import annotations

from typing import Any, Protocol

RERANK_MODEL_ID = "cohere.rerank-v3-5:0"
RERANK_MAX_DOCUMENTS = 10


class RankableHit(Protocol):
    key: str
    metadata: dict[str, Any]


def rerank_model_arn(region: str) -> str:
    return f"arn:aws:bedrock:{region}::foundation-model/{RERANK_MODEL_ID}"


def _document_text(metadata: dict[str, Any]) -> str:
    summary = metadata.get("content_summary")
    if summary:
        return str(summary)
    content = metadata.get("content")
    if content:
        text = str(content)
        return text[:512] if len(text) > 512 else text
    return metadata.get("task_id") or metadata.get("canonical_id") or ""


def rerank_hits(
    hits: list[Any],
    query: str,
    *,
    region: str,
    rerank_client: Any | None = None,
    max_documents: int = RERANK_MAX_DOCUMENTS,
) -> list[Any]:
    """Re-order collapsed hits with Cohere Rerank 3.5. Returns input on empty/failure."""
    if not hits or not query.strip():
        return hits
    head = hits[:max_documents]
    tail = hits[max_documents:]
    texts = [_document_text(h.metadata) for h in head]
    if not any(texts):
        return hits

    client = rerank_client
    if client is None:
        import boto3

        client = boto3.client("bedrock-agent-runtime", region_name=region)

    sources = [
        {
            "type": "INLINE",
            "inlineDocumentSource": {
                "type": "TEXT",
                "textDocument": {"text": text or "(empty)"},
            },
        }
        for text in texts
    ]
    try:
        response = client.rerank(
            queries=[{"type": "TEXT", "textQuery": {"text": query}}],
            sources=sources,
            rerankingConfiguration={
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "numberOfResults": len(head),
                    "modelConfiguration": {"modelArn": rerank_model_arn(region)},
                },
            },
        )
    except Exception:
        return hits

    order = [item["index"] for item in response.get("results", []) if "index" in item]
    if not order:
        return hits
    reordered = [head[i] for i in order if 0 <= i < len(head)]
    seen = {id(h) for h in reordered}
    for h in head:
        if id(h) not in seen:
            reordered.append(h)
    return reordered + tail
