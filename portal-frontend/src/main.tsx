import React, { useCallback, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  AlertTriangle,
  CalendarClock,
  ChevronRight,
  CircleAlert,
  Database,
  LayoutDashboard,
  LoaderCircle,
  MessageCircle,
  MessagesSquare,
  Pill,
  RefreshCw,
  Search,
  Send,
  ShieldCheck,
  UserRound,
  Users,
  X,
} from "lucide-react";
import "./styles.css";

const API = "http://127.0.0.1:8788";

type Prescription = {
  prescription_id: string;
  drug_name: string;
  condition: string;
  next_checkin_date: string | null;
};

type Patient = {
  patient_id: string;
  first_name: string;
  last_name: string;
  status: string;
  telegram_chat_id: string | null;
  active_prescriptions: Prescription[];
  last_interaction: { outcome: string; created_at: string } | null;
  open_escalation_count: number;
};

type Chat = {
  patient_id: string;
  chat_id: string;
  name: string;
  last_outcome: string | null;
  last_message_at: string | null;
  message_count: number;
  status: string;
};

type Message = { from: "bot" | "customer"; text: string; time: string };

type Escalation = {
  escalation_id: string;
  patient_id: string;
  prescription_id: string;
  patient_name: string;
  drug_name: string;
  red_flag_type: string;
  severity: "low" | "medium" | "high" | "critical";
  reason: string;
  raw_patient_text: string;
  status: string;
  notified: boolean;
  assigned_to: string | null;
  created_at: string;
};

async function getMongo<T>(path: string): Promise<T> {
  const response = await fetch(`${API}${path}`, { cache: "no-store" });
  const payload = await response.json().catch(() => null);
  if (!response.ok) throw new Error(payload?.error || `Request failed (${response.status})`);
  if (payload?.source !== "mongodb") throw new Error("Portal refused a non-MongoDB response");
  return payload as T;
}

function formatDate(value: string | null | undefined, withTime = false) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString([], withTime
    ? { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }
    : { month: "short", day: "numeric", year: "numeric" });
}

function dueNow(patient: Patient) {
  const now = new Date();
  return patient.active_prescriptions.some((rx) => {
    if (!rx.next_checkin_date) return false;
    const due = new Date(rx.next_checkin_date);
    return !Number.isNaN(due.getTime()) && due <= now;
  });
}

function StatusPill({ children, tone = "neutral" }: { children: React.ReactNode; tone?: string }) {
  return <span className={`pill pill-${tone}`}>{children}</span>;
}

function chatStatusTone(status: string) {
  if (status === "Escalated") return "danger";
  if (status === "Verification Failed" || status === "Awaiting Reply") return "warning";
  if (status === "Conversation Active") return "live";
  return "neutral";
}

function Kpi({ label, value, hint, icon: Icon, alert = false }: {
  label: string; value: number; hint: string; icon: React.ComponentType<{ size?: number }>; alert?: boolean;
}) {
  return (
    <article className={`kpi ${alert ? "kpi-alert" : ""}`}>
      <div className="kpi-top"><span>{label}</span><Icon size={17} /></div>
      <strong>{value}</strong>
      <p>{hint}</p>
    </article>
  );
}

function ChatDrawer({ chat, patient, onClose }: {
  chat: Chat; patient?: Patient; onClose: () => void;
}) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const result = await getMongo<{ source: "mongodb"; messages: Message[] }>(
        `/messages?chat_id=${encodeURIComponent(chat.chat_id)}`,
      );
      setMessages(result.messages);
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load chat");
    } finally {
      setLoading(false);
    }
  }, [chat.chat_id]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 8000);
    return () => window.clearInterval(timer);
  }, [load]);

  return (
    <div className="drawer-backdrop" onMouseDown={onClose}>
      <aside className="drawer" onMouseDown={(event) => event.stopPropagation()}>
        <header className="drawer-header">
          <div>
            <div className="eyebrow"><Activity size={13} /> Live Telegram conversation</div>
            <h2>{chat.name}</h2>
            <p>{patient?.active_prescriptions.map((rx) => rx.drug_name).join(" · ") || chat.patient_id}</p>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="Close chat"><X size={19} /></button>
        </header>
        <div className="drawer-meta">
          <StatusPill tone={chatStatusTone(chat.status)}>{chat.status}</StatusPill>
          <span>Telegram ID {chat.chat_id}</span>
          <span>{messages.length} messages</span>
        </div>
        <div className="messages">
          {loading && messages.length === 0 ? <div className="loading"><LoaderCircle className="spin" /> Loading transcript…</div> : null}
          {error ? <div className="error-inline">{error}</div> : null}
          {messages.map((message, index) => (
            <div key={`${message.time}-${index}`} className={`message-row ${message.from}`}>
              <div className="message-bubble">
                <p>{message.text}</p>
                <time>{message.time}</time>
              </div>
            </div>
          ))}
          {!loading && messages.length === 0 ? <div className="empty">No Telegram messages recorded.</div> : null}
        </div>
        <footer className="drawer-footer">
          <Database size={14} /> Read-only transcript synchronized to MongoDB every 8 seconds
        </footer>
      </aside>
    </div>
  );
}

