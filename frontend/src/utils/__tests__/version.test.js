// 版本号一致性：package.json 的 version 必须与仓库根目录 VERSION 相同，发版时两处一起改
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const root = new URL('../../../../', import.meta.url)

test('package.json 版本与 VERSION 文件一致', () => {
  const version = readFileSync(new URL('VERSION', root), 'utf-8').trim()
  const pkg = JSON.parse(readFileSync(new URL('frontend/package.json', root), 'utf-8'))
  assert.match(version, /^\d+\.\d+\.\d+$/)
  assert.equal(pkg.version, version)
})
