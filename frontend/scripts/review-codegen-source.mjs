// 仅解析模板展开后的文本及静态预览脚本，不运行应用或请求接口。
import fs from 'node:fs'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { parse as parseJS } from '@babel/parser'
import { parse, compileScript, compileTemplate, compileStyle } from '@vue/compiler-sfc'
const result = spawnSync('python', ['-X', 'utf8', '../backend/scripts/review_codegen_sources.py'], { encoding: 'utf8' })
if (result.status !== 0) throw new Error(result.stderr || result.error?.message || 'Python 源码解析失败')
const { files } = JSON.parse(result.stdout)
const problems = []
for (const filename of ['src/pages/ai/MyAIChat.vue', 'src/pages/CodeGenerator.vue']) {
  const { descriptor, errors } = parse(fs.readFileSync(filename, 'utf8'), { filename })
  try {
    if (errors.length) throw errors[0]
    const script = compileScript(descriptor, { id: filename })
    if (descriptor.template) {
      const compiled = compileTemplate({ source: descriptor.template.content, filename, id: filename,
        compilerOptions: { bindingMetadata: script.bindings } })
      if (compiled.errors.length) throw compiled.errors[0]
    }
    for (const style of descriptor.styles) {
      const source = style.src ? fs.readFileSync(path.resolve(path.dirname(filename), style.src), 'utf8') : style.content
      const result = compileStyle({ source, filename, id: 'data-v-source-review', scoped: style.scoped })
      if (result.errors.length) throw result.errors[0]
    }
  } catch (error) { problems.push({ file: filename, error: String(error.message || error) }) }
}
for (const file of files) {
  try {
    if (file.name.endsWith('.js.tpl')) parseJS(file.source, { sourceType: 'module' })
    if (file.name.endsWith('.vue.tpl')) {
      const { descriptor, errors } = parse(file.source, { filename: file.name })
      if (errors.length) throw errors[0]
      compileScript(descriptor, { id: file.name })
      const compiled = compileTemplate({ source: descriptor.template.content, filename: file.name, id: file.name })
      if (compiled.errors.length) throw compiled.errors[0]
    }
  } catch (error) { problems.push({ file: file.name, error: String(error.message || error) }) }
}
const previewPath = process.argv[2] ? path.resolve(process.argv[2]) : null
if (previewPath) {
  const preview = fs.readFileSync(previewPath, 'utf8')
  for (const match of preview.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) {
    try { parseJS(match[1], { sourceType: 'script' }) }
    catch (error) { problems.push({ file: previewPath, error: error.message }) }
  }
}
console.log(JSON.stringify({ mode: 'static-codegen-source-review', preview: previewPath || 'not-requested', templateVariants: files.length, problems }, null, 2))
process.exitCode = problems.length ? 1 : 0