function Dashboard({ patients, chats, loading, onRefresh }: {
  patients: Patient[]; chats: Chat[]; loading: boolean; onRefresh: () => void;
}) {
  const [query, setQuery] = useState("");
  const [openChat, setOpenChat] = useState<Chat | null>(null);
  const patientMap = useMemo(() => new Map(patients.map((patient) => [patient.patient_id, patient])), [patients]);
  const visiblePatients = useMemo(() => {
    const term = query.trim().toLowerCase();
    if (!term) return patients;
    return patients.filter((patient) => [
      patient.patient_id,
      patient.first_name,
      patient.last_name,
      ...patient.active_prescriptions.flatMap((rx) => [rx.drug_name, rx.condition]),
    ].some((value) => value.toLowerCase().includes(term)));
  }, [patients, query]);

  return (
    <>
      <header className="page-header">
        <div><div className="eyebrow"><Database size={13} /> MongoDB live</div><h1>Medication check-ins</h1><p>Active patients, prescriptions, outcomes, and Telegram conversations from RxLocal.</p></div>
        <button className="button secondary" onClick={onRefresh} disabled={loading}><RefreshCw className={loading ? "spin" : ""} size={16} /> Refresh</button>
      </header>

      <section className="kpi-grid">
        <Kpi label="Active patients" value={patients.length} hint="MongoDB patient records" icon={Users} />
        <Kpi label="Due now" value={patients.filter(dueNow).length} hint="Active prescriptions due" icon={CalendarClock} />
        <Kpi label="Telegram chats" value={chats.length} hint="Mapped active conversations" icon={MessageCircle} />
        <Kpi label="Open escalations" value={patients.reduce((sum, item) => sum + item.open_escalation_count, 0)} hint="Awaiting pharmacist review" icon={AlertTriangle} alert />
      </section>

      <section className="panel chats-panel">
        <div className="panel-heading">
          <div><h2><span className="live-dot" /> Active Telegram chats</h2><p>Current conversations synchronized into MongoDB.</p></div>
          <StatusPill tone="live">{chats.length} active</StatusPill>
        </div>
        {chats.length ? chats.map((chat) => {
          const patient = patientMap.get(chat.patient_id);
          return (
            <button className="chat-row" key={chat.chat_id} onClick={() => setOpenChat(chat)}>
              <span className="avatar"><UserRound size={19} /></span>
              <span className="chat-primary"><strong>{chat.name}</strong><small>{chat.patient_id} · {patient?.active_prescriptions.map((rx) => rx.drug_name).join(" · ") || "No active medication"}</small></span>
              <span className="chat-status"><StatusPill tone={chatStatusTone(chat.status)}>{chat.status}</StatusPill><small>{chat.message_count} messages · synced {formatDate(chat.last_message_at, true)}</small></span>
              <ChevronRight size={18} />
            </button>
          );
        }) : <div className="empty">No patients have an active Telegram chat mapping in MongoDB.</div>}
      </section>

      <section className="panel">
        <div className="panel-heading table-heading">
          <div><h2>Active patient records</h2><p>No frontend fixture rows are included.</p></div>
          <label className="search"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search patients or medications" /></label>
        </div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Patient</th><th>Active medication</th><th>Condition</th><th>Next check-in</th><th>Telegram</th><th>Last outcome</th><th>Escalations</th></tr></thead>
            <tbody>
              {visiblePatients.map((patient) => {
                const next = patient.active_prescriptions[0];
                return <tr key={patient.patient_id}>
                  <td><strong>{patient.first_name} {patient.last_name}</strong><small>{patient.patient_id}</small></td>
                  <td>{patient.active_prescriptions.map((rx) => <span className="stack" key={rx.prescription_id}>{rx.drug_name}<small>{rx.prescription_id}</small></span>)}</td>
                  <td>{[...new Set(patient.active_prescriptions.map((rx) => rx.condition))].join(", ") || "—"}</td>
                  <td>{formatDate(next?.next_checkin_date)}{dueNow(patient) ? <StatusPill tone="warning">Due</StatusPill> : null}</td>
                  <td>{patient.telegram_chat_id ? <StatusPill tone="live">Connected</StatusPill> : <StatusPill>Not connected</StatusPill>}</td>
                  <td>{patient.last_interaction ? <StatusPill tone={patient.last_interaction.outcome === "escalated" ? "danger" : "neutral"}>{patient.last_interaction.outcome.replace("_", " ")}</StatusPill> : "—"}</td>
                  <td>{patient.open_escalation_count ? <StatusPill tone="danger">{patient.open_escalation_count} open</StatusPill> : <span className="muted">None</span>}</td>
                </tr>;
              })}
            </tbody>
          </table>
          {!visiblePatients.length ? <div className="empty">No MongoDB patients match this search.</div> : null}
        </div>
      </section>
      {openChat ? <ChatDrawer chat={openChat} patient={patientMap.get(openChat.patient_id)} onClose={() => setOpenChat(null)} /> : null}
    </>
  );
}

