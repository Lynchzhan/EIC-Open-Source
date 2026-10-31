import { defineComponent, h, ref, provide, inject, getCurrentInstance, type Component, type App } from 'vue'
import {
  NButton, NInput, NInputNumber, NForm, NAlert, NSpin, NCard, NSpace, NTag, NEmpty,
  NSwitch, NCheckbox, NCheckboxGroup, NRadio, NRadioGroup, NRadioButton, NBadge,
  NTabs, NTabPane, NCollapse, NCollapseItem, NDescriptions, NDescriptionsItem,
  NDivider, NSkeleton, NSlider, NProgress, NTooltip, NPopover, NPagination, NAutoComplete,
} from 'naive-ui'
import EicModal from './EicModal.vue'
import EicDrawer from './EicDrawer.vue'
import EicSelect, { EicOption } from './EicSelect'
import EicTable from './EicTable'
import EicFormItem from './EicFormItem.vue'
import EicPopconfirm from './EicPopconfirm.vue'
import { EicRow, EicCol } from './EicGrid'
import { Tree, TreeSelect, Upload, DatePicker } from './EicDataControls'
import { useForm } from './formValidation'
import { message, confirm, warning } from './feedback'

export { message }
export const Form = { useForm }
export const Modal = { confirm, warning }

export const Button = defineComponent({
  inheritAttrs: false,
  props: { type: String, danger: Boolean, ghost: Boolean, htmlType: String, block: Boolean },
  setup(p, { attrs, slots }) { return () => h(NButton, {
    ...attrs, type: p.danger ? 'error' : ['primary','info','success','warning','error'].includes(p.type || '') ? p.type as any : 'default',
    text: ['text','link'].includes(p.type || ''), dashed: p.type === 'dashed', secondary: p.ghost,
    attrType: p.htmlType as any, block: p.block,
  }, slots) },
})
const input = (type?: string) => defineComponent({
  inheritAttrs: false, props: { value: [String, Number], allowClear: Boolean, autoSize: [Boolean, Object], rows: Number, showCount: Boolean },
  emits: ['update:value','change','pressEnter'],
  setup(p, { attrs, slots, emit, expose }) {
    const control = ref<any>(); expose({ focus: () => control.value?.focus(), blur: () => control.value?.blur() })
    return () => h(NInput, { ...attrs, ref: control, value: p.value == null ? '' : String(p.value), type: (type || attrs.type || 'text') as any,
      clearable: p.allowClear, autosize: p.autoSize as any, rows: p.rows, showCount: p.showCount,
      showPasswordOn: type === 'password' ? 'click' : undefined,
      'onUpdate:value': value => { emit('update:value',value); emit('change',{ target:{ value } }) },
      onKeydown: (e: KeyboardEvent) => { if (e.key === 'Enter' && !e.isComposing) emit('pressEnter', e) },
    }, slots)
  },
})
export const Input = input()
const SearchInput = defineComponent({
  inheritAttrs:false, props:{ value:String, allowClear:Boolean, enterButton:[Boolean,String] }, emits:['update:value','change','search'],
  setup(p,{ attrs,emit }) {
    const text=ref(p.value || '')
    return () => h(NInput,{...attrs,value:p.value ?? text.value,clearable:p.allowClear,
      'onUpdate:value': (v:string) => {text.value=v;emit('update:value',v);emit('change',{target:{value:v}})},
      onKeydown:(e:KeyboardEvent)=>{if(e.key==='Enter'&&!e.isComposing)emit('search',p.value??text.value)},
    },{suffix:()=>h(NButton,{text:true,attrType:'button','aria-label':'执行搜索',onClick:()=>emit('search',p.value??text.value)},()=>typeof p.enterButton==='string'?p.enterButton:'搜索')})
  },
})
const FormControl=defineComponent({
  inheritAttrs:false,props:{layout:String,model:Object,rules:Object,labelCol:Object,wrapperCol:Object},emits:['submit','finish','finishFailed'],
  setup(p,{attrs,slots,emit,expose}) {
    const form=ref<any>();expose({validate:()=>form.value?.validate(),validateFields:()=>form.value?.validate(),clearValidate:()=>form.value?.restoreValidation()})
    return ()=>h(NForm,{...attrs,ref:form,model:p.model,rules:p.rules as any,labelPlacement:'top',
      inline:p.layout==='inline',onSubmit:async(e:Event)=>{e.preventDefault();emit('submit',e);try{await form.value?.validate();emit('finish',p.model)}catch(error){emit('finishFailed',error)}},
    },slots)
  },
})
const Alert=defineComponent({
  inheritAttrs:false,props:{message:String,description:String,type:String,showIcon:Boolean,closable:Boolean},
  setup(p,{attrs,slots}){return()=>h(NAlert,{...attrs,title:p.message,type:p.type as any,showIcon:p.showIcon,closable:p.closable},{
    default:()=>[p.description,slots.default?.(),slots.action?h('div',{class:'eic-alert-action'},slots.action()):null],
  })},
})
const Spin=defineComponent({inheritAttrs:false,props:{spinning:{type:Boolean,default:true},tip:String},setup(p,{attrs,slots}){return()=>h(NSpin,{...attrs,show:p.spinning,description:p.tip},slots)}})
const Card=defineComponent({inheritAttrs:false,props:{title:String,hoverable:Boolean,bordered:{type:Boolean,default:true}},setup(p,{attrs,slots}){return()=>h(NCard,{...attrs,title:p.title,hoverable:p.hoverable,bordered:p.bordered}, {...slots,header:slots.title, 'header-extra':slots.extra})}})
const Tag=defineComponent({inheritAttrs:false,props:{color:String},setup(p,{attrs,slots}){return()=>h(NTag,{...attrs,type:({red:'error',error:'error',green:'success',success:'success',orange:'warning',warning:'warning',blue:'info',processing:'info'} as any)[p.color||'']||'default',bordered:false,round:true},slots)}})
const Switch=defineComponent({inheritAttrs:false,props:{checked:Boolean,checkedChildren:String,unCheckedChildren:String},emits:['update:checked','change'],setup(p,{attrs,emit}){return()=>h(NSwitch,{...attrs,value:p.checked,'onUpdate:value':v=>{emit('update:checked',v);emit('change',v)}},{checked:()=>p.checkedChildren,unchecked:()=>p.unCheckedChildren})}})
const Checkbox=defineComponent({inheritAttrs:false,props:{checked:{type:Boolean,default:undefined},value:[String,Number,Boolean],indeterminate:Boolean},emits:['update:checked','change'],setup(p,{attrs,slots,emit}){return()=>h(NCheckbox,{...attrs,checked:p.checked,value:p.value as any,indeterminate:p.indeterminate,'onUpdate:checked':v=>{emit('update:checked',v);emit('change',{target:{checked:v,value:p.value}})}},slots)}})
const CheckboxGroup=defineComponent({inheritAttrs:false,props:{value:Array,options:Array},emits:['update:value','change'],setup(p,{attrs,slots,emit}){return()=>h(NCheckboxGroup,{...attrs,value:p.value as any,'onUpdate:value':v=>{emit('update:value',v);emit('change',v)}},slots.default?slots:{default:()=>p.options?.map((o:any)=>h(NCheckbox,{value:o.value??o,label:o.label??String(o),disabled:o.disabled}))})}})
const RadioGroup=defineComponent({inheritAttrs:false,props:{value:[String,Number,Boolean]},emits:['update:value','change'],setup(p,{attrs,slots,emit}){return()=>h(NRadioGroup,{...attrs,value:p.value as any,'onUpdate:value':v=>{emit('update:value',v);emit('change',{target:{value:v}})}},slots)}})
const Segmented=defineComponent({inheritAttrs:false,props:{value:[String,Number],options:Array},emits:['update:value','change'],setup(p,{attrs,emit}){return()=>h(NRadioGroup,{...attrs,value:p.value,'onUpdate:value':v=>{emit('update:value',v);emit('change',v)}},{default:()=>p.options?.map((o:any)=>h(NRadioButton,{value:o.value??o,disabled:o.disabled},()=>o.label??String(o)))})}})
const Tooltip=defineComponent({inheritAttrs:false,props:{title:String},setup(p,{attrs,slots}){return()=>h(NTooltip,attrs,{trigger:slots.default,default:slots.title||(()=>p.title)})}})
const dropdownClose=Symbol('eic-dropdown-close'),menuSelect=Symbol('eic-menu-select')
const Dropdown=defineComponent({inheritAttrs:false,props:{open:{type:Boolean,default:undefined},trigger:Array,placement:String},emits:['update:open'],setup(p,{attrs,slots,emit}){
  const show=ref(false);const update=(v:boolean)=>{show.value=v;emit('update:open',v)};provide(dropdownClose,()=>update(false))
  return()=>h(NPopover,{...attrs,trigger:p.trigger?.includes('click')?'click':'hover',placement:(p.placement==='topLeft'?'top-start':'bottom-end') as any,show:p.open??show.value,'onUpdate:show':update,showArrow:false},{trigger:slots.default,default:slots.overlay})
}})
const Menu=defineComponent({emits:['click'],setup(_, {slots,emit}){provide(menuSelect,(value:any)=>emit('click',value));return()=>h('div',{class:'eic-action-menu',role:'menu'},slots.default?.())}})
const MenuItem=defineComponent({props:{disabled:Boolean,danger:Boolean},emits:['click'],setup(p,{slots,emit}){
  const key=getCurrentInstance()?.vnode.key;const close=inject<()=>void>(dropdownClose,()=>{});const select=inject<(v:any)=>void>(menuSelect,()=>{})
  const activate=(domEvent:Event)=>{if(p.disabled)return;const value={key,domEvent};emit('click',value);select(value);close()}
  return()=>h('div',{role:'menuitem',tabindex:p.disabled?-1:0,'aria-disabled':p.disabled,class:['eic-action-menu-item',{'is-danger':p.danger}],onClick:activate,onKeydown:(event:KeyboardEvent)=>{if(event.target===event.currentTarget&&(event.key==='Enter'||event.key===' ')){event.preventDefault();activate(event)}}},slots.default?.())
}})
const Pagination=defineComponent({inheritAttrs:false,props:{current:Number,pageSize:Number,total:Number,showTotal:Function,showSizeChanger:Boolean,showQuickJumper:Boolean,pageSizeOptions:Array},emits:['update:current','update:pageSize','change','showSizeChange'],setup(p,{attrs,emit}){return()=>h(NPagination,{...attrs,page:p.current,pageSize:p.pageSize,itemCount:p.total,showSizePicker:p.showSizeChanger,showQuickJumper:p.showQuickJumper,pageSizes:p.pageSizeOptions?.map(Number),
  'onUpdate:page':v=>{emit('update:current',v);emit('change',v,p.pageSize)},'onUpdate:pageSize':v=>{emit('update:pageSize',v);emit('update:current',1);emit('showSizeChange',1,v);emit('change',1,v)},
},{prefix:p.showTotal?()=>p.showTotal?.(p.total):undefined})}})
const Descriptions=defineComponent({inheritAttrs:false,props:{column:[Number,Object]},setup(p,{attrs,slots}){return()=>h(NDescriptions,{...attrs,column:typeof p.column==='number'?p.column:1},slots)}})
const Progress=defineComponent({inheritAttrs:false,props:{percent:Number,status:String},setup(p,{attrs}){return()=>h(NProgress,{...attrs,type:'line',percentage:p.percent,status:({exception:'error',success:'success'} as any)[p.status||'']||'default'})}})
const List=defineComponent({props:{dataSource:Array},setup(p,{slots}){return()=>h('div',{class:'eic-list'},p.dataSource?p.dataSource.map((item,index)=>slots.renderItem?.({item,index})):slots.default?.())}})
const ListItem=defineComponent({setup(_,{slots}){return()=>h('article',{class:'eic-list-item'},[slots.default?.(),slots.actions?h('div',{class:'eic-list-actions'},slots.actions()):null])}})
const Badge=defineComponent({inheritAttrs:false,props:{status:String,text:String,count:[String,Number],dot:Boolean},setup(p,{attrs,slots}){return()=>h('span',{class:'eic-badge'},[h(NBadge,{...attrs,value:p.count,dot:p.dot||Boolean(p.status),type:({error:'error',warning:'warning',success:'success',processing:'info'} as any)[p.status||'']||'default'},slots),p.text?h('span',p.text):null])}})
const Skeleton=defineComponent({setup(){return()=>h('div',{class:'eic-skeleton','aria-label':'正在加载',role:'status'},[h(NSkeleton,{text:true,repeat:3}),h(NSkeleton,{height:80,borderRadius:12})])}})
const ListMeta=defineComponent({props:{title:String,description:String},setup(p,{slots}){return()=>h('div',{class:'eic-list-meta'},[slots.avatar?.(),h('div',[h('strong',slots.title?.()||p.title),h('p',slots.description?.()||p.description)])])}})

