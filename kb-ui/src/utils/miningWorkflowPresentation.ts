import type { MiningExecutionZone, MiningOperatorDef } from '@/types/miningWorkflow'

export interface MiningOperatorFamily {
  key: string
  label: string
  types: readonly string[]
}

export const MINING_OPERATOR_FAMILIES: readonly MiningOperatorFamily[] = [
  { key: 'input_parse', label: '输入与解析', types: ['input_ingest', 'parse_segment'] },
  {
    key: 'discourse_retrieval',
    label: '篇章与检索',
    types: ['enrich', 'discourse_line', 'contextual_retrieval_enrich', 'retrieval_unit_build', 'embedding'],
  },
  {
    key: 'entity_graph',
    label: '实体与图谱',
    types: ['entity_extract', 'entity_resolve', 'entity_relation_extract', 'entity_review_gate', 'graph_write'],
  },
  { key: 'ontology_evolution', label: '本体演化', types: ['ontology_induction', 'ontology_review_gate'] },
  { key: 'asset_publish', label: '资产与发布', types: ['asset_persist', 'mining_finalize'] },
]

export const MINING_ZONE_LABELS: Record<MiningExecutionZone, string> = {
  input: '输入阶段',
  document: '逐文档',
  global: '整批次',
}

export interface MiningOperatorPresentationGroup {
  key: string
  label: string
  items: MiningOperatorDef[]
}

export function groupMiningOperators(operators: MiningOperatorDef[]): MiningOperatorPresentationGroup[] {
  const byType = new Map(operators.map(operator => [operator.type, operator]))
  const knownTypes = new Set(MINING_OPERATOR_FAMILIES.flatMap(family => [...family.types]))
  const groups = MINING_OPERATOR_FAMILIES.map(family => ({
    key: family.key,
    label: family.label,
    items: family.types.flatMap(type => {
      const operator = byType.get(type)
      return operator ? [operator] : []
    }),
  })).filter(group => group.items.length)
  const unknown = operators.filter(operator => !knownTypes.has(operator.type))
  if (unknown.length) groups.push({ key: 'other', label: '其他', items: unknown })
  return groups
}
