// 只检查已有构建产物，不启动浏览器或业务接口。
import fs from 'node:fs'
import path from 'node:path'
import crypto from 'node:crypto'
import { parseAst } from 'rollup/parseAst'

const root = path.resolve('dist')
const assets = path.join(root, 'assets')
const files = fs.readdirSync(assets).filter(name => name.endsWith('.js')).sort()
const graph = new Map()
const checks = []
for (const name of files) {
  const file = path.join(assets, name)
  const code = fs.readFileSync(file, 'utf8')
  const dependencies = []
  const ast = parseAst(code)
  for (const node of ast.body) {
    if (node.type === 'ImportDeclaration' || node.type === 'ExportAllDeclaration' || node.type === 'ExportNamedDeclaration') {
      const reference = node.source?.value
      if (reference?.startsWith('.')) {
        const target = path.resolve(assets, reference)
        if (!fs.existsSync(target)) throw new Error(`Missing import: ${name}: ${reference}`)
        if (target.endsWith('.js')) dependencies.push(path.basename(target))
      }
    }
  }
  graph.set(name, dependencies)
  const bytes = fs.statSync(file).size
  if (bytes > 500000) throw new Error(`Chunk exceeds default limit: ${name}: ${bytes}`)
  checks.push({ name, bytes, sha256:crypto.createHash('sha256').update(code).digest('hex') })
}
const visited = new Set(), pending = new Set()
function visit(name) {
  if (pending.has(name)) throw new Error(`Static chunk cycle: ${name}`)
  if (visited.has(name)) return
  pending.add(name)
  for (const dependency of graph.get(name) || []) visit(dependency)
  pending.delete(name); visited.add(name)
}
files.forEach(visit)
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8')
for (const match of html.matchAll(/(?:src|href)="(\/assets\/[^\"]+)"/g)) {
  if (!fs.existsSync(path.join(root, match[1]))) throw new Error(`Missing HTML asset: ${match[1]}`)
}
checks.sort((a,b) => b.bytes - a.bytes)
console.log(JSON.stringify({ mode:'static-build-artifact-review', passed:true, staticChunkCycles:0, chunks:checks.length, largest:checks[0], entry:checks.find(item => item.name.startsWith('index-')) }, null, 2))
