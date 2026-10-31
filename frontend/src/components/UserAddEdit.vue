<template>
  <e-modal
    v-model:open="modalVisible"
    :title="modalTitle"
    ok-text="确定"
    cancel-text="取消"
    @ok="handleOk"
    @cancel="handleCancel"
    :width="900"
  :confirm-loading="confirmLoading"
  >
    <e-form :model="form" :label-col="{ span: 6 }" :wrapper-col="{ span: 18 }">
      <e-row :gutter="16">
        <e-col :span="12">
          <e-form-item label="用户名" v-bind="validateInfos.username">
            <e-input v-model:value="form.username" class="custom-input" />
          </e-form-item>
        </e-col>
        <e-col :span="12">
          <e-form-item label="昵称" v-bind="validateInfos.nickName">
            <e-input v-model:value="form.nickName" class="custom-input" />
          </e-form-item>
        </e-col>
        <e-col :span="12">
          <e-form-item label="手机号" v-bind="validateInfos.phone">
            <e-input v-model:value="form.phone" class="custom-input" />
          </e-form-item>
        </e-col>
        <e-col :span="12">
          <e-form-item label="邮箱" v-bind="validateInfos.email">
            <e-input v-model:value="form.email" class="custom-input" />
          </e-form-item>
        </e-col>
        <e-col :span="12">
          <e-form-item label="系统角色" v-bind="validateInfos.roles">
            <e-select
              v-model:value="form.roles"
              mode="multiple"
              placeholder="决定菜单和接口权限，与岗位不同"
              class="custom-select"
              :loading="roleLoading"
            >
              <e-select-option v-for="role in roleList" :key="role.id" :value="role.id">
                {{ role.name }}
              </e-select-option>
            </e-select>
          </e-form-item>
        </e-col>
        <e-col :span="12">
          <e-form-item label="部门" v-bind="validateInfos.departmentId">
            <e-tree-select
              v-model:value="form.departmentId"
              show-search
              placeholder="请选择部门"
              :dropdown-style="{ maxHeight: '400px', overflow: 'auto' }"
              :tree-data="departmentTreeData"
              :filterTreeNode="filterTreeNode"
              tree-default-expand-all
              :getPopupContainer="(trigger) => trigger.parentNode || document.body"
              style="width: 100%"
            />
          </e-form-item>
        </e-col>
        <e-col :span="12">
          <e-form-item label="岗位" v-bind="validateInfos.positions">
            <e-tree-select
              v-model:value="form.positions"
              multiple
              show-search
              placeholder="请选择岗位"
              :dropdown-style="{ maxHeight: '400px', overflow: 'auto' }"
              :tree-data="positionTreeData"
              :filterTreeNode="filterTreeNode"
              tree-default-expand-all
              :getPopupContainer="(trigger) => trigger.parentNode || document.body"
              style="width: 100%"
            />
          </e-form-item>
        </e-col>
        <e-col :span="12">
          <e-form-item label="员工状态">
            <e-select v-model:value="form.dtoUserInfo.employeeStatus" placeholder="请选择员工状态">
              <e-select-option :value="-1">离职</e-select-option>
              <e-select-option :value="1">在职</e-select-option>
              <e-select-option :value="2">休假</e-select-option>
              <e-select-option :value="3">停职</e-select-option>
              <e-select-option :value="4">退休</e-select-option>
              <e-select-option :value="5">实习</e-select-option>
            </e-select>
          </e-form-item>
        </e-col>
        <e-col :span="12">
          <e-form-item label="入职时间">
            <e-date-picker v-model:value="form.dtoUserInfo.hireDate" placeholder="请选择入职时间" style="width: 100%" />
          </e-form-item>
        </e-col>
        <e-col :span="12">
          <e-form-item label="个性签名">
            <e-input v-model:value="form.dtoUserInfo.signature" placeholder="请输入个性签名" />
          </e-form-item>
        </e-col>
        <e-col :span="12">
          <e-form-item label="性别">
            <e-select v-model:value="form.dtoUserInfo.sex" placeholder="请选择性别">
              <e-select-option :value="true">男</e-select-option>
              <e-select-option :value="false">女</e-select-option>
            </e-select>
          </e-form-item>
        </e-col>
        <e-col :span="12">
          <e-form-item label="工号">
            <e-input v-model:value="form.dtoUserInfo.employeeNo" placeholder="请输入工号" />
          </e-form-item>
        </e-col>
        <e-col :span="12">
          <e-form-item label="上级">
            <e-input v-model:value="form.dtoUserInfo.supervisorId" placeholder="请输入上级用户ID" />
          </e-form-item>
        </e-col>
        <e-col :span="12">
          <e-form-item label="微信号">
            <e-input v-model:value="form.dtoUserInfo.weChat" placeholder="请输入微信号" />
          </e-form-item>
        </e-col>
        <e-col :span="12">
          <e-form-item label="QQ">
            <e-input v-model:value="form.dtoUserInfo.qq" placeholder="请输入QQ号" />
          </e-form-item>
        </e-col>
        <e-col :span="12">
          <e-form-item label="状态">
            <e-switch
              v-model:checked="form.status"
              checked-children="启用"
              un-checked-children="禁用"
            /> 
          </e-form-item>
        </e-col>
        <e-col :span="12">
          <e-form-item label="密码" v-bind="validateInfos.PassWord">
            <e-input-password
              size="middle"
              placeholder="密码"
              v-model:value="form.PassWord"
            />
          </e-form-item>
        </e-col>
        <e-col :span="24">
          <e-form-item label="头像">
            <e-upload
              v-model:file-list="form.avatar"
              list-type="picture-card"
              class="avatar-uploader"
              :show-upload-list="false"
              :before-upload="beforeUpload"
            >
              <AuthenticatedImage
                v-if="form.avatarUrl"
                :source="form.avatarUrl"
                style="width: 100%"
              />
              <div v-else>
                <PlusOutlined />
                <div style="margin-top: 8px">上传头像</div>
              </div>
            </e-upload>
          </e-form-item>
        </e-col>
      </e-row>
    </e-form>
  </e-modal>
