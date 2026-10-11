// 数字格式化统一实现的表驱动测试（FEF-11 / OPS-08）
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  toNum, fmtFixed, fmtSigned, fmtPct, fmtRate, fmtAmount, fmtVolume, fmtShare, signClass,
} from '../number.js'

function table(fn, cases) {
  for (const [input, expected] of cases) {
    const args = Array.isArray(input) ? input : [input]
    assert.equal(fn(...args), expected, `${fn.name}(${args.map(a => JSON.stringify(a)).join(', ')})`)
  }
}

test('toNum：空串、空白、NaN、Infinity 视为无值', () => {
  table(toNum, [
    [null, null], [undefined, null], ['', null], ['  ', null], ['abc', null],
    [Infinity, null], [-Infinity, null], [NaN, null],
    ['1.5', 1.5], [0, 0], ['-0', -0], [' 2 ', 2],
  ])
})

test('fmtFixed：空串显示 "-"，-0 不带负号', () => {
  table(fmtFixed, [
    ['', '-'], [null, '-'], [Infinity, '-'],
    [0, '0.00'], [-0.004, '0.00'], [-0, '0.00'],
    [12.345, '12.35'], ['-3.1', '-3.10'], [[1.23456, 3], '1.235'],
  ])
})

test('fmtSigned / fmtPct：按舍入后的值决定正负号，0 不带 "+"', () => {
  table(fmtSigned, [
    [0.004, '0.00'], [-0.004, '0.00'], [0, '0.00'], [0.005, '+0.01'],
    [0.4, '+0.40'], ['-0.25', '-0.25'], ['', '-'],
  ])
  table(fmtPct, [
    [0.004, '0.00%'], [-0.004, '0.00%'], [1.234, '+1.23%'], [-1.235, '-1.24%'],
    [Infinity, '-'], ['', '-'], [null, '-'],
  ])
})

test('fmtRate：振幅、换手率不加正号', () => {
  table(fmtRate, [[5.2, '5.20%'], [0, '0.00%'], [null, '-'], ['', '-']])
})

test('fmtAmount：元 → 万/亿/万亿，边界进位', () => {
  table(fmtAmount, [
    [99_999_999.9, '1.00 亿'],
    [99_999_949, '9999.99 万'],
    [9_999.6, '1.00 万'],
    [9_999.4, '9999'],
    [999_999_995_000, '1.00 万亿'],
    [999_999_900_000, '1.00 万亿'],
    [-0.4, '0'],
    [[52_345_678_900, { signed: true }], '+523.46 亿'],
    [[-52_345_678_900, { signed: true }], '-523.46 亿'],
    [[0, { signed: true }], '0'],
    ['', '-'],
  ])
})

test('fmtAmount：输入单位为亿时先换算再选档（两融、北向）', () => {
  table(fmtAmount, [
    [[26508.92, { unit: '亿' }], '2.65 万亿'],
    [[173, { unit: '亿' }], '173.00 亿'],
    [[-4.08, { unit: '亿', signed: true }], '-4.08 亿'],
    [[0.5, { unit: '亿', signed: true }], '+5000.00 万'],
    [[12, { unit: '万' }], '12.00 万'],
  ])
  assert.throws(() => fmtAmount(1, { unit: '美元' }))
})

test('fmtVolume：股/手统一显示为股并带单位', () => {
  table(fmtVolume, [
    [1_234_567, '123.46 万股'],
    [320_000_000, '3.20 亿股'],
    [9_999, '9999 股'],
    [[12_345, { unit: '手' }], '123.45 万股'],
    [null, '-'],
  ])
})

test('fmtShare：份额与份额变化', () => {
  table(fmtShare, [
    [123_456_789, '1.23 亿份'],
    [99_999_999.9, '1.00 亿份'],
    [5000, '5000 份'],
    [[-50_000_000, { signed: true }], '-5000.00 万份'],
    [[120_000_000, { signed: true }], '+1.20 亿份'],
  ])
})

test('signClass：与显示文本一致，舍入为 0 时不着色', () => {
  table(signClass, [[0.004, ''], [-0.004, ''], [0.01, 'up'], [-2, 'down'], ['', ''], [null, '']])
})