export const controls: Record<string, Component> = {
  'e-button':Button,'e-input':Input,'e-input-search':SearchInput,'e-input-password':input('password'),'e-textarea':input('textarea'),
  'e-input-number':NInputNumber,'e-form':FormControl,'e-form-item':EicFormItem,'e-modal':EicModal,'e-drawer':EicDrawer,
  'e-select':EicSelect,'e-select-option':EicOption,'e-table':EicTable,'e-alert':Alert,'e-spin':Spin,'e-card':Card,'e-tag':Tag,
  'e-space':NSpace,'e-empty':NEmpty,'e-switch':Switch,'e-checkbox':Checkbox,'e-checkbox-group':CheckboxGroup,
  'e-radio-group':RadioGroup,'e-radio':NRadio,'e-segmented':Segmented,'e-tooltip':Tooltip,'e-popconfirm':EicPopconfirm,
  'e-dropdown':Dropdown,'e-menu':Menu,'e-menu-item':MenuItem,'e-menu-divider':NDivider,
  'e-tabs':NTabs,'e-tab-pane':NTabPane,'e-collapse':NCollapse,'e-collapse-panel':NCollapseItem,
  'e-descriptions':Descriptions,'e-descriptions-item':NDescriptionsItem,'e-progress':Progress,'e-slider':NSlider,'e-divider':NDivider,
  'e-pagination':Pagination,'e-row':EicRow,'e-col':EicCol,'e-badge':Badge,'e-skeleton':Skeleton,
  'e-auto-complete':NAutoComplete,'e-list':List,'e-list-item':ListItem,'e-list-item-meta':ListMeta,
  'e-tree':Tree,'e-directory-tree':Tree,'e-tree-select':TreeSelect,'e-upload':Upload,'e-date-picker':DatePicker,
}
export function installControls(app: App) { for (const [name, component] of Object.entries(controls)) app.component(name,component) }
