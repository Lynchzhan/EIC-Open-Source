<template>
  <div class="user-profile-container">
    <e-card class="user-profile-card">

      <div class="profile-content">
        <e-row :gutter="24">
          <e-col :span="8" class="avatar-section">
            <div class="avatar-wrapper">
              <e-upload
                v-model:file-list="avatarFileList"
                list-type="picture-card"
                class="avatar-uploader"
                :show-upload-list="false"
                :before-upload="beforeUpload"
              >
                <AuthenticatedImage v-if="avatarSrc" :source="avatarSrc" class="avatar-preview" />
                <div v-else>
                  <PlusOutlined />
                  <div style="margin-top: 8px">上传头像</div>
                </div>
              </e-upload>
              <h3>{{ form.nickName || form.name }}</h3>
              <p class="user-id">ID: {{ form.id }}</p>
            </div>
          </e-col>

          <e-col :span="16" class="info-section">
            <e-form :model="form" :label-col="{ span: 6 }" :wrapper-col="{ span: 18 }">
              <e-form-item label="用户名">
                <e-input :value="form.name" disabled />
              </e-form-item>
              <e-form-item label="昵称">
                <e-input v-model:value="form.nickName" />
              </e-form-item>
              <e-form-item label="手机号">
                <e-input v-model:value="form.phone" />
              </e-form-item>
              <e-form-item label="邮箱">
                <e-input v-model:value="form.email" />
              </e-form-item>
              <e-form-item label="部门">
                <e-select v-model:value="form.departmentId" allow-clear placeholder="请选择部门" :options="departmentOptions" />
              </e-form-item>
              <e-form-item label="岗位">
                <e-select v-model:value="form.positionId" allow-clear placeholder="请选择岗位" :options="positionOptions" />
              </e-form-item>
              <e-form-item label="角色">
                <e-tag v-for="role in form.roles" :key="role.id" color="blue">{{ role.name }}</e-tag>
              </e-form-item>
              <e-form-item label="创建时间">
                <span>{{ form.createTime }}</span>
              </e-form-item>
              <e-form-item>
                <e-space>
                  <e-button type="primary" :loading="saving" @click="handleSave">保存资料</e-button>
                  <e-button danger @click="changePasswordAction">修改密码</e-button>
                </e-space>
              </e-form-item>
            </e-form>
          </e-col>
        </e-row>
      </div>
    </e-card>

    <AppearanceSettings />
    <e-modal v-model:open="changePasswordVisible" title="修改密码" @ok="handleChangePassword"
      @cancel="cancelChangePassword" :confirm-loading="changePasswordLoading">
      <e-form :model="changePasswordForm" :label-col="{ span: 6 }" :wrapper-col="{ span: 18 }">
        <e-form-item label="原密码" name="OldPwd" :rules="[{ required: true, message: '请输入原密码' }]">
          <e-input-password v-model:value="changePasswordForm.OldPwd" />
        </e-form-item>
        <e-form-item label="新密码" name="NewPwd" :rules="[{ required: true, message: '请输入新密码' }]">
          <e-input-password v-model:value="changePasswordForm.NewPwd" />
        </e-form-item>
        <e-form-item label="确认密码" name="confirmPassword"
          :rules="[{ required: true, message: '请确认新密码' }, { validator: validateConfirmPassword }]">
          <e-input-password v-model:value="changePasswordForm.confirmPassword" />
        </e-form-item>
      </e-form>
    </e-modal>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue';
import { PlusOutlined, UserOutlined } from '@/app/ui/icons';
import { message } from '@/app/ui/controls';
import { changePassword, getTokenUser, updateUser } from '@/api/userapi';
import { getDepartmentALLList } from '@/api/organizational/department';
import { getPositionALLList } from '@/api/organizational/position';
import { isMutationSuccess } from '@/utils/http';
import AuthenticatedImage from '@/components/AuthenticatedImage.vue';
import defaultAvatar from '@/assets/eic-avatar.svg';
import AppearanceSettings from '@/components/AppearanceSettings.vue';

const saving = ref(false);
const avatarFileList = ref([]);
const departmentOptions = ref([]);
const positionOptions = ref([]);

const form = reactive({
  id: '',
  name: '',
  nickName: '',
  phone: '',
  email: '',
  roles: [],
  createTime: '',
  headImgs: [],
  avatarUrl: '',
  departmentId: undefined,
  positionId: undefined,
  dtoUserInfo: {},
  positions: [],
});

const avatarSrc = computed(() => {
  const imgs = form.headImgs;
  if (Array.isArray(imgs) && imgs.length) {
    const first = imgs[0];
    if (typeof first === 'string' && first) return first;
    if (first && (first.value || first.url)) return first.value || first.url;
  }
  return form.avatarUrl || defaultAvatar;
});

const changePasswordVisible = ref(false);
const changePasswordLoading = ref(false);
const changePasswordForm = reactive({
  OldPwd: '',
  NewPwd: '',
  confirmPassword: ''
});

const unwrap = (response) => (response && response.data !== undefined ? response.data : response);

const applyUser = (data) => {
  const dto = data.dtoUserInfo || {};
  form.id = data.id;
  form.name = data.name || data.username || '';
  form.nickName = data.nickName || data.nickname || '';
  form.phone = data.phone || '';
  form.email = data.email || '';
  form.roles = data.roles || [];
  form.createTime = data.createTime || data.createdAt || '';
  form.headImgs = data.headImgs || [];
  form.avatarUrl = data.avatarUrl || data.avatar || (form.headImgs[0] && (form.headImgs[0].value || form.headImgs[0])) || '';
  form.departmentId = data.departmentId || dto.departmentId || undefined;
  form.positionId = data.positionId || (data.positions && data.positions[0] && data.positions[0].id) || undefined;
  form.dtoUserInfo = dto;
  form.positions = data.positions || [];
  localStorage.setItem('user', JSON.stringify(data));
};

