import request from '@/utils/http';

export function getTenantPage(params) {
    return request({
        url: '/api/Tenant/GetPageData',
        method: 'post',
        data: params
    });
}

export function createTenant(data) {
    return request({
        url: '/api/Tenant/AddEdit',
        method: 'post',
        data
    });
}

export function editTenant(data) {
    return request({
        url: '/api/Tenant/AddEdit',
        method: 'post',
        data
    });
}

export function deleteTenant(id) {
    return request({
        url: `/api/Tenant/Delete?Id=${id}`,
        method: 'delete'
    });
}

export function activateTenant(id) {
    return request({
        url: '/api/Tenant/AddEdit',
        method: 'post',
        data: { id, active: true }
    });
}

export function deactivateTenant(id) {
    return request({
        url: '/api/Tenant/AddEdit',
        method: 'post',
        data: { id, active: false }
    });
}
