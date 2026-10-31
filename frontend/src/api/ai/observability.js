import http from '@/utils/http';

const query = (name, params) => http.get(`/api/Observability/${name}`, { params, timeout: 10000 });
export const getObservationSummary = (params) => query('Summary', params);
export const getObservationEvents = (params) => query('Events', params);
export const getObservationTrace = (params) => query('Trace', params);
export const getCollectionHealth = (params) => query('CollectionHealth', params);
export const getObservationSessions = (params) => query('Sessions', params);
export const getObservationSession = (params) => query('Session', params);
