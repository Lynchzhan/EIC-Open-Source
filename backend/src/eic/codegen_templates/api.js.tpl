import http from '../../utils/http'
const base = '/api/Generated/__ENTITY__'
export const listRecords = payload => http.post(`${base}/GetPageData`, payload)
export const saveRecord = payload => http.post(`${base}/AddEdit`, payload)
export const deleteRecord = id => http.delete(`${base}/Delete`, { params: { Id: id } })
