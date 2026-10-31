import { defineComponent, h, Comment, Fragment, unref, type PropType, type VNode } from 'vue'
import { NDataTable } from 'naive-ui'

function meaningful(nodes: VNode[]): boolean {
  return nodes.some(node => node.type !== Comment && (node.type === Fragment
    ? meaningful((node.children || []) as VNode[]) : typeof node.children !== 'string' || node.children.trim() !== ''))
}
function get(row: any, key: string | string[]) {
  return (Array.isArray(key) ? key : String(key || '').split('.')).reduce((value, part) => value?.[part], row)
}
export default defineComponent({
  name: 'EicTable', inheritAttrs: false,
  props: {
    columns: { type: Array as PropType<any[]>, default: () => [] },
    dataSource: { type: Array as PropType<any[]>, default: () => [] },
    pagination: { type: [Boolean, Object] as PropType<any>, default: true },
    rowSelection: Object as PropType<any>, rowKey: [String, Function] as PropType<any>,
    loading: Boolean, scroll: Object as PropType<any>, size: String, bordered: Boolean,
  }, emits: ['change', 'expand', 'update:expandedRowKeys'],
  setup(props, { attrs, slots, emit }) {
    let sorter: any = {}; let filters: any = {}
    const keyFor = (row: any) => typeof props.rowKey === 'function' ? props.rowKey(row)
      : row[props.rowKey || 'key'] ?? row.id ?? props.dataSource.indexOf(row)
    const pageChange = (page: number, pageSize?: number) => emit('change', {
      ...(typeof props.pagination === 'object' ? props.pagination : {}), current: page,
      pageSize: pageSize || props.pagination?.pageSize || 10,
    }, filters, sorter)
    return () => {
      const cols = props.columns.map(column => ({
        ...column, key: column.key ?? column.dataIndex,
        sorter: column.sorter === true ? 'default' : column.sorter,
        render: (row: any, index: number) => {
          const value = get(row, column.dataIndex)
          const context = { text: value, value, record: row, index, column }
          const custom = slots.bodyCell?.(context)
          if (custom && meaningful(custom)) return custom
          if (column.customRender) return column.customRender(context)
          return value == null ? '' : String(value)
        },
        ...(slots.headerCell ? { title: () => slots.headerCell?.({ column }) } : {}),
      }))
      if (props.rowSelection) cols.unshift({
        type: 'selection', multiple: props.rowSelection.type !== 'radio',
        disabled: (row: any) => Boolean(props.rowSelection.getCheckboxProps?.(row)?.disabled),
      })
      const pagination = !props.pagination ? false : {
        ...(typeof props.pagination === 'object' ? props.pagination : {}),
        page: props.pagination.current,
        pageSize: props.pagination.pageSize || 10,
        itemCount: props.pagination.total,
        showSizePicker: props.pagination.showSizeChanger,
        showQuickJumper: props.pagination.showQuickJumper,
        pageSizes: props.pagination.pageSizeOptions?.map(Number) || [10, 20, 50, 100],
        prefix: props.pagination.showTotal ? (info: any) => props.pagination.showTotal(info.itemCount) : undefined,
        onUpdatePage: (page: number) => { props.pagination.onChange?.(page, props.pagination.pageSize || 10); pageChange(page) },
        onUpdatePageSize: (size: number) => { props.pagination.onShowSizeChange?.(1, size); props.pagination.onChange?.(1, size); pageChange(1, size) },
      }
      return h(NDataTable, {
        ...attrs, columns: cols, data: props.dataSource, rowKey: keyFor,
        pagination, loading: props.loading, size: props.size as any, bordered: props.bordered,
        remote: typeof props.pagination === 'object' && props.pagination.total != null,
        scrollX: props.scroll?.x, maxHeight: props.scroll?.y,
        checkedRowKeys: unref(props.rowSelection?.selectedRowKeys),
        'onUpdate:checkedRowKeys': (keys: any[], rows: any[]) => props.rowSelection?.onChange?.(keys, rows),
        onUpdateSorter: (value: any) => {
          sorter = value ? { ...value, field: value.columnKey, order: value.order } : {}
          pageChange(props.pagination?.current || 1)
        },
        onUpdateFilters: (value: any) => { filters = value; pageChange(1) },
      }, slots.empty ? { empty: slots.empty } : undefined)
    }
  },
})