</template>

<script setup>
/* eslint-disable no-undef */
import { ref, reactive, watch } from 'vue';
import AuthenticatedImage from './AuthenticatedImage.vue';
import { PlusOutlined } from '@/app/ui/icons';
import { message } from '@/app/ui/controls';
import { Form } from '@/app/ui/controls'; 
import { GetSnowflakeId } from "../api/baseapi"; 
import { createUser, updateUser, getUserRoleList } from "../api/userapi";
import { getPositionTree } from "../api/organizational/position";
import { getDepartmentTree } from "../api/organizational/department";
import dayjs from 'dayjs';
const emit = defineEmits(['ok', 'cancel']);

const useForm = Form.useForm;

// 表单数据
const form = reactive({
  username: "",
  id: "",
  nickName: "",
  PassWord: "",
  phone: "",
  email: "",
  roles: [],
  positions:[],
  departmentId: "",
  status: true,
  avatar: [],
  avatarUrl: "",
 dtoUserInfo:{ 
    departmentId: "",
    departmentName: "",
    employeeStatus: 1,
    hireDate: null,
    signature: "",
    sex: null,
    supervisorId: null,
    employeeNo: "",
    weChat: "",
    qq: "",
    id: 0
  }
});

// 表单验证规则
const formRules = reactive({
  username: [
    { required: true, message: "请输入用户名" },
    { min: 2, message: "用户名至少2个字符" },
  ],
  nickName: [{ required: true, message: "请输入昵称" }],
  phone: [
    {
      validator: (_rule, value) => {
        if (!value) return Promise.resolve();
        if (!/^1[3-9]\d{9}$/.test(value)) return Promise.reject("请输入正确的手机号");
        return Promise.resolve();
      }
    }
  ],
  email: [ 
    { type: "email", message: "请输入有效的邮箱地址" },
  ], 
  roles: [ 
      { required: true, message: "请选择角色" },   
  ], positions: [ 
      { required: true, message: "请选择岗位" },   
  ],
  departmentId: [ 
      { required: true, message: "请选择部门" },   
  ],
});

// 表单验证
const { validate: validateForm, validateInfos } = useForm(form, formRules);

// Props
const props = defineProps({
  visible: {
    type: Boolean,
    default: false
  },
  title: {
    type: String,
    default: "用户管理"
  },
  user: {
    type: Object,
    default: null
  },
  mode: {
    type: String,
    default: "create",
    validator: (value) => ["create", "edit"].includes(value)
  },
  roleList: {
    type: Array,
    default: () => []
  },
  roleLoading: {
    type: Boolean,
    default: false
  }
});

// 内部状态
const modalVisible = ref(false);
const modalTitle = ref("用户管理");
const confirmLoading = ref(false);
const roleLoading = ref(false);
const roleList = ref([]);
const positionTreeData = ref([]);
const departmentTreeData = ref([]);
let formRevision = 0;
// 监听props变化
watch(() => props.visible, (newVal) => {
  modalVisible.value = newVal;
  if (newVal) {
    loadRoleList();
    loadPositionList();
    loadDepartmentList();
  }
});

watch(() => props.title, (newVal) => {
  modalTitle.value = newVal;
});

