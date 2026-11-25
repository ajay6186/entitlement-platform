import { FormEvent, useCallback, useEffect, useRef, useState } from 'react';
import {
  Bell, BellOff, Bot, Check, LoaderCircle, MessageSquare, Plus, Send, Settings, ShieldAlert, ShieldCheck, User,
  Webhook, X,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import Notifications from './Notifications';
import Webhooks from './Webhooks';

type ToolCall = { tool: string; is_error: boolean };
type PendingAction = { id: string; tool: string; preview: Record<string, unknown>; expires_in_seconds: number };
type Msg = {
  role: 'user' | 'assistant';
  content: string;
  tools?: ToolCall[];
  pending?: PendingAction;
  decided?: 'approve' | 'reject';
};

const API = import.meta.env.VITE_CHAT_API_URL || 'http://localhost:8100';
// Product naming, in one place.
export const APP_NAME = 'Customer Support Platform';
const APP_TAGLINE = 'Entitlements & Case Management';
const ASSISTANT_NAME = 'Support Assistant';
const PERSONAS = ['support_agent', 'support_lead', 'finance_analyst', 'platform_admin'] as const;
const GREETING: Msg = {
  role: 'assistant',
  content: `Hello! I am your ${ASSISTANT_NAME}. I can search support cases, summarize customer entitlements, `
    + 'add comments and help escalate cases. What would you like to investigate?',
};

function tokenInfo(token: string): { sub?: string; scope?: string; exp?: number } {
  try {
    return JSON.parse(atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
  } catch {
    return {};
  }
}

export default function App() {
  const [token, setToken] = useState(localStorage.getItem('platform_token') || '');
  const [showSettings, setShowSettings] = useState(!localStorage.getItem('platform_token'));
  const [draftToken, setDraftToken] = useState(token);
  const [persona, setPersona] = useState<(typeof PERSONAS)[number]>('support_lead');
  const [messages, setMessages] = useState<Msg[]>([GREETING]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [view, setView] = useState<'chat' | 'webhooks'>(location.hash === '#webhooks' ? 'webhooks' : 'chat');
  const [desktopAlerts, setDesktopAlerts] = useState(localStorage.getItem('desktop_alerts') === '1');
  const [liveNotifications, setLiveNotifications] = useState(false);
  const onNotifyStatus = useCallback((connected: boolean) => setLiveNotifications(connected), []);
  const bottom = useRef<HTMLDivElement>(null);
  const who = tokenInfo(token);

  // Braces matter: newer browsers return a Promise from scrollIntoView, and React would call it as the cleanup.
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, busy]);

  async function call(path: string, body: unknown) {
    const r = await fetch(`${API}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `Request failed (${r.status})`);
    return data;
  }

  function saveToken(value = draftToken) {
    const t = value.trim();
    localStorage.setItem('platform_token', t);
    setToken(t);
    setDraftToken(t);
    setShowSettings(false);
    setError('');
  }

  async function getDevToken() {
    setError('');
    try {
      const r = await fetch(`${API}/api/dev-token`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ persona }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || `Request failed (${r.status})`);
      saveToken(data.access_token);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not get a token');
    }
  }

  function go(next: 'chat' | 'webhooks') {
    setView(next);
    history.replaceState(null, '', next === 'webhooks' ? '#webhooks' : '#');
  }

  async function toggleDesktopAlerts() {
    if (desktopAlerts) {
      setDesktopAlerts(false);
      localStorage.setItem('desktop_alerts', '0');
      return;
    }
    if (!('Notification' in window)) {
      setError('This browser does not support desktop notifications.');
      return;
    }
    const permission = Notification.permission === 'default' ? await Notification.requestPermission() : Notification.permission;
    if (permission !== 'granted') {
      setError('Desktop notifications are blocked for this site. Allow them in the browser\'s site settings.');
      return;
    }
    setDesktopAlerts(true);
    localStorage.setItem('desktop_alerts', '1');
  }

  function newChat() {
    go('chat');
    setMessages([{ role: 'assistant', content: 'New conversation started. What can I help you with?' }]);
    setError('');
  }

  async function send(e?: FormEvent) {
    e?.preventDefault();
    const text = input.trim();
    if (!text || busy) return;
    if (!token) {
      setShowSettings(true);
      setError('Sign in with a platform token in Settings first.');
      return;
    }
    const prior = messages;
    setMessages([...prior, { role: 'user', content: text }]);
    setInput('');
    setBusy(true);
    setError('');
    try {
      const data = await call('/api/chat', {
        message: text,
        history: prior.slice(-16).map((m) => ({ role: m.role, content: m.content })),
      });
      setMessages((old) => [...old, {
        role: 'assistant', content: data.reply, tools: data.tool_calls || [], pending: data.pending_action || undefined,
      }]);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unexpected error');
    } finally {
      setBusy(false);
    }
  }

  async function decide(index: number, action: PendingAction, decision: 'approve' | 'reject') {
    setBusy(true);
    setError('');
    setMessages((old) => old.map((m, i) => (i === index ? { ...m, decided: decision } : m)));
    try {
      const data = await call(`/api/actions/${action.id}`, { decision });
      setMessages((old) => [...old, { role: 'assistant', content: data.reply }]);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unexpected error');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-icon"><Bot /></div>
          <div><b>{APP_NAME}</b><small>{APP_TAGLINE}</small></div>
        </div>
        <button className="newchat" onClick={newChat}><Plus size={17} /> New chat</button>
        <div className="side-label">WORKSPACE</div>
        <button className={`side-item ${view === 'chat' ? 'active' : ''}`} onClick={() => go('chat')}>
          <MessageSquare size={16} /> Customer support
        </button>
        <button className={`side-item ${view === 'webhooks' ? 'active' : ''}`} onClick={() => go('webhooks')}>
          <Webhook size={16} /> Webhooks
        </button>
        <div className="side-footer">
          <div className="secure"><ShieldCheck size={16} /> {who.sub ? `Signed in as ${who.sub}` : 'Not signed in'}</div>
          <button className="settings-btn" onClick={() => { setDraftToken(token); setShowSettings(true); }}>
            <Settings size={17} /> API connection settings
          </button>
        </div>
      </aside>

      <main className="main">
        <header>
          <div className="mobile-brand"><Bot /> {APP_NAME}</div>
          <div className="view-tabs">
            <button className={view === 'chat' ? 'active' : ''} onClick={() => go('chat')}><MessageSquare size={14} /> Chat</button>
            <button className={view === 'webhooks' ? 'active' : ''} onClick={() => go('webhooks')}><Webhook size={14} /> Webhooks</button>
          </div>
          <div className="header-meta"><span className="dot" /> Local development</div>
          <button className={`icon-btn bell ${desktopAlerts ? 'on' : ''}`} onClick={() => { void toggleDesktopAlerts(); }}
            title={`${liveNotifications ? 'Webhook notifications: connected' : 'Webhook notifications: receiver not reachable'}`
              + ` · desktop alerts ${desktopAlerts ? 'on (click to turn off)' : 'off (click to turn on)'}`}>
            {desktopAlerts ? <Bell size={18} /> : <BellOff size={18} />}
            <span className={`live-dot ${liveNotifications ? 'on' : ''}`} />
          </button>
          <button className="icon-btn" title="Settings" onClick={() => { setDraftToken(token); setShowSettings(true); }}>
            <Settings size={19} />
          </button>
        </header>

        {view === 'webhooks' ? <Webhooks /> : (<>
        <section className="conversation">
          {messages.length === 1 && (
            <div className="welcome">
              <div className="welcome-icon"><Bot size={30} /></div>
              <h1>How can I help you today?</h1>
              <p>Ask questions about customers, entitlements and support cases.</p>
              <div className="suggestions">
                <button onClick={() => setInput('Find Platinum customers with open P1 cases.')}>Find Platinum P1 cases</button>
                <button onClick={() => setInput('Show me the customer summary for ACC-007.')}>Summarize a customer</button>
                <button onClick={() => setInput('Which Platinum customers have P1 cases open for more than 48 hours and a renewal in the next 90 days?')}>
                  Identify renewal risks
                </button>
              </div>
            </div>
          )}

          {messages.map((m, i) => (
            <div className={`message ${m.role}`} key={i}>
              <div className="avatar">{m.role === 'assistant' ? <Bot size={18} /> : <User size={18} />}</div>
              <div className="message-body">
                <div className="role-name">{m.role === 'assistant' ? ASSISTANT_NAME : 'You'}</div>
                {m.role === 'assistant' ? (
                  // Markdown (GFM tables) from the model. Raw HTML is not rendered, so untrusted case text stays text.
                  <div className="message-text markdown">
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm]}
                      components={{
                        table: ({ children }) => <div className="table-wrap"><table>{children}</table></div>,
                        a: ({ href, children }) => <a href={href} target="_blank" rel="noreferrer noopener">{children}</a>,
                      }}
                    >
                      {m.content}
                    </ReactMarkdown>
                  </div>
                ) : (
                  <div className="message-text">{m.content}</div>
                )}
                {!!m.tools?.length && (
                  <div className="tool-meta"><ShieldCheck size={13} /> Tools used: {m.tools.map((t) => t.tool).join(', ')}</div>
                )}
                {m.pending && (
                  <div className="approval">
                    <div className="approval-title">
                      <ShieldAlert size={16} />
                      {m.pending.tool === 'escalate_case' ? 'Escalate this case?' : 'Add this comment?'}
                    </div>
                    <dl>
                      {Object.entries(m.pending.preview).filter(([k]) => k !== 'action').map(([k, v]) => (
                        <div key={k}><dt>{k.replace(/_/g, ' ')}</dt><dd>{String(v)}</dd></div>
                      ))}
                    </dl>
                    {m.decided ? (
                      <div className="approval-done">{m.decided === 'approve' ? 'Approved' : 'Rejected'}</div>
                    ) : (
                      <div className="approval-actions">
                        <button className="secondary" disabled={busy} onClick={() => decide(i, m.pending!, 'reject')}>
                          <X size={15} /> Reject
                        </button>
                        <button className="primary" disabled={busy} onClick={() => decide(i, m.pending!, 'approve')}>
                          <Check size={15} /> Approve
                        </button>
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          ))}

          {busy && (
            <div className="message assistant">
              <div className="avatar"><Bot size={18} /></div>
              <div className="message-body">
                <div className="role-name">{ASSISTANT_NAME}</div>
                <div className="typing"><LoaderCircle className="spin" size={16} /> Thinking and checking tools…</div>
              </div>
            </div>
          )}
          {error && <div className="error">{error}</div>}
          <div ref={bottom} />
        </section>

        <form className="composer-wrap" onSubmit={send}>
          <div className="composer">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder={`Message ${ASSISTANT_NAME}…`}
              rows={1}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send(); } }}
            />
            <button className="send" type="submit" disabled={!input.trim() || busy}><Send size={17} /></button>
          </div>
          <div className="disclaimer"><ShieldCheck size={12} /> Write actions always wait for your approval.</div>
        </form>
        </>)}
      </main>

      <Notifications desktop={desktopAlerts} onOpen={() => go('webhooks')} onStatus={onNotifyStatus} />

      {showSettings && (
        <div className="modal-backdrop">
          <div className="modal">
            <div className="modal-title">
              <h2>Connect to your platform</h2>
              <button className="icon-btn" onClick={() => setShowSettings(false)}>×</button>
            </div>
            <p>Sign in as a local development persona, or paste a bearer token from <code>POST /dev/token</code>.
              The token is stored in this browser only and limits what the assistant can see and do.</p>
            <label>Quick sign-in (local development)</label>
            <div className="persona-row">
              <select value={persona} onChange={(e) => setPersona(e.target.value as (typeof PERSONAS)[number])}>
                {PERSONAS.map((p) => <option key={p} value={p}>{p}</option>)}
              </select>
              <button className="primary" onClick={getDevToken}>Get token</button>
            </div>
            <label>Platform API bearer token</label>
            <textarea className="token-input" value={draftToken} onChange={(e) => setDraftToken(e.target.value)} placeholder="eyJ..." rows={4} />
            {who.scope && <small>Current scopes: {who.scope}</small>}
            <div className="modal-actions">
              <button className="secondary" onClick={() => setShowSettings(false)}>Cancel</button>
              <button className="primary" onClick={() => saveToken()} disabled={!draftToken.trim()}>Save connection</button>
            </div>
            {error && <div className="error">{error}</div>}
            <small>Never paste an Anthropic API key here. It belongs only in the chat backend environment.</small>
          </div>
        </div>
      )}
    </div>
  );
}
