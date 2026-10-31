import { createDiscreteApi, darkTheme, zhCN, dateZhCN } from 'naive-ui'
import { computed, h } from 'vue'
import { appearance } from '../appearance'
import { createTheme } from './theme'
import { fontSizes } from '../typography'

// 拦截器也需要反馈服务，不能依赖页面组件的 setup 生命周期。
export const { message, dialog, notification } = createDiscreteApi(
  ['message', 'dialog', 'notification'],
  { configProviderProps: computed(() => ({
    theme: appearance.value === 'dark' ? darkTheme : null,
    themeOverrides: createTheme(appearance.value === 'dark', fontSizes.value), locale: zhCN, dateLocale: dateZhCN,
  })) },
)

type ConfirmOptions = {
  title?: string; content?: string | (() => ReturnType<typeof h>);
  okText?: string; cancelText?: string;
  okType?: string;
  onOk?: () => unknown | Promise<unknown>; onCancel?: () => unknown;
}
export function confirm(options: ConfirmOptions) {
  const instance = dialog.warning({
    title: options.title, content: options.content,
    positiveButtonProps: options.okType === 'danger' ? { type: 'error' } : undefined,
    positiveText: options.okText || '确认', negativeText: options.cancelText || '取消',
    onPositiveClick: async () => {
      instance.loading = true
      try { return await options.onOk?.() }
      catch (error) { if (error) message.error(error instanceof Error ? error.message : '操作失败'); return false }
      finally { instance.loading = false }
    },
    onNegativeClick: options.onCancel,
  })
  return instance
}

export function warning(options: ConfirmOptions) {
  return dialog.warning({ title: options.title, content: options.content, positiveText: options.okText || '知道了' })
}