const initializeForm = (newVal, mode) => {
  formRevision += 1;
  form.PassWord = "";
  if (mode === "edit" && newVal && newVal.id) {
    // 填充表单数据
    const userInfo = newVal.dtoUserInfo || {};
    Object.assign(form, {
      id: newVal.id,
      username: newVal.name,
      nickName: newVal.nickName,
      phone: newVal.phone,
      email: newVal.email,
      roles: Array.isArray(newVal.roles) ? newVal.roles.map((role) => role.id) : [],
      positions: Array.isArray(newVal.positions) ? newVal.positions.map((role) => role.id) : [],
      departmentId: userInfo.departmentId || newVal.departmentId || "",
      status: newVal.status == 1,
      avatar: [],
      avatarUrl: newVal.avatar || newVal.avatarUrl || "",
      dtoUserInfo:{ 
        departmentId: userInfo.departmentId || "",
        departmentName: userInfo.departmentName || "",
        employeeStatus: userInfo.employeeStatus !== undefined ? userInfo.employeeStatus : 1,
        hireDate: userInfo.hireDate ? dayjs(userInfo.hireDate) : null,
        signature: userInfo.signature || "",
        sex: userInfo.sex,
        supervisorId: userInfo.supervisorId,
        employeeNo: userInfo.employeeNo || "",
        weChat: userInfo.weChat || "",
        qq: userInfo.qq || "",
        id: userInfo.id || 0
      }
    });
  } else {
    Object.assign(form, {
      username: "",
      id: "",
      nickName: "",
      phone: "",
      email: "",
      roles: [],
      positions: Array.isArray(newVal?.positions) ? newVal.positions.map((role) => role.id) : [],
      departmentId: newVal?.dtoUserInfo?.departmentId || "",
      status: true,
      avatar: [],
      avatarUrl: "",
      dtoUserInfo:{ 
        departmentId: newVal?.dtoUserInfo?.departmentId || "",
        departmentName: "",
        employeeStatus: 1,
        hireDate: null,
        signature: "",
        sex: null,
        supervisorId: null,
        employeeNo: "",
        weChat: "",
        qq: "",
        id: 0
      }
    });
  }
};

watch(
  () => [props.visible, props.user, props.mode],
  ([visible, user, mode]) => {
    initializeForm(visible ? user : null, visible ? mode : "create");
  },
  { immediate: true, flush: "sync" }
);

// 头像上传前处理
const beforeUpload = (file) => {
  const isJpgOrPng = file.type === "image/jpeg" || file.type === "image/png";
  if (!isJpgOrPng) {
    message.error("只能上传JPG/PNG格式的图片!");
  }
  const isLt2M = file.size / 1024 / 1024 < 2;
  if (!isLt2M) {
    message.error("图片大小不能超过2MB!");
  }
  if (isJpgOrPng && isLt2M) {
    // 读取文件并预览
    const reader = new FileReader();
    const revision = formRevision;
    reader.onload = (e) => {
      if (revision === formRevision && props.visible) {
        form.avatarUrl = e.target.result;
      }
    };
    reader.readAsDataURL(file);
  }
  return false; // 不自动上传
};

// 加载角色列表
const loadRoleList = async () => {
  roleLoading.value = true;
  try {
    const response = await getUserRoleList();
    if (response && response.code === 200 && response.data) {
      roleList.value = response.data.map((role) => ({
        id: role.key ?? role.id,
        name: role.value ?? role.name,
        value: role.key ?? role.id,
      }));
    }
  } catch (error) {
    console.error("获取角色列表失败:", error);
    message.error("获取角色列表失败: " + error.message);
  } finally {
    roleLoading.value = false;
  }
};
const loadPositionList = async () => {
  roleLoading.value = true;
  try {
    const response = await getPositionTree();
    if (response && response.code === 200 && response.data) {
      positionTreeData.value = convertToTreeData(response.data);
    }
  } catch (error) {
    console.error("获取岗位列表失败:", error);
    message.error("获取岗位列表失败: " + error.message);
  } finally {
    roleLoading.value = false;
  }
};
const loadDepartmentList = async () => {
  roleLoading.value = true;
  try {
    const response = await getDepartmentTree();
    if (response && response.code === 200 && response.data) {
      departmentTreeData.value = convertToTreeData(response.data);
    }
  } catch (error) {
    console.error("获取部门列表失败:", error);
    message.error("获取部门列表失败: " + error.message);
  } finally {
    roleLoading.value = false;
  }
};

