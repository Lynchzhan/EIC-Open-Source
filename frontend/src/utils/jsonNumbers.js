// 兼容旧接口的长整数：只处理 JSON 数字 token，不修改字符串正文。
export function parseJsonSafe(data) {
  if (typeof data !== 'string' || !data) return data
  const parts = []
  let start = 0
  let quoted = false
  let escaped = false
  for (let index = 0; index < data.length; index++) {
    const char = data[index]
    if (quoted) {
      if (escaped) escaped = false
      else if (char === '\\') escaped = true
      else if (char === '"') quoted = false
      continue
    }
    if (char === '"') { quoted = true; continue }
    if (char !== '-' && (char < '0' || char > '9')) continue
    const begin = index
    while (index + 1 < data.length && /[0-9eE+.\-]/.test(data[index + 1])) index++
    const token = data.slice(begin, index + 1)
    // 暂保留旧接口 16 位整数的字符串类型，后续单独迁移 ID 字段协议。
    let before = begin - 1
    let after = index + 1
    while (before >= 0 && /\s/.test(data[before])) before--
    while (after < data.length && /\s/.test(data[after])) after++
    const valueStart = before < 0 || ':,['.includes(data[before])
    const valueEnd = after === data.length || ',]}'.includes(data[after])
    if (valueStart && valueEnd && /^-?[1-9][0-9]{15,}$/.test(token)) {
      parts.push(data.slice(start, begin), '"', token, '"')
      start = index + 1
    }
  }
  parts.push(data.slice(start))
  return JSON.parse(parts.join(''))
}
