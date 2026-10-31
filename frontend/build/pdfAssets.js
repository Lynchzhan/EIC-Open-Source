import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'

// PDF 资源随应用发布，预览期间不请求外部 CDN。
export function pdfAssets() {
  const root = path.dirname(createRequire(import.meta.url).resolve('pdfjs-dist/package.json'))
  const assets = new Map()
  for (const folder of ['cmaps', 'standard_fonts', 'wasm']) {
    for (const name of fs.readdirSync(path.join(root, folder))) {
      const source = path.join(root, folder, name)
      if (fs.statSync(source).isFile()) assets.set(`assets/pdf/${folder}/${name}`, source)
    }
  }
  return {
    name: 'local-pdf-assets',
    generateBundle() {
      this.emitFile({ type: 'asset', fileName: 'assets/pdf/LICENSE', source: fs.readFileSync(path.join(root, 'LICENSE')) })
      for (const [fileName, source] of assets) this.emitFile({ type: 'asset', fileName, source: fs.readFileSync(source) })
    },
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const source = assets.get((req.url || '').split('?')[0].replace(/^\//, ''))
        if (!source) return next()
        res.setHeader('Content-Type', source.endsWith('.wasm') ? 'application/wasm' : 'application/octet-stream')
        fs.createReadStream(source).pipe(res)
      })
    },
  }
}
