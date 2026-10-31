import fs from 'node:fs';
import path from 'node:path';
import { parse, compileTemplate } from '@vue/compiler-sfc';
import { parse as js } from '@babel/parser';
const root=path.resolve('src'), files=[];
function walk(dir){for(const e of fs.readdirSync(dir,{withFileTypes:true})){const f=path.join(dir,e.name);if(e.isDirectory())walk(f);else if(/\.(vue|[jt]s|css)$/.test(f))files.push(f)}}
walk(root);
const errors=[], tags={},imports=[];
for(const f of files){const source=fs.readFileSync(f,'utf8'), name=path.relative(root,f);
  if(f.endsWith('.vue')){
    const {descriptor:d}=parse(source); if(d.template){
      const result=compileTemplate({source:d.template.content,filename:f,id:'source-audit',compilerOptions:{expressionPlugins:['typescript']}});
      for(const e of result.errors) errors.push({file:name,error:String(e.message||e)});
      for(const m of d.template.content.matchAll(/<(e-[\w-]+)\b([^>]*?)(?:\s*\/?[>])/g)){(tags[m[1]]??=[]).push({file:name,attributes:m[2].trim()})}
    }
  }
  for(const m of source.matchAll(/(?:from\s*|import\s*|@import\s*)['"]([^'"]+)['"]/g)){
    const p=m[1]; if(!p.startsWith('.')&&!p.startsWith('@/'))continue;
    const target=p.startsWith('@/')?path.join(root,p.slice(2)):path.resolve(path.dirname(f),p);
    if(!['','.js','.ts','.vue','.css','/index.js','/index.ts'].some(ext=>fs.existsSync(target+ext)))imports.push({file:name,path:p});
  }
}
const mode=process.argv[2];
console.log(JSON.stringify(mode==='tags'?tags:mode==='components'?Object.keys(tags):{mode:'static-template-and-import-review',errors,missingImports:imports},null,2));
process.exitCode = errors.length || imports.length ? 1 : 0;
