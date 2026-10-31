import { defineComponent, h, ref, watch, type PropType } from 'vue'
import { NTree, NTreeSelect, NUpload, NDatePicker } from 'naive-ui'
import dayjs from 'dayjs'

const normalize = (rows: any[]): any[] => (rows || []).map(row => ({...row,key:row.key??row.value??row.id,label:row.title??row.label??row.name,children:row.children?.length?normalize(row.children):undefined}))
export const Tree=defineComponent({
  inheritAttrs:false,props:{treeData:Array,checkedKeys:[Array,Object],expandedKeys:Array,selectedKeys:Array,checkable:Boolean,disabled:Boolean,defaultExpandAll:Boolean,checkStrictly:Boolean,autoExpandParent:Boolean},
  emits:['update:checkedKeys','update:expandedKeys','update:selectedKeys','check','expand','select'],
  setup(p,{attrs,slots,emit}){const halfChecked=ref<any[]>([]);return()=>h(NTree,{...attrs,data:normalize(p.treeData||[]),checkable:p.checkable,disabled:p.disabled,
    checkedKeys:(Array.isArray(p.checkedKeys)?p.checkedKeys:(p.checkedKeys as any)?.checked) as any,expandedKeys:p.expandedKeys as any,selectedKeys:p.selectedKeys as any,
    cascade:!p.checkStrictly,defaultExpandAll:p.defaultExpandAll,checkStrategy:'all',
    renderLabel:slots.title?({option}:any)=>slots.title?.({...option,title:option.label,dataRef:option}):undefined,
    'onUpdate:indeterminateKeys':(keys:any[])=>{halfChecked.value=keys},
    'onUpdate:checkedKeys':(keys:any[],options:any[],meta:any)=>{emit('update:checkedKeys',keys);emit('check',keys,{checked:meta.action==='check',node:meta.node,checkedNodes:options,halfCheckedKeys:halfChecked.value})},
    'onUpdate:expandedKeys':(keys:any[])=>{emit('update:expandedKeys',keys);emit('expand',keys)},
    'onUpdate:selectedKeys':(keys:any[],options:any[])=>{emit('update:selectedKeys',keys);emit('select',keys,{node:{...options[0],dataRef:options[0]},selectedNodes:options})},
  })},
})
export const TreeSelect=defineComponent({inheritAttrs:false,props:{treeData:Array,value:[String,Number,Array],multiple:Boolean,showSearch:Boolean,allowClear:Boolean,treeDefaultExpandAll:Boolean,filterTreeNode:Function},emits:['update:value','change'],setup(p,{attrs,emit}){return()=>h(NTreeSelect,{...attrs,options:normalize(p.treeData||[]),value:p.value as any,multiple:p.multiple,filterable:p.showSearch,clearable:p.allowClear,defaultExpandAll:p.treeDefaultExpandAll,
  filter:p.filterTreeNode?((pattern:string,node:any)=>p.filterTreeNode?.(pattern,{...node,title:node.label})):undefined,
  'onUpdate:value':(v:any)=>{emit('update:value',v);emit('change',v)},
})}})
export const Upload=defineComponent({
  inheritAttrs:false,props:{fileList:Array,customRequest:Function,beforeUpload:Function,accept:String,multiple:Boolean,maxCount:Number,disabled:Boolean,showUploadList:{type:Boolean,default:true},listType:String},
  emits:['update:fileList','change'],setup(p,{attrs,slots,emit}){
    return()=>h(NUpload,{...attrs,defaultUpload:true,accept:p.accept,multiple:p.multiple,max:p.maxCount,disabled:p.disabled,showFileList:p.showUploadList,listType:p.listType==='picture-card'?'image-card':p.listType==='picture'?'image':'text',
      fileList:p.fileList?.map((f:any)=>({...f,id:String(f.id??f.uid),status:f.status==='done'?'finished':f.status||'pending'})),
      onBeforeUpload:async({file,fileList}:any)=>p.beforeUpload?await p.beforeUpload(file.file,fileList.map((f:any)=>f.file)):true,
      customRequest:p.customRequest?((options:any)=>p.customRequest?.({file:options.file.file,onSuccess:()=>options.onFinish(),onError:()=>options.onError(),onProgress:({percent}:any)=>options.onProgress({percent})})):undefined,
      'onUpdate:fileList':(files:any[])=>{const list=files.map(f=>({...f,uid:f.id,status:f.status==='finished'?'done':f.status}));emit('update:fileList',list);emit('change',{fileList:list})},
    },slots)
  },
})
export const DatePicker=defineComponent({inheritAttrs:false,props:{value:{type:[String,Number,Object] as PropType<any>}},emits:['update:value','change'],setup(p,{attrs,emit}){return()=>h(NDatePicker,{...attrs,type:'date',value:p.value&&dayjs(p.value).isValid()?dayjs(p.value).valueOf():null,clearable:true,
  'onUpdate:value':(v:number|null)=>{const value=v==null?null:dayjs(v);emit('update:value',value);emit('change',value,value?.format('YYYY-MM-DD'))},
})}})
