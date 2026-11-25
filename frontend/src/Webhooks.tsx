import { FormEvent, useCallback, useEffect, useState } from 'react';
import {
  ArrowRight, CircleCheck, CircleX, Clock, LogIn, Plus, Radio, RefreshCw, RotateCcw, Send, ShieldCheck, ShieldX,
  Trash2, Webhook,
} from 'lucide-react';

const CHAT_API = import.meta.env.VITE_CHAT_API_URL || 'http://localhost:8100';
const PLATFORM = import.meta.env.VITE_PLATFORM_API_URL || 'http://localhost:8000';
const RECEIVER = import.meta.env.VITE_RECEIVER_URL || 'http://localhost:8200';
const POLL_MS = 3000;
const EVENT_TYPES = ['case.escalated', 'case.comment_added'] as const;

type Delivery = {
  id: number; event_id: string; subscription_id: string; status: 'pending' | 'delivered' | 'failed';
  attempts: number; last_status_code: number | null; last_error: string | null; updated_at: string;
};
type Attempt = {
  attempt_no: number; trigger: string; attempted_at: string; request_headers: Record<string, string>;
  status_code: number | null; error: string | null; response_body: string | null; duration_ms: number;
};
type EventRow = {
  id: string; event_type: string; resource_id: string; created_at: string;
  status: 'pending' | 'delivered' | 'failed' | 'no_subscribers'; deliveries: Delivery[];
};
type EventDetail = Omit<EventRow, 'deliveries'> & {
  payload: Record<string, unknown>;
  deliveries: (Delivery & { endpoint_url: string | null; attempt_log: Attempt[] })[];
};
type Stats = {
  events_total: number; deliveries: { pending: number; delivered: number; failed: number };
  attempts_total: number; failed_attempts: number; subscriptions_enabled: number; subscriptions_total: number;
};
type Subscription = { id: string; endpoint_url: string; event_types: string[]; enabled: boolean; created_at: string };
type Received = {
  id: number; received_at: string; event_id: string | null; event_type: string | null;
  outcome: 'accepted' | 'duplicate' | 'rejected' | 'simulated_failure'; http_status: number;
  signature_valid: boolean | null; reason: string | null; headers: Record<string, string>;
  payload: Record<string, unknown> | null;
};
type ReceiverData = { counts: Record<string, number>; events: Received[] };
type Mode = 'ok' | 'fail' | 'fail_next' | 'slow' | 'reject';
type Control = { mode: Mode; fail_count: number; delay_seconds: number; failures_left: number };

const MODES: { mode: Mode; label: string; hint: string }[] = [
  { mode: 'ok', label: 'Healthy', hint: 'Answers 200' },
  { mode: 'fail_next', label: 'Fail next 2', hint: '500 twice, then recovers: the sender succeeds on attempt 3' },
  { mode: 'fail', label: 'Down (500)', hint: 'Every request fails: the sender gives up after 3 attempts' },
  { mode: 'slow', label: 'Slow (8 s)', hint: 'Sender gives up after 5 s on every try (failed), yet the receiver processed the first: the rest are duplicates' },
  { mode: 'reject', label: 'Reject (400)', hint: 'Permanent error: the sender does not retry' },
];

