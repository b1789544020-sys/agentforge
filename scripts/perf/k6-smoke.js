import http from 'k6/http';
import { check, sleep } from 'k6';

const scenario = __ENV.SCENARIO || 'short';
const scenarios = {
  short: { executor: 'constant-vus', vus: 2, duration: '30s' },
  ramp: {
    executor: 'ramping-vus', startVUs: 1,
    stages: [
      { duration: '30s', target: 2 }, { duration: '30s', target: 5 },
      { duration: '1m', target: 10 }, { duration: '15s', target: 0 },
    ],
  },
  spike: {
    executor: 'ramping-vus', startVUs: 1,
    stages: [{ duration: '5s', target: 20 }, { duration: '25s', target: 20 }, { duration: '10s', target: 0 }],
  },
  soak: { executor: 'constant-vus', vus: 10, duration: '5m' },
};
if (!scenarios[scenario]) throw new Error(`Unknown SCENARIO=${scenario}`);

export const options = {
  scenarios: { [scenario]: scenarios[scenario] },
  thresholds: { http_req_failed: ['rate<0.01'], http_req_duration: ['p(95)<500'] },
};
const base = __ENV.BASE_URL || 'http://localhost:8000';
const headers = __ENV.PERF_TOKEN ? { Authorization: `Bearer ${__ENV.PERF_TOKEN}` } : {};

export default function () {
  const health = http.get(`${base}/api/health`);
  check(health, { 'health 200': (r) => r.status === 200 });
  const agents = http.get(`${base}/api/agents`, { headers });
  check(agents, { 'agents 200': (r) => r.status === 200 });
  sleep(0.1);
}
