import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { ACCENT, DOWN, FLAT, UP, alpha, trendColor } from '../../constants/colors.js'

test('trendColor：正红负绿，0 与空值为平盘灰（FEX-01）', () => {
  assert.equal(trendColor(1.2), UP)
  assert.equal(trendColor('-0.5'), DOWN)
  assert.equal(trendColor(0), FLAT)
  assert.equal(trendColor(null), FLAT)
  assert.equal(trendColor(''), FLAT)
})

test('alpha：十六进制转 rgba', () => {
  assert.equal(alpha('#ff6b81', 0.5), 'rgba(255,107,129,0.5)')
})

test('图表色与 style.css 变量一致，强调色不等于上涨红', () => {
  const css = readFileSync(new URL('../../style.css', import.meta.url), 'utf8')
  const v = name => css.match(new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{6})`))[1].toLowerCase()
  assert.equal(v('up'), UP)
  assert.equal(v('down'), DOWN)
  assert.equal(v('flat'), FLAT)
  assert.equal(v('accent'), ACCENT)
  assert.notEqual(v('accent'), v('up'))
})
