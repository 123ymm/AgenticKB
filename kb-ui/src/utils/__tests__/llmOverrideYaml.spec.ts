import { describe, expect, it } from 'vitest'
import {
  embeddingSection,
  ownModelKeys,
  parseOverrideYaml,
  patchFlat,
  patchForm,
  readFlat,
  readForm,
} from '../llmOverrideYaml'

describe('llmOverrideYaml', () => {
  it('空/全注释/非法 YAML 解析为 {}，不抛异常', () => {
    expect(parseOverrideYaml('')).toEqual({})
    expect(parseOverrideYaml('# 只有注释\n# 另一行\n')).toEqual({})
    expect(parseOverrideYaml('provider: [未闭合')).toEqual({})
    expect(parseOverrideYaml('- 数组\n- 不是映射\n')).toEqual({})
  })

  it('readForm 读取嵌套字符串字段，缺省为空串', () => {
    const text = 'provider:\n  active_model: odn-chat\nrerank:\n  model: rerank-pro\n'
    expect(readForm(text, 'provider', 'active_model')).toBe('odn-chat')
    expect(readForm(text, 'rerank', 'model')).toBe('rerank-pro')
    expect(readForm(text, 'embedding', 'model')).toBe('')
    expect(readForm(text, 'nope', 'model')).toBe('')
  })

  it('patchForm 写入字段并保留未知键', () => {
    const text = 'provider:\n  active_model: odn-chat\ntask:\n  max: 1\n'
    const out = patchForm(text, 'rerank', 'model', 'rerank-odn')
    expect(readForm(out, 'rerank', 'model')).toBe('rerank-odn')
    // 未知键/段原样保留
    expect(readForm(out, 'provider', 'active_model')).toBe('odn-chat')
    expect(readForm(out, 'task', 'max')).toBe('1')
  })

  it('patchForm 空值删键、段内清空删段', () => {
    let text = patchForm('', 'rerank', 'model', 'rerank-odn')
    expect(text).toContain('rerank')
    text = patchForm(text, 'rerank', 'model', '')
    // 唯一键被删 → 段也被删 → 空文档
    expect(parseOverrideYaml(text)).toEqual({})
    // 段内还有别的键：只删目标键
    let multi = patchForm('', 'embedding', 'model', 'emb-odn')
    multi = patchForm(multi, 'embedding', 'base_url', 'https://x')
    multi = patchForm(multi, 'embedding', 'model', '')
    expect(readForm(multi, 'embedding', 'base_url')).toBe('https://x')
    expect(readForm(multi, 'embedding', 'model')).toBe('')
  })

  it('embeddingSection 判定独立 embedding 配置', () => {
    expect(Object.keys(embeddingSection('provider:\n  active_model: x\n'))).toEqual([])
    const withEmb = patchForm('', 'embedding', 'model', 'emb-odn')
    expect(embeddingSection(withEmb)).toEqual({ model: 'emb-odn' })
  })

  it('ownModelKeys 提取覆盖文件声明的自有 chat 模型键', () => {
    expect(ownModelKeys('rerank:\n  model: x\n')).toEqual([])
    const text = [
      'provider:',
      '  models:',
      '    odn-chat:',
      '      model: glm-4-flash',
      '      base_url: https://x',
      '      api_key: k',
      '',
    ].join('\n')
    expect(ownModelKeys(text)).toEqual(['odn-chat'])
  })

  it('readFlat 读平铺键（onenet），缺省空串', () => {
    const text = 'app_id: odn-app\nchunk_width: 500\n'
    expect(readFlat(text, 'app_id')).toBe('odn-app')
    expect(readFlat(text, 'chunk_width')).toBe('500')
    expect(readFlat(text, 'static_token')).toBe('')
    expect(readFlat('# 全注释模板\n', 'app_id')).toBe('')
  })

  it('patchFlat 平铺写回：空值删键、其余键保留、全清成空映射', () => {
    let text = patchFlat('', 'app_id', 'odn-app')
    text = patchFlat(text, 'static_token', 'tok-odn')
    text = patchFlat(text, 'token_url', 'http://odn.example/token')
    expect(readFlat(text, 'app_id')).toBe('odn-app')
    expect(readFlat(text, 'token_url')).toBe('http://odn.example/token')
    // 清空一键不影响兄弟键
    text = patchFlat(text, 'static_token', '')
    expect(readFlat(text, 'static_token')).toBe('')
    expect(readFlat(text, 'app_id')).toBe('odn-app')
    // YAML 先写的调优键，表单改凭据时保留
    text = 'app_id: a-1\nchunk_width: 500\n'
    text = patchFlat(text, 'app_id', 'a-2')
    expect(readFlat(text, 'chunk_width')).toBe('500')
    expect(readFlat(text, 'app_id')).toBe('a-2')
    // 全清 → 空映射（合法 YAML，parse 回 {}）
    text = patchFlat(text, 'app_id', '')
    text = patchFlat(text, 'chunk_width', '')
    expect(parseOverrideYaml(text)).toEqual({})
  })
})
