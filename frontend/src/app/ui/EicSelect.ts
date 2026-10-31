import { defineComponent, h, Fragment, type VNode, type PropType } from 'vue'
import { NSelect, type SelectOption } from 'naive-ui'

export const EicOption = defineComponent({ name: 'EicOption', setup: () => () => null })
function label(nodes: unknown): string {
  if (typeof nodes === 'string' || typeof nodes === 'number') return String(nodes)
  if (Array.isArray(nodes)) return nodes.map(label).join('')
  if (nodes && typeof nodes === 'object' && 'children' in nodes) return label((nodes as VNode).children)
  return ''
}
function optionsFromSlots(nodes: VNode[]): SelectOption[] {
  return nodes.flatMap(node => {
    if (node.type === Fragment && Array.isArray(node.children)) return optionsFromSlots(node.children as VNode[])
    if (node.type !== EicOption) return []
    const slot = node.children as { default?: () => VNode[] } | null
    return [{ ...node.props, value: node.props?.value, label: label(slot?.default?.() || node.children) } as SelectOption]
  })
}
export default defineComponent({
  name: 'EicSelect', inheritAttrs: false,
  props: {
    value: [String, Number, Array] as PropType<string | number | (string | number)[] | null>,
    options: Array as PropType<SelectOption[]>, mode: String,
    allowClear: Boolean, showSearch: Boolean, loading: Boolean, disabled: Boolean,
    filterOption: [Function, Boolean],
  },
  emits: ['update:value', 'change', 'search', 'select'],
  setup(props, { attrs, slots, emit }) {
    return () => {
      const options = props.options || optionsFromSlots(slots.default?.() || [])
      return h(NSelect, {
        ...attrs, value: props.value ?? null, options, multiple: ['multiple', 'tags'].includes(props.mode || ''),
        tag: props.mode === 'tags', filterable: props.showSearch, clearable: props.allowClear,
        loading: props.loading, disabled: props.disabled, remote: props.filterOption === false,
        ...(typeof props.filterOption === 'function' ? { filter: props.filterOption as (p: string, o: SelectOption) => boolean } : {}),
        onSearch: value => emit('search', value),
        'onUpdate:value': (value, option) => { emit('update:value', value); emit('change', value, option); emit('select', value, option) },
      })
    }
  },
})
