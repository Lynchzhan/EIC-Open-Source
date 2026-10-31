// 依赖先后排序并保持强连通分量完整，避免按组件名拆包引入新的循环。
export function vendorChunks() {
  let assignment
  return (id, api) => {
    if (!assignment) {
      const modules = [...api.getModuleIds()].filter(key => key.includes('/node_modules/') && !/\.css(?:\?|$)/.test(key)).sort()
      const members = new Set(modules), states = new Map(), stack = [], components = []
      let sequence = 0
      function visit(key) {
        const state = { index:sequence, low:sequence++, stacked:true }
        states.set(key, state); stack.push(key)
        for (const dependency of api.getModuleInfo(key)?.importedIds || []) {
          if (!members.has(dependency)) continue
          if (!states.has(dependency)) { visit(dependency); state.low = Math.min(state.low, states.get(dependency).low) }
          else if (states.get(dependency).stacked) state.low = Math.min(state.low, states.get(dependency).index)
        }
        if (state.low === state.index) {
          const component = []
          let current
          do { current = stack.pop(); states.get(current).stacked = false; component.push(current) } while (current !== key)
          components.push(component)
        }
      }
      modules.forEach(key => { if (!states.has(key)) visit(key) })
      assignment = new Map()
      let batch = 1, bytes = 0
      for (const component of components) {
        const size = component.reduce((total,key) => total + (api.getModuleInfo(key)?.isIncluded ? api.getModuleInfo(key)?.code?.length || 0 : 0), 0)
        if (bytes && bytes + size > 450000) { batch++; bytes = 0 }
        for (const key of component) assignment.set(key, `vendor-${String(batch).padStart(2,'0')}`)
        bytes += size
      }
    }
    return assignment.get(id)
  }
}
