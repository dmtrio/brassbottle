import { writeFileSync } from 'node:fs'
import path from 'node:path'
import { compileFromFile } from 'json-schema-to-typescript'

const contractDir = path.resolve(import.meta.dirname, '../../contract')
const outPath = path.resolve(import.meta.dirname, '../src/contract.ts')

const schemas = [
  'queue_snapshot',
  'decide_response',
  'recent_page',
  'sse_event',
  'error_response',
]

const parts: string[] = []
for (const name of schemas) {
  const defs = await compileFromFile(path.join(contractDir, `${name}.schema.json`), {
    cwd: contractDir,
    bannerComment: '',
  })
  parts.push(`// ${name}.schema.json\n${defs}`)
}

writeFileSync(outPath, parts.join('\n\n') + '\n')
console.log(`generated ${outPath}`)
