import { describe, expect, it } from 'vitest'
import { decodeRowKey } from '../rowKey'

describe('decodeRowKey（行键 hex 解码展示）', () => {
  it('中文主键解码回原文，多列以 · 连接', () => {
    // hexlify('测试') = e6b58be8af95；hexlify('0031') = 30303331
    expect(decodeRowKey('e6b58be8af95.30303331', true)).toBe('测试 · 0031')
  })

  it('数字主键（ASCII hex）解码回数字文本', () => {
    // hexlify('31') = 3331
    expect(decodeRowKey('3331', true)).toBe('31')
  })

  it('无主键（uuid 行键）原样返回——uuid 解码是乱码不展示', () => {
    const uuidKey = '3f2a1b8c111142228333444455556666'
    expect(decodeRowKey(uuidKey, false)).toBe(uuidKey)
  })

  it('非 hex 片段原样保留（防御：协议只会产生 hex，但展示层不炸）', () => {
    expect(decodeRowKey('zz.e6b58be8af95', true)).toBe('zz · 测试')
    expect(decodeRowKey('abc', true)).toBe('abc') // 奇数长度不拆
  })

  it('空键/空串安全', () => {
    expect(decodeRowKey('', true)).toBe('')
    expect(decodeRowKey('', false)).toBe('')
  })
})
