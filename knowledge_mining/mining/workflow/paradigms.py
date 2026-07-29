from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkflowParadigm:
    name: str
    description: str
    template_key: str


ORDINARY_WORKFLOW_PARADIGMS = (
    WorkflowParadigm(
        name="基础文档入库",
        description="仅解析、切分并持久化文档资产，适合低成本基础入库。",
        template_key="minimal",
    ),
    WorkflowParadigm(
        name="快速向量检索",
        description="直接生成检索单元与向量，适合快速构建基础 RAG 检索。",
        template_key="fast_retrieval",
    ),
    WorkflowParadigm(
        name="篇章增强检索",
        description="理解篇章关系并补充检索上下文，适合长文档的高质量语义检索。",
        template_key="discourse_only",
    ),
    WorkflowParadigm(
        name="固定本体图谱构建",
        description="按当前 Domain 的已有本体抽取实体和关系并写入图谱，不演化本体。",
        template_key="entity_graph",
    ),
    WorkflowParadigm(
        name="检索与图谱联合构建",
        description="同时构建篇章增强检索资产和固定本体实体图谱。",
        template_key="hybrid_knowledge",
    ),
    WorkflowParadigm(
        name="本体演化专项",
        description="抽取实体关系并归纳、审核本体候选，适合本体持续演化。",
        template_key="ontology_only",
    ),
)


__all__ = ["ORDINARY_WORKFLOW_PARADIGMS", "WorkflowParadigm"]
