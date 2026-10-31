import { defineComponent, h, type PropType } from 'vue'

export const EicRow = defineComponent({
  name: 'EicRow', props: { gutter: { type: [Number, Array] as PropType<number | number[]>, default: 16 } },
  setup(props, { slots }) {
    return () => h('div', { class: 'eic-grid-row', style: {
      '--eic-grid-column-gap': `${(Array.isArray(props.gutter) ? props.gutter[0] : props.gutter) || 0}px`,
      rowGap: `${(Array.isArray(props.gutter) ? props.gutter[1] : 0) || 0}px`,
    } }, slots.default?.())
  },
})
export const EicCol = defineComponent({
  name: 'EicCol', props: { span: Number, xs: [Number, Object], sm: [Number, Object], md: [Number, Object], lg: [Number, Object], xl: [Number, Object], xxl: [Number, Object] },
  setup(props, { slots }) {
    return () => {
      const style: Record<string, number> = { '--eic-col-span': props.span || 24 }
      for (const key of ['xs', 'sm', 'md', 'lg', 'xl', 'xxl'] as const) {
        const value = props[key]
        if (value != null) style[`--eic-col-${key}`] = typeof value === 'number' ? value : (value as any).span
      }
      return h('div', { class: 'eic-grid-col', style }, slots.default?.())
    }
  },
})