const convertToTreeData = (data) => {
  if (!data) return [];
  const traverse = (node) => {
    if (!node) return null;
    const title = node.code ? `${node.name} - ${node.code}` : (node.name || "");
    const treeNode = {
      value: node.id == null ? node.id : String(node.id),
      title,
      key: node.id == null ? node.id : String(node.id)
    };
    if (node.children && node.children.length > 0) {
      treeNode.children = node.children.map(child => traverse(child)).filter(child => child !== null);
    }
    return treeNode;
  };
  const nodes = Array.isArray(data) ? data : [data];
  return nodes.map((node) => traverse(node)).filter((node) => node !== null);
};

const filterTreeNode = (inputValue, treeNode) => {
  return treeNode.title.toLowerCase().indexOf(inputValue.toLowerCase()) >= 0;
};

const findPositionNameById = (id, treeData) => {
  if (!treeData || !Array.isArray(treeData)) return null;
  for (const node of treeData) {
    if (node.value === id) {
      return node.title.split(' - ')[0];
    }
    if (node.children && node.children.length > 0) {
      const found = findPositionNameById(id, node.children);
      if (found) return found;
    }
  }
  return null;
}; 

// 处理确认
const handleOk = () => {
  if (confirmLoading.value || !props.visible) return;
  const revision = formRevision;
  const mode = props.mode;
  const targetId = mode === "edit" ? form.id : "";
  if (mode === "edit" && !targetId) {
    message.error("缺少待编辑用户ID");
    return;
  }
  confirmLoading.value = true;
  validateForm()
    .then(async () => {
      if (revision !== formRevision || !props.visible) return;
      if(!form.departmentId){
           message.error("请选择部门！");
            return;
      }
      form.dtoUserInfo.departmentId = form.departmentId;
      try {
        confirmLoading.value = true;
        const selected = (form.roles || []).map((item) => String(item));
        var roles = roleList.value.filter((role) => selected.includes(String(role.id)));
        var positions = [];
        if (form.positions && form.positions.length > 0) {
          form.positions.forEach(posId => {
            var posName = findPositionNameById(posId, positionTreeData.value);
            if (posName) {
              positions.push({ id: posId, name: posName });
            }
          });
        }
        const userData = {
          id: targetId,
          username: form.username,
          name: form.username,
          nickName: form.nickName,
          PassWord: form.PassWord,
          phone: form.phone,
          email: form.email,
          departmentId: form.departmentId,
          positionId: form.positions && form.positions.length ? form.positions[0] : null,
          dtoUserInfo: { ...form.dtoUserInfo },
          roles: roles.map((role) => ({ id: role.id, name: role.name })),
          positions: positions.length ? positions : (form.positions || []).map((posId) => ({ id: posId })),
          status: form.status,
          avatar: form.avatarUrl,
          avatarUrl: form.avatarUrl,
          headImgs: form.avatarUrl
            ? [{ key: "avatar", value: form.avatarUrl }]
            : [],
        };
        if (mode === "edit" && !userData.PassWord) delete userData.PassWord;

        let result;
        // 如果是新增用户，获取新ID
        if (mode === "create") {
          var dataid = await GetSnowflakeId();
          if (revision !== formRevision || !props.visible) return;
          if (dataid && dataid.code == 200 && dataid.data) {
            var id = dataid.data;
            userData.id = id;
            result = await createUser(userData);
          } else {
            message.error("获取用户ID失败"); 
            return;
          }
        } else {
          // 编辑用户
          result = await updateUser(userData);
        }

        if (revision !== formRevision || !props.visible) return;
        if (result && result.code == 200) {
          form.PassWord = "";
          delete userData.PassWord;
          message.success(mode === "edit" ? "用户信息更新成功" : "用户添加成功");
          emit('ok', userData, mode);
        } else {
          message.error(mode === "edit" ? "用户信息更新失败" : "用户添加失败");
        }
      } catch {
        if (revision === formRevision && props.visible) {
          message.error(mode === "edit" ? "用户信息更新失败" : "用户添加失败");
        }
      }
    })
    .catch(() => {
      // 字段校验由表单展示，避免记录含密码的表单对象。
    })
    .finally(() => {
      confirmLoading.value = false;
    });
};

// 处理取消
const handleCancel = () => {
    formRevision += 1;
    form.PassWord = "";
    Object.assign(form, {
      username: "",
      id: "",
      nickName: "",
      phone: "",
      email: "",
      roles: [],
      positions:[],
      departmentId: "",
      status: true,
      avatar: [],
      avatarUrl: "",
      dtoUserInfo:{ 
        departmentId: "",
        departmentName: "",
        employeeStatus: 1,
        hireDate: null,
        signature: "",
        sex: null,
        supervisorId: null,
        employeeNo: "",
        weChat: "",
        qq: "",
        id: 0
      }
    });
  emit('cancel');
};
</script>
