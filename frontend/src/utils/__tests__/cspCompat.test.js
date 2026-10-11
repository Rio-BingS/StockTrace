// P3 第 14 条：nginx CSP 只放行同源脚本、同源连接（deploy/nginx-stocktrace.conf）。
// 这里锁住前端源码不引入会被该策略拦下的写法，改了策略或确需放开时同步两边。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

const ROOT = new URL('../../../', import.meta.url).pathname
const SRC = join(ROOT, 'src')

function sourceFiles(dir) {
  const out = []
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) {
      if (name !== '__tests__') out.push(...sourceFiles(p))
    } else if (/\.(js|vue)$/.test(name)) {
      out.push(p)
    }
  }
  return out
}

test('index.html 没有内联脚本与内联事件（script-src self）', () => {
  const html = readFileSync(join(ROOT, 'index.html'), 'utf-8')
  const scripts = html.match(/<script\b[^>]*>/g) || []
  assert.ok(scripts.length > 0)
  for (const tag of scripts) assert.match(tag, /\ssrc=/, `内联脚本会被 CSP 拦截：${tag}`)
  assert.doesNotMatch(html, /\son[a-z]+=/i, '内联事件属性会被 CSP 拦截')
  assert.doesNotMatch(html, /<style\b/i, '入口页不放内联样式块')
})

test('源码不用 eval/new Function/外部连接（无 unsafe-eval，connect-src self）', () => {
  const files = sourceFiles(SRC)
  assert.ok(files.length > 10)
  for (const f of files) {
    const code = readFileSync(f, 'utf-8')
    assert.doesNotMatch(code, /\beval\s*\(|new\s+Function\s*\(/, `${f} 需要 unsafe-eval`)
    assert.doesNotMatch(code, /new\s+(WebSocket|EventSource)\s*\(/, `${f} 新连接需改 connect-src`)
    // 请求只走同源 /api（api/stocks.js baseURL）；外部地址只允许出现在注释/占位文案里
    assert.doesNotMatch(
      code,
      /(baseURL|fetch\(|axios\.(get|post)\()\s*:?\s*['"`]https?:\/\//,
      `${f} 直连外部域名会被 connect-src 拦截`,
    )
  }
})
