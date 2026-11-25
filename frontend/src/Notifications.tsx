import { useEffect, useState } from 'react';
import { CircleAlert, CircleCheck, CircleX, Copy, ShieldCheck, ShieldX, Webhook, X } from 'lucide-react';

const RECEIVER = import.meta.env.VITE_RECEIVER_URL || 'http://localhost:8200';
const MAX_TOASTS = 4;

type Received = {
  id: number; received_at: string; event_id: string | null; event_type: string | null;
  outcome: 'accepted' | 'duplicate' | 'rejected' | 'simulated_failure'; http_status: number;
  signature_valid: boolean | null; reason: string | null; payload: Record<string, unknown> | null;
};
type Toast = { key: number; item: Received };

const OUTCOME = {
  accepted: { label: 'Webhook received', kind: 'ok', ms: 7000 },
  duplicate: { label: 'Duplicate ignored', kind: 'info', ms: 7000 },
  rejected: { label: 'Webhook rejected', kind: 'bad', ms: 10000 },
  simulated_failure: { label: 'Receiver failed', kind: 'warn', ms: 9000 },
} as const;

/** Human wording for a received webhook: what happened in the business, plus what the receiver did. */
export function describe(item: Received): { title: string; body: string } {
  const p = item.payload || {};
  const target = String(p.case_id ?? '');
  let title = 'Webhook';
  let body = '';
  if (item.event_type === 'case.escalated') {
    title = `Case ${target} escalated`;
    body = [p.priority, p.customer_id, p.reason].filter(Boolean).join(' · ');
  } else if (item.event_type === 'case.comment_added') {
    title = `New comment on ${target}`;
    body = `Comment ${String(p.comment_id ?? '')} by ${String(p.author_id ?? 'unknown')}`;
  } else if (item.event_type === 'webhook.test') {
    title = 'Test webhook';
    body = String(p.message ?? '');
  } else if (item.event_type) {
    title = item.event_type;
  }
  if (item.outcome === 'duplicate') body = 'Already processed: acknowledged, not processed again.';
  if (item.outcome === 'rejected') body = item.reason || 'Rejected by the receiver.';
  if (item.outcome === 'simulated_failure') body = 'Receiver answered 500. The platform will retry automatically.';
  return { title, body };
}

function Icon({ outcome }: { outcome: Received['outcome'] }) {
  if (outcome === 'accepted') return <CircleCheck size={18} />;
  if (outcome === 'duplicate') return <Copy size={18} />;
  if (outcome === 'rejected') return <CircleX size={18} />;
  return <CircleAlert size={18} />;
}

export default function Notifications({ desktop, onOpen, onStatus }: {
  desktop: boolean; onOpen: () => void; onStatus: (connected: boolean) => void;
}) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  useEffect(() => {
    // Server-Sent Events: the receiver pushes each webhook the moment it arrives (EventSource reconnects by itself).
    const source = new EventSource(`${RECEIVER}/events/stream`);
    source.onopen = () => onStatus(true);
    source.onerror = () => onStatus(false);
    source.addEventListener('webhook', (e) => {
      const item = JSON.parse((e as MessageEvent).data) as Received;
      const key = item.id;
      setToasts((old) => [{ key, item }, ...old.filter((t) => t.key !== key)].slice(0, MAX_TOASTS));
      window.setTimeout(() => setToasts((old) => old.filter((t) => t.key !== key)), OUTCOME[item.outcome].ms);
      if (desktop && 'Notification' in window && Notification.permission === 'granted'
          && (document.hidden || !document.hasFocus())) {
        const { title, body } = describe(item);
        new Notification(`${OUTCOME[item.outcome].label}: ${title}`, { body, tag: `webhook-${key}` });
      }
    });
    return () => {
      source.close();
      onStatus(false);
    };
  }, [desktop, onStatus]);

  return (
    <div className="toasts" aria-live="polite">
      {toasts.map(({ key, item }) => {
        const meta = OUTCOME[item.outcome];
        const { title, body } = describe(item);
        return (
          <div key={key} className={`toast toast-${meta.kind}`} role="status" onClick={onOpen}
            title="Open the Webhooks dashboard">
            <div className="toast-icon"><Icon outcome={item.outcome} /></div>
            <div className="toast-main">
              <div className="toast-top">
                <Webhook size={11} /> {meta.label} · HTTP {item.http_status}
                {item.signature_valid === true && <span className="sig ok"><ShieldCheck size={11} /> signed</span>}
                {item.signature_valid === false && <span className="sig bad"><ShieldX size={11} /> bad signature</span>}
              </div>
              <div className="toast-title">{title}</div>
              {body && <div className="toast-body">{body}</div>}
            </div>
            <button className="toast-close" aria-label="Dismiss"
              onClick={(e) => { e.stopPropagation(); setToasts((old) => old.filter((t) => t.key !== key)); }}>
              <X size={14} />
            </button>
          </div>
        );
      })}
    </div>
  );
}