function Messages({ escalations, loading, onRefresh }: { escalations: Escalation[]; loading: boolean; onRefresh: () => void }) {
  const critical = escalations.filter((item) => item.severity === "critical").length;
  const high = escalations.filter((item) => item.severity === "high").length;
  return (
    <>
      <header className="page-header">
        <div><div className="eyebrow"><ShieldCheck size={13} /> Pharmacist review queue</div><h1>Messages & escalations</h1><p>Open clinical concerns recorded by the medication check-in agent in MongoDB.</p></div>
        <button className="button secondary" onClick={onRefresh} disabled={loading}><RefreshCw className={loading ? "spin" : ""} size={16} /> Refresh</button>
      </header>
      <section className="kpi-grid three">
        <Kpi label="Open messages" value={escalations.length} hint="Open or acknowledged" icon={MessagesSquare} alert />
        <Kpi label="Critical" value={critical} hint="Immediate review queue" icon={CircleAlert} alert={critical > 0} />
        <Kpi label="High severity" value={high} hint="Priority pharmacist review" icon={AlertTriangle} alert={high > 0} />
      </section>
      <section className="panel inbox">
        <div className="panel-heading"><div><h2>Escalation inbox</h2><p>Patient words are shown verbatim; the portal does not generate summaries.</p></div><StatusPill tone={escalations.length ? "danger" : "live"}>{escalations.length} open</StatusPill></div>
        {escalations.length ? escalations.map((item) => (
          <article className="escalation" key={item.escalation_id}>
            <div className={`severity severity-${item.severity}`}><AlertTriangle size={18} /><span>{item.severity}</span></div>
            <div className="escalation-body">
              <div className="escalation-title"><div><h3>{item.patient_name}</h3><p>{item.patient_id} · {item.drug_name} · {item.prescription_id}</p></div><time>{formatDate(item.created_at, true)}</time></div>
              <blockquote>“{item.raw_patient_text}”</blockquote>
              <div className="escalation-details"><span><strong>Flag</strong>{item.red_flag_type.replace("_", " ")}</span><span><strong>Recorded reason</strong>{item.reason || "Not provided"}</span><span><strong>Assigned</strong>{item.assigned_to || "Unassigned"}</span><span><strong>Status</strong>{item.status}</span></div>
            </div>
          </article>
        )) : <div className="empty large"><ShieldCheck size={28} /><strong>No open escalations</strong><span>MongoDB has no open or acknowledged pharmacist review items.</span></div>}
      </section>
    </>
  );
}

function App() {
  const [tab, setTab] = useState<"dashboard" | "messages">("dashboard");
  const [patients, setPatients] = useState<Patient[]>([]);
  const [chats, setChats] = useState<Chat[]>([]);
  const [escalations, setEscalations] = useState<Escalation[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [patientResult, chatResult, escalationResult] = await Promise.all([
        getMongo<{ source: "mongodb"; patients: Patient[] }>("/patients"),
        getMongo<{ source: "mongodb"; chats: Chat[] }>("/chats"),
        getMongo<{ source: "mongodb"; escalations: Escalation[] }>("/escalations"),
      ]);
      setPatients(patientResult.patients);
      setChats(chatResult.chats);
      setEscalations(escalationResult.escalations);
      setLastRefresh(new Date());
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load MongoDB data");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 15000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand"><span><Pill size={21} /></span><div><strong>RxLocal</strong><small>Pharmacist portal</small></div></div>
        <nav>
          <button className={tab === "dashboard" ? "active" : ""} onClick={() => setTab("dashboard")}><LayoutDashboard size={18} /> Dashboard</button>
          <button className={tab === "messages" ? "active" : ""} onClick={() => setTab("messages")}><MessagesSquare size={18} /> Messages{escalations.length ? <b>{escalations.length}</b> : null}</button>
        </nav>
        <div className="source-card"><Database size={17} /><div><strong>MongoDB connected</strong><small>{lastRefresh ? `Updated ${lastRefresh.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : "Connecting…"}</small></div></div>
      </aside>
      <div className="mobile-nav"><div className="brand"><span><Pill size={18} /></span><strong>RxLocal</strong></div><button className={tab === "dashboard" ? "active" : ""} onClick={() => setTab("dashboard")}>Dashboard</button><button className={tab === "messages" ? "active" : ""} onClick={() => setTab("messages")}>Messages {escalations.length ? `(${escalations.length})` : ""}</button></div>
      <main>
        {error ? <div className="error-banner"><CircleAlert size={18} /><div><strong>Live data unavailable</strong><span>{error}. No fallback or demo records are being shown.</span></div><button onClick={() => void refresh()}>Retry</button></div> : null}
        {loading && patients.length === 0 && !error ? <div className="page-loading"><LoaderCircle className="spin" size={26} /><span>Loading MongoDB records…</span></div> : null}
        {!error && (patients.length > 0 || !loading) ? tab === "dashboard"
          ? <Dashboard patients={patients} chats={chats} loading={loading} onRefresh={() => void refresh()} />
          : <Messages escalations={escalations} loading={loading} onRefresh={() => void refresh()} /> : null}
      </main>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