function tokenExp(token: string): number {
  try {
    return JSON.parse(atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/'))).exp || 0;
  } catch {
    return 0;
  }
}

const time = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const short = (id: string | null) => (id ? `${id.slice(0, 12)}…` : '—');

function Pill({ value }: { value: string }) {
  const label = value.replace(/_/g, ' ');
  return <span className={`pill pill-${value}`}>{label}</span>;
}

export default function Webhooks() {
  const [token, setToken] = useState(localStorage.getItem('admin_token') || '');
  const [live, setLive] = useState(true);
  const [stats, setStats] = useState<Stats | null>(null);
  const [events, setEvents] = useState<EventRow[]>([]);
  const [subs, setSubs] = useState<Subscription[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<EventDetail | null>(null);
  const [receiver, setReceiver] = useState<ReceiverData | null>(null);
  const [control, setControl] = useState<Control | null>(null);
  const [platformError, setPlatformError] = useState('');
  const [receiverError, setReceiverError] = useState('');
  const [notice, setNotice] = useState('');
  const [newUrl, setNewUrl] = useState('http://webhook-receiver:8200/webhooks/receive');
  const [newTypes, setNewTypes] = useState<string[]>([...EVENT_TYPES]);
  const signedIn = !!token && tokenExp(token) * 1000 > Date.now();

  const platform = useCallback(async <T,>(path: string, init: RequestInit = {}): Promise<T> => {
    const r = await fetch(`${PLATFORM}${path}`, {
      ...init,
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json', ...(init.headers || {}) },
    });
    const data = await r.json().catch(() => ({}));
    if (r.status === 401) {
      localStorage.removeItem('admin_token');
      setToken('');
    }
    if (!r.ok) throw new Error(data.detail || data.title || `Platform API returned ${r.status}`);
    return data as T;
  }, [token]);

  const receiverCall = useCallback(async <T,>(path: string, init: RequestInit = {}): Promise<T> => {
    const r = await fetch(`${RECEIVER}${path}`, { ...init, headers: { 'Content-Type': 'application/json' } });
    if (!r.ok) throw new Error(`Receiver returned ${r.status}`);
    return r.json() as Promise<T>;
  }, []);

  const refresh = useCallback(async () => {
    if (signedIn) {
      try {
        const [s, e, sb] = await Promise.all([
          platform<Stats>('/v1/webhooks/stats'),
          platform<EventRow[]>('/v1/webhooks/events?limit=30'),
          platform<Subscription[]>('/v1/webhooks/subscriptions'),
        ]);
        setStats(s); setEvents(e); setSubs(sb); setPlatformError('');
      } catch (err) {
        setPlatformError(err instanceof Error ? err.message : `Cannot reach the platform API at ${PLATFORM}`);
      }
    }
    try {
      const [rv, ct] = await Promise.all([
        receiverCall<ReceiverData>('/events?limit=40'), receiverCall<Control>('/control'),
      ]);
      setReceiver(rv); setControl(ct); setReceiverError('');
    } catch {
      setReceiverError(`Cannot reach the webhook receiver at ${RECEIVER}. Is the webhook-receiver container running?`);
    }
  }, [platform, receiverCall, signedIn]);

  const loadDetail = useCallback(async (id: string) => {
    try {
      setDetail(await platform<EventDetail>(`/v1/webhooks/events/${id}`));
    } catch (err) {
      setNotice(err instanceof Error ? err.message : 'Could not load the event');
    }
  }, [platform]);

  useEffect(() => {
    void refresh();
    if (!live) return undefined;
    const timer = window.setInterval(() => { void refresh(); }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [live, refresh]);

  useEffect(() => {
    if (!selected || !signedIn) {
      setDetail(null);
      return undefined;
    }
    void loadDetail(selected);
    if (!live) return undefined;
    const timer = window.setInterval(() => { void loadDetail(selected); }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [selected, live, signedIn, loadDetail]);

  async function signIn() {
    setPlatformError('');
    try {
      const r = await fetch(`${CHAT_API}/api/dev-token`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ persona: 'platform_admin' }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || `Request failed (${r.status})`);
      localStorage.setItem('admin_token', data.access_token);
      setToken(data.access_token);
    } catch (err) {
      setPlatformError(err instanceof Error ? err.message : 'Could not get an admin token');
    }
  }

  async function act(label: string, fn: () => Promise<unknown>) {
    setNotice('');
    try {
      await fn();
      setNotice(label);
      await refresh();
      if (selected) await loadDetail(selected);
    } catch (err) {
      setNotice(err instanceof Error ? err.message : 'Action failed');
    }
  }

  const sendTest = (subscriptionId?: string) => act('Test event queued.', async () => {
    const res = await platform<{ event_id: string }>('/v1/webhooks/test', {
      method: 'POST', body: JSON.stringify(subscriptionId ? { subscription_id: subscriptionId } : {}),
    });
    setSelected(res.event_id);
  });
  const retry = (deliveryId: number) => act(`Delivery ${deliveryId} queued for retry.`, () => platform(
    `/v1/webhooks/deliveries/${deliveryId}/retry`, { method: 'POST' }));
  const toggleSub = (s: Subscription) => act(`Subscription ${s.enabled ? 'disabled' : 'enabled'}.`, () => platform(
    `/v1/webhooks/subscriptions/${s.id}`, { method: 'PATCH', body: JSON.stringify({ enabled: !s.enabled }) }));
  const setMode = (mode: Mode) => act(`Receiver mode: ${MODES.find((m) => m.mode === mode)?.label}.`, () => receiverCall(
    '/control', { method: 'PUT', body: JSON.stringify({ mode, fail_count: 2, delay_seconds: 8 }) }));
  const clearReceived = () => act('Receiver history cleared.', () => receiverCall('/events', { method: 'DELETE' }));

  function addSubscription(e: FormEvent) {
    e.preventDefault();
    void act('Subscription created.', () => platform('/v1/webhooks/subscriptions', {
      method: 'POST', body: JSON.stringify({ endpoint_url: newUrl.trim(), event_types: newTypes }),
    }));
  }

  const counts = receiver?.counts || {};

  return (
    <section className="wh-page">
      <div className="wh-head">
        <div>
          <h1><Webhook size={22} /> Webhooks</h1>
          <p>Signed events the platform sends, and what the receiving system got.</p>
        </div>
        <div className="wh-actions">
          <button className={`wh-live ${live ? 'on' : ''}`} onClick={() => setLive(!live)} title="Refresh every 3 seconds">
            <Radio size={14} /> {live ? 'Live' : 'Paused'}
          </button>
          <button className="secondary" onClick={() => { void refresh(); }}><RefreshCw size={14} /> Refresh</button>
          <button className="primary" disabled={!signedIn} onClick={() => { void sendTest(); }}>
            <Send size={14} /> Send test event
          </button>
        </div>
      </div>

      <div className="wh-flow">
        <span><b>Chat / API</b> escalate or comment</span><ArrowRight size={15} />
        <span><b>Platform API :8000</b> outbox, HMAC-SHA256 signature, retries</span><ArrowRight size={15} />
        <span><b>webhook-receiver :8200</b> verify, de-duplicate, store</span>
      </div>

      {!signedIn && (
        <div className="wh-signin">
          <ShieldCheck size={18} />
          <div><b>The dashboard needs the platform_admin persona</b> (scope <code>webhooks:manage</code>). Chat keeps its own
            sign-in.</div>
          <button className="primary" onClick={() => { void signIn(); }}><LogIn size={14} /> Sign in as platform_admin</button>
        </div>
      )}
      {platformError && <div className="error">{platformError}</div>}
      {notice && <div className="wh-notice" onClick={() => setNotice('')}>{notice}</div>}

      <div className="wh-stats">
        <div className="stat"><span>Events</span><b>{stats?.events_total ?? '—'}</b></div>
        <div className="stat ok"><span>Delivered</span><b>{stats?.deliveries.delivered ?? '—'}</b></div>
        <div className="stat bad"><span>Failed</span><b>{stats?.deliveries.failed ?? '—'}</b></div>
        <div className="stat warn"><span>Pending</span><b>{stats?.deliveries.pending ?? '—'}</b></div>
        <div className="stat"><span>HTTP attempts</span><b>{stats ? `${stats.attempts_total}` : '—'}</b>
          {stats && <small>{stats.failed_attempts} failed</small>}</div>
      </div>

      <div className="wh-grid">
        <div className="wh-card">
          <div className="wh-card-head"><h2>Sent by the platform</h2><small>newest first · click for details</small></div>
          <div className="table-wrap">
            <table className="wh-table">
              <thead><tr><th>Event</th><th>Resource</th><th>Time</th><th>Status</th><th>HTTP</th><th>Tries</th></tr></thead>
              <tbody>
                {events.length === 0 && (
                  <tr><td colSpan={6} className="empty">No events yet. Escalate a case or add a comment in Chat, or send a test event.</td></tr>
                )}
                {events.map((ev) => {
                  const d = ev.deliveries[0];
                  return (
                    <tr key={ev.id} className={selected === ev.id ? 'sel' : ''} onClick={() => setSelected(ev.id)}>
                      <td><code className="evt">{ev.event_type}</code></td>
                      <td>{ev.resource_id}</td>
                      <td>{time(ev.created_at)}</td>
                      <td><Pill value={ev.status} /></td>
                      <td>{d ? (d.last_status_code ?? d.last_error ?? '—') : '—'}</td>
                      <td>{ev.deliveries.reduce((n, x) => n + x.attempts, 0) || '—'}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>

        <div className="wh-card">
          <div className="wh-card-head">
            <h2>Received by webhook-receiver</h2>
            <button className="link" onClick={() => { void clearReceived(); }}><Trash2 size={13} /> Clear</button>
          </div>
          {receiverError ? <div className="error">{receiverError}</div> : (
            <>
              <div className="wh-modes">
                {MODES.map((m) => (
                  <button key={m.mode} title={m.hint} className={control?.mode === m.mode ? 'active' : ''}
                    onClick={() => { void setMode(m.mode); }}>{m.label}</button>
                ))}
              </div>
              <div className="wh-hint">
                {MODES.find((m) => m.mode === control?.mode)?.hint}
                {control?.mode === 'fail_next' && ` · ${control.failures_left} failure(s) left`}
              </div>
              <div className="wh-counts">
                <span><CircleCheck size={13} /> {counts.accepted ?? 0} accepted</span>
                <span><RotateCcw size={13} /> {counts.duplicate ?? 0} duplicate</span>
                <span><ShieldX size={13} /> {counts.rejected ?? 0} rejected</span>
                <span><CircleX size={13} /> {counts.simulated_failure ?? 0} failed</span>
              </div>
              <ul className="wh-received">
                {(receiver?.events || []).length === 0 && <li className="empty">Nothing received yet.</li>}
                {(receiver?.events || []).map((r) => (
                  <li key={r.id} onClick={() => r.event_id && setSelected(r.event_id)}>
                    <div className="row1">
                      <Pill value={r.outcome} />
                      <code className="evt">{r.event_type || 'unknown'}</code>
                      <span className="http">{r.http_status}</span>
                      <span className="when">{time(r.received_at)}</span>
                    </div>
                    <div className="row2">
                      {r.signature_valid === true && <span className="sig ok"><ShieldCheck size={12} /> signature valid</span>}
                      {r.signature_valid === false && <span className="sig bad"><ShieldX size={12} /> signature invalid</span>}
                      {r.signature_valid === null && <span className="sig">signature not checked</span>}
                      <span className="mono">{short(r.event_id)}</span>
                      {r.reason && <span className="reason">{r.reason}</span>}
                    </div>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      </div>

      {detail && (
        <div className="wh-card wh-detail">
          <div className="wh-card-head">
            <h2><code className="evt">{detail.event_type}</code> {detail.resource_id} <Pill value={detail.status} /></h2>
            <button className="link" onClick={() => setSelected(null)}>Close</button>
          </div>
          <div className="wh-detail-grid">
            <div>
              <h3>Payload (exactly what is signed and sent)</h3>
              <pre>{JSON.stringify(detail.payload, null, 2)}</pre>
            </div>
            <div>
              {detail.deliveries.length === 0 && <p className="empty">No subscription takes this event type, so nothing was sent.</p>}
              {detail.deliveries.map((d) => (
                <div key={d.id} className="wh-delivery">
                  <div className="wh-delivery-head">
                    <div><h3>Delivery #{d.id} → {d.endpoint_url}</h3><small>{d.subscription_id}</small></div>
                    <Pill value={d.status} />
                    {d.status === 'failed' && (
                      <button className="primary small" onClick={() => { void retry(d.id); }}><RotateCcw size={13} /> Retry</button>
                    )}
                  </div>
                  <ol className="wh-timeline">
                    {d.attempt_log.map((a) => {
                      const ok = a.status_code !== null && a.status_code < 300;
                      return (
                        <li key={a.attempt_no} className={ok ? 'ok' : 'bad'}>
                          <div className="row1">
                            {ok ? <CircleCheck size={14} /> : <CircleX size={14} />}
                            <b>Attempt {a.attempt_no}</b>
                            <span>{a.status_code !== null ? `HTTP ${a.status_code}` : a.error}</span>
                            <span><Clock size={12} /> {a.duration_ms} ms</span>
                            <span className="when">{time(a.attempted_at)}</span>
                            {a.trigger !== 'auto' && <span className="pill pill-trigger">{a.trigger.replace('_', ' ')}</span>}
                          </div>
                          <div className="mono small" title={a.request_headers['Webhook-Signature']}>
                            Webhook-Timestamp: {a.request_headers['Webhook-Timestamp']} · Webhook-Signature:{' '}
                            {(a.request_headers['Webhook-Signature'] || '').slice(0, 30)}…
                          </div>
                          {a.response_body && <div className="mono small">Response: {a.response_body.slice(0, 160)}</div>}
                        </li>
                      );
                    })}
                    {d.status === 'pending' && <li className="pending"><Clock size={14} /> waiting for the next attempt…</li>}
                  </ol>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      <div className="wh-card">
        <div className="wh-card-head"><h2>Subscriptions</h2><small>{stats ? `${stats.subscriptions_enabled} of ${stats.subscriptions_total} enabled` : ''}</small></div>
        <div className="table-wrap">
          <table className="wh-table">
            <thead><tr><th>Endpoint</th><th>Events</th><th>Enabled</th><th /></tr></thead>
            <tbody>
              {subs.map((s) => (
                <tr key={s.id}>
                  <td><div className="mono">{s.endpoint_url}</div><small>{s.id}</small></td>
                  <td>{s.event_types.map((t) => <code key={t} className="evt">{t}</code>)}</td>
                  <td>
                    <button className={`switch ${s.enabled ? 'on' : ''}`} onClick={() => { void toggleSub(s); }}
                      aria-label={s.enabled ? 'Disable' : 'Enable'}><span /></button>
                  </td>
                  <td><button className="link" disabled={!s.enabled} onClick={() => { void sendTest(s.id); }}><Send size={13} /> Test</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <form className="wh-add" onSubmit={addSubscription}>
          <input value={newUrl} onChange={(e) => setNewUrl(e.target.value)} placeholder="https://your-system.example/hooks" />
          {EVENT_TYPES.map((t) => (
            <label key={t}>
              <input type="checkbox" checked={newTypes.includes(t)}
                onChange={(e) => setNewTypes(e.target.checked ? [...newTypes, t] : newTypes.filter((x) => x !== t))} />
              {t}
            </label>
          ))}
          <button className="primary" disabled={!signedIn || !newUrl.trim() || newTypes.length === 0}><Plus size={14} /> Add</button>
        </form>
      </div>
    </section>
  );
}