const fetchUserProfile = async () => {
  try {
    const response = await getTokenUser();
    applyUser(unwrap(response) || {});
  } catch (error) {
    console.error('获取用户信息失败:', error);
    message.error('获取用户信息失败');
  }
};

const flattenOptions = (nodes) => {
  const items = [];
  const walk = (node) => {
    if (!node) return;
    if (Array.isArray(node)) {
      node.forEach(walk);
      return;
    }
    if (node.id != null) {
      items.push({ value: node.id, label: node.name || node.title || String(node.id) });
    }
    if (node.children) walk(node.children);
  };
  walk(nodes);
  return items;
};

const loadOrgOptions = async () => {
  try {
    const [departments, positions] = await Promise.all([getDepartmentALLList(), getPositionALLList()]);
    departmentOptions.value = flattenOptions(unwrap(departments) || []);
    positionOptions.value = flattenOptions(unwrap(positions) || []);
  } catch (error) {
    console.error('加载组织选项失败:', error);
  }
};

const beforeUpload = (file) => {
  const isJpgOrPng = file.type === 'image/jpeg' || file.type === 'image/png';
  if (!isJpgOrPng) {
    message.error('只能上传JPG/PNG格式的图片!');
    return false;
  }
  const isLt2M = file.size / 1024 / 1024 < 2;
  if (!isLt2M) {
    message.error('图片大小不能超过2MB!');
    return false;
  }
  const reader = new FileReader();
  reader.onload = (e) => {
    form.avatarUrl = e.target.result;
    form.headImgs = [{ key: 'avatar', value: e.target.result }];
  };
  reader.readAsDataURL(file);
  return false;
};

const handleSave = async () => {
  saving.value = true;
  try {
    const payload = {
      id: form.id,
      nickName: form.nickName,
      phone: form.phone,
      email: form.email,
      avatarUrl: form.avatarUrl,
      headImgs: form.avatarUrl ? [{ key: 'avatar', value: form.avatarUrl }] : [],
      departmentId: form.departmentId,
      positionId: form.positionId,
      positions: form.positionId ? [{ id: form.positionId }] : [],
      dtoUserInfo: {
        ...(form.dtoUserInfo || {}),
        departmentId: form.departmentId,
      },
    };
    const response = await updateUser(payload);
    if (!isMutationSuccess(response)) {
      message.error('保存资料未生效');
      return;
    }
    message.success('资料已保存');
    await fetchUserProfile();
  } catch (error) {
    console.error('保存资料失败:', error);
    message.error('保存资料失败: ' + (error.message || '未知错误'));
  } finally {
    saving.value = false;
  }
};

const changePasswordAction = () => {
  changePasswordForm.OldPwd = '';
  changePasswordForm.NewPwd = '';
  changePasswordForm.confirmPassword = '';
  changePasswordVisible.value = true;
};

const validateConfirmPassword = (_, value) => {
  if (value && value !== changePasswordForm.NewPwd) {
    return Promise.reject('两次输入的密码不一致');
  }
  return Promise.resolve();
};

const handleChangePassword = async () => {
  try {
    changePasswordLoading.value = true;
    if (!changePasswordForm.OldPwd) {
      message.error('请输入原密码');
      return;
    }
    if (!changePasswordForm.NewPwd) {
      message.error('请输入新密码');
      return;
    }
    if (changePasswordForm.NewPwd !== changePasswordForm.confirmPassword) {
      message.error('两次输入的密码不一致');
      return;
    }
    const response = await changePassword({
      oldPassword: changePasswordForm.OldPwd,
      newPassword: changePasswordForm.NewPwd
    });
    if (!isMutationSuccess(response)) {
      message.error('密码修改未生效');
      return;
    }
    message.success('密码修改成功');
    changePasswordForm.OldPwd = '';
    changePasswordForm.NewPwd = '';
    changePasswordForm.confirmPassword = '';
    changePasswordVisible.value = false;
  } catch (error) {
    // 不打印含请求配置/密码的原始异常。
    message.error('修改密码失败: ' + (error.message || '未知错误'));
  } finally {
    changePasswordLoading.value = false;
  }
};

const cancelChangePassword = () => {
  if (changePasswordLoading.value) return;
  changePasswordForm.OldPwd = '';
  changePasswordForm.NewPwd = '';
  changePasswordForm.confirmPassword = '';
  changePasswordVisible.value = false;
};

onMounted(() => {
  fetchUserProfile();
  loadOrgOptions();
});
</script>

<style scoped>

.user-profile-container {
  min-height: 100%;
  padding: 0;
}

.user-profile-card {
  min-height: 0;
  background: var(--eic-surface);
  border-radius: 8px;
  border: 1px solid var(--eic-line);
  color: var(--eic-text);
  overflow: hidden;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03);
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.header-title {
  display: flex;
  align-items: center;
  font-size: var(--eic-font-section);
  font-weight: 500;
  color: var(--eic-text);
}

.title-icon {
  margin-right: 8px;
  font-size: 20px;
  color: var(--eic-primary);
}

.profile-content {
  padding: 20px;
}

.avatar-section {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-start;
  padding-top: 20px;
}

.avatar-wrapper {
  text-align: center;
  margin-bottom: 20px;
}

.avatar-preview {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.avatar-wrapper h3 {
  margin-top: 16px;
  margin-bottom: 8px;
  font-size: 20px;
  color: var(--eic-text);
}

.user-id {
  color: var(--eic-muted);
  font-size: var(--eic-font-meta);
  margin: 0;
}

.info-section {
  padding-top: 20px;
}
</style>
