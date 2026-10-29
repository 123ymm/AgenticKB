import { describe, expect, it } from 'vitest'
import {
  embeddingSection,
  ownModelKeys,
  parseOverrideYaml,
  patchForm,
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
})
