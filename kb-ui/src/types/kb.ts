/**
 * 知识库管理（KB）前端类型。
 *
 * 与后端 knowledge_mining/mining/kb/routes 的响应形状对齐：
 * - KbSummary 来自 list_visible（含派生 my_role + document_count）
 * - KbDetail 来自 get_kb
 * - KbMember 来自 list_members
 * - KbDocument 来自 documents 各端点（字段按端点部分返回，故多数可选）
 */

export type KbVisibility = 'private' | 'shared' | 'public'
export type KbStatus = 'active' | 'deleted'
export type KbMemberRole = 'viewer' | 'editor'
/** 当前用户在该 KB 的有效访问级别（列表页展示用）。 */
export type KbMyRole = 'owner' | 'editor' | 'viewer'
/** 文档派生状态（后端 derive_document_status 实时计算，不存列）。 */
export type KbDocStatus = 'uploaded' | 'mining' | 'published' | 'withdrawn' | 'failed' | 'unknown'

export interface KbSummary {
  id: string
  domain: string
  name: string
  description: string | null
  owner_id: string
  visibility: KbVisibility
  created_at: string
  my_role: KbMyRole
  document_count: number
}

export interface KbDetail {
  id: string
  domain: string
  name: string
  description: string | null
  owner_id: string
  visibility: KbVisibility
  status: KbStatus
  deleted_at: string | null
  created_at: string
  updated_at: string
}

export interface KbMember {
  kb_id: string
  user_id: string
  role: KbMemberRole
  added_at: string
  username: string
  display_name: string | null
}

export interface KbFolder {
  id: string
  kb_id: string
  parent_id: string | null
  name: string
  path: string
  created_at: string
  created_by: string | null
}

export interface KbDocument {
  id: string
  domain?: string
  kb_id?: string
  document_key?: string
  document_name: string
  document_type?: string | null
  storage_path?: string
  directory_path?: string | null
  owner_id?: string | null
  created_at?: string
  status?: KbDocStatus
}

export interface KbCreateBody {
  domain: string
  name: string
  visibility: KbVisibility
  description?: string | null
}

export interface KbUpdateBody {
  name?: string
  description?: string | null
  visibility?: KbVisibility
}

export interface KbMineResult {
  run_id: string
  kb_id: string
  status: string
  started_at: string
}
