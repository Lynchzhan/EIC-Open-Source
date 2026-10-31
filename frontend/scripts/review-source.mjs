// 仅检查源码语法与迁移残留，不构建、不挂载应用、不请求接口。
import fs from 'node:fs';
import path from 'node:path';
import { parse } from '@vue/compiler-sfc';
import { parse as parseScript } from '@babel/parser';

const root = path.resolve('src');
const problems = [];
const remaining = [];
let files = 0;
function visit(dir) {
  for (const item of fs.readdirSync(dir, { withFileTypes: true })) {
    const file = path.join(dir, item.name);
    if (item.isDirectory()) { visit(file); continue; }
    if (!/\.(vue|[jt]s)$/.test(file)) continue;
    files++;
    const source = fs.readFileSync(file, 'utf8');
    const name = path.relative(root, file);
    if (/ant-design-vue|@ant-design\/icons-vue|element-plus|<(?:a|el)-[a-z]/.test(source)) remaining.push(name);
    try {
      if (file.endsWith('.vue')) {
        const result = parse(source, { filename: file });
        for (const error of result.errors) problems.push({ file: name, error: String(error.message || error) });
        for (const block of [result.descriptor.script, result.descriptor.scriptSetup].filter(Boolean)) {
          parseScript(block.content, { sourceType: 'module', plugins: block.lang === 'ts' ? ['typescript'] : [] });
        }
      } else parseScript(source, { sourceType: 'module', plugins: file.endsWith('.ts') ? ['typescript'] : [] });
    } catch (error) { problems.push({ file: name, error: error.message }); }
  }
}
visit(root);
console.log(JSON.stringify({ mode: 'source-syntax-review-only', files, problems, remainingAntFiles: remaining }, null, 2));
process.exitCode = problems.length || remaining.length ? 1 : 0;
