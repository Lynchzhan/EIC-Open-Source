// 体验接口不附带正式账号 token，不将体验凭据或对话写入 Web Storage。
export async function experienceRequest(path, body, method = 'GET', signal, extraHeaders = {}) {
  const binary = typeof Blob !== 'undefined' && body instanceof Blob;
  const response = await fetch(`/api/experience${path}`, {
    method, credentials: 'same-origin', cache: 'no-store', signal,
    headers: { 'Content-Type': binary ? 'application/octet-stream' : 'application/json', 'X-EIC-Experience': '1', ...extraHeaders },
    ...(body === undefined ? {} : { body: binary ? body : JSON.stringify(body) }),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const error = new Error(typeof data.detail === 'string' ? data.detail : '体验请求未完成');
    error.status = response.status;
    throw error;
  }
  return path === '/chat' ? response : response.json();
}
