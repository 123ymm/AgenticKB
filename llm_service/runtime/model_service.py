from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from llm_service.models import (
    EmbeddingData,
    EmbeddingRequest,
    EmbeddingResponse,
    RerankRequest,
    RerankResponse,
    RerankResult,
)
from llm_service.providers.model_base import ModelProviderProtocol
from llm_service.providers.utils import extract_doc_text
from llm_service.runtime.persist_writer import PersistWriter, SyncModelRecord

if TYPE_CHECKING:
    from llm_service.db import LlmRuntimeDB
    from llm_service.runtime.provider_resolver import ProviderResolver

logger = logging.getLogger(__name__)


class ModelService:
    """Synchronous-style model facade exposed over HTTP for mining/serving."""

    def __init__(self, provider: ModelProviderProtocol, db: LlmRuntimeDB | None = None, *,
                 persist_writer: PersistWriter | None = None,
                 default_embedding_model: str = "",
                 default_rerank_model: str = "",
                 default_embedding_dimensions: int | None = None,
                 provider_resolver: "ProviderResolver | None" = None):
        self._provider = provider
        self._db = db
        self._persist_writer = persist_writer
        self._default_embedding_model = default_embedding_model
        self._default_rerank_model = default_rerank_model
        self._default_embedding_dimensions = default_embedding_dimensions
        # 62号二期：rerank 与 embedding 都按域解析（维度锁全局由装载闸保证）。
        # 不传 resolver 时保持旧版单 provider 行为。
        self._resolver = provider_resolver

    def _embedding_provider(self, knowledge_domain: str | None) -> ModelProviderProtocol:
        """该域的 embedding provider（无覆盖域 = 全局实例）。"""
        if self._resolver is None:
            return self._provider
        return self._resolver.model_provider(knowledge_domain)

    def _embedding_model_for(self, knowledge_domain: str | None, explicit: str | None) -> str:
        """embedding 默认模型：显式 > 域 effective（含全局兜底）> 构造期默认。"""
        if explicit:
            return explicit
        if self._resolver is not None:
            resolved = self._resolver.embedding_default_model(knowledge_domain)
            if resolved:
                return resolved
        return self._default_embedding_model

    def _embedding_dimensions_for(self, knowledge_domain: str | None, explicit: int | None) -> int | None:
        """embedding 维度：显式 > 域 effective（锁全局，同全局值）> 构造期默认。"""
        if explicit is not None:
            return explicit
        if self._resolver is not None:
            resolved = self._resolver.embedding_default_dimensions(knowledge_domain)
            if resolved is not None:
                return resolved
        return self._default_embedding_dimensions

    def _rerank_provider(self, knowledge_domain: str | None) -> ModelProviderProtocol:
        """该域的 rerank provider（无覆盖域 = 全局实例）。"""
        if self._resolver is None:
            return self._provider
        return self._resolver.model_provider(knowledge_domain)

    def _rerank_model_for(self, knowledge_domain: str | None, explicit: str | None) -> str:
        """rerank 默认模型：显式 > 域 effective（含全局兜底）> 构造期默认。"""
        if explicit:
            return explicit
        if self._resolver is not None:
            resolved = self._resolver.rerank_default_model(knowledge_domain)
            if resolved:
                return resolved
        return self._default_rerank_model

    def _enqueue_record(self, record: SyncModelRecord) -> None:
        """Push record to PersistWriter queue (non-blocking, fire-and-forget)."""
        if self._persist_writer is not None:
            self._persist_writer.enqueue(record)

    async def embed(self, body: EmbeddingRequest) -> EmbeddingResponse:
        t0 = datetime.now(timezone.utc)
        model = self._embedding_model_for(body.knowledge_domain, body.model)
        dimensions = self._embedding_dimensions_for(body.knowledge_domain, body.dimensions)
        texts = body.input
        input_json = {"texts": texts, "model": model, "dimensions": dimensions}
        try:
            raw = await self._embedding_provider(body.knowledge_domain).embed(
                texts,
                model=model,
                dimensions=dimensions,
            )
            latency = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
            usage = raw.get("usage") or {}
            token_usage = usage.get("total_tokens") or usage.get("prompt_tokens")

            text_output = "\n".join(texts)
            self._enqueue_record(SyncModelRecord(
                task_type="embedding", model=model,
                caller_service=body.caller_service or "model",
                knowledge_domain=body.knowledge_domain,
                pipeline_stage=body.pipeline_stage,
                status="succeeded",
                latency_ms=latency, total_tokens=token_usage,
                input_json=json.dumps(input_json, ensure_ascii=False),
                raw_response_json=json.dumps(raw or {}, ensure_ascii=False),
                text_output=text_output,
                now=datetime.now(timezone.utc).isoformat(),
            ))

            data = sorted(raw.get("data", []), key=lambda item: item.get("index") or 0)
            return EmbeddingResponse(
                model=raw.get("model") or model or "",
                data=[
                    EmbeddingData(
                        index=item.get("index") if item.get("index") is not None else idx,
                        embedding=item.get("embedding", []),
                    )
                    for idx, item in enumerate(data)
                ],
                usage=usage,
            )
        except Exception as e:
            latency = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
            error_type = getattr(e, "error_type", "unexpected_error")
            self._enqueue_record(SyncModelRecord(
                task_type="embedding", model=model,
                caller_service=body.caller_service or "model",
                knowledge_domain=body.knowledge_domain,
                pipeline_stage=body.pipeline_stage,
                status="failed",
                latency_ms=latency, total_tokens=None,
                input_json=json.dumps(input_json, ensure_ascii=False),
                error_type=error_type, error_message=str(e)[:500],
                now=datetime.now(timezone.utc).isoformat(),
            ))
            raise

    async def rerank(self, body: RerankRequest) -> RerankResponse:
        t0 = datetime.now(timezone.utc)
        model = self._rerank_model_for(body.knowledge_domain, body.model)
        input_json = {
            "query": body.query,
            "documents": body.documents,
            "model": model,
            "top_n": body.top_n,
        }
        try:
            raw = await self._rerank_provider(body.knowledge_domain).rerank(
                body.query,
                body.documents,
                model=model,
                top_n=body.top_n,
            )
            latency = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
            usage = raw.get("usage") or {}
            token_usage = usage.get("total_tokens")

            lines = [f"Query: {body.query}"]
            for r in raw.get("results", []):
                score = r.get("relevance_score", 0)
                doc = extract_doc_text(r.get("document"))[:100]
                lines.append(f"  [{r.get('index', '?')}] score={score:.4f}: {doc}")
            text_output = "\n".join(lines)

            self._enqueue_record(SyncModelRecord(
                task_type="rerank", model=model,
                caller_service=body.caller_service or "model",
                knowledge_domain=body.knowledge_domain,
                pipeline_stage=body.pipeline_stage,
                status="succeeded",
                latency_ms=latency, total_tokens=token_usage,
                input_json=json.dumps(input_json, ensure_ascii=False),
                raw_response_json=json.dumps(raw or {}, ensure_ascii=False),
                text_output=text_output,
                now=datetime.now(timezone.utc).isoformat(),
            ))

            return RerankResponse(
                model=raw.get("model") or model or "",
                results=[
                    RerankResult(
                        index=item.get("index") if item.get("index") is not None else idx,
                        relevance_score=float(item.get("relevance_score", 0.0)),
                        document=extract_doc_text(item.get("document")),
                    )
                    for idx, item in enumerate(raw.get("results", []))
                ],
            )
        except Exception as e:
            latency = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
            error_type = getattr(e, "error_type", "unexpected_error")
            self._enqueue_record(SyncModelRecord(
                task_type="rerank", model=model,
                caller_service=body.caller_service or "model",
                knowledge_domain=body.knowledge_domain,
                pipeline_stage=body.pipeline_stage,
                status="failed",
                latency_ms=latency, total_tokens=None,
                input_json=json.dumps(input_json, ensure_ascii=False),
                error_type=error_type, error_message=str(e)[:500],
                now=datetime.now(timezone.utc).isoformat(),
            ))
            raise
