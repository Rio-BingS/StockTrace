// 全站文字颜色对比度回归（FEX-02）：扫描各组件 <style> 中的 color 声明，
// 在主卡片底色上的对比度须 ≥ 4.5（WCAG AA 正文）。纯装饰分隔符列入白名单。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

const SRC = new URL('../../', import.meta.url).pathname
// 深色卡片常用底色
const BACKGROUNDS = ['#16213e', '#111d34', '#1a2744']
// 装饰性分隔线文字（"|"），不承载信息
const DECORATIVE = new Set(['#2a3a5e'])

function hex(h) {
  let s = h.slice(1).toLowerCase()
  if (s.length === 3) s = [...s].map(c => c + c).join('')
  return '#' + s
}

function luminance(h) {
  const [r, g, b] = [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16) / 255)
    .map(c => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4))
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

export function contrast(a, b) {
  const [x, y] = [luminance(hex(a)), luminance(hex(b))].sort((m, n) => n - m)
  return (x + 0.05) / (y + 0.05)
}

function vueFiles(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap(d => {
    const p = join(dir, d.name)
    if (d.isDirectory()) return d.name === '__tests__' ? [] : vueFiles(p)
    return p.endsWith('.vue') ? [p] : []
  })
}

test('contrast：WCAG 公式自检', () => {
  assert.ok(Math.abs(contrast('#666666', '#16213e') - 2.77) < 0.01)
  assert.ok(contrast('#9aa3b2', '#16213e') >= 4.5)
})

test('组件样式中的文字颜色在深色卡片上对比度 ≥ 4.5', () => {
  const failures = []
  for (const file of vueFiles(SRC)) {
    const src = readFileSync(file, 'utf8')
    for (const [, css] of src.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)) {
      for (const [, color] of css.matchAll(/(?:^|[;{\s])color:\s*(#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3})\b/g)) {
        const c = hex(color)
        if (DECORATIVE.has(c)) continue
        const worst = Math.min(...BACKGROUNDS.map(bg => contrast(c, bg)))
        if (worst < 4.5) failures.push(`${file.replace(SRC, '')}: ${color} (${worst.toFixed(2)})`)
      }
    }
  }
  assert.deepEqual(failures, [])
})
