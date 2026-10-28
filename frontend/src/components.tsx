import { lazy, Suspense, useState, type ReactNode } from 'react';
import { AlertCircle, Inbox, LoaderCircle } from 'lucide-react';

const MarkdownContent = lazy(() => import('./Markdown').then((module) => ({ default: module.Markdown })));

export const basename = (value: string) => value.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || '未记录项目';
export const date = (value?: number) => value ? new Date(value * 1000).toLocaleString('zh-CN') : '尚无记录';
export const bytes = (value: number) => value > 1048576 ? `${(value / 1048576).toFixed(1)} MiB` : `${(value / 1024).toFixed(1)} KiB`;

export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function run(work: () => Promise<unknown>) {
    if (busy) return;
    setBusy(true); setError('');
    try { await work(); } catch (error) { setError(error instanceof Error ? error.message : '操作失败，请重试。'); }
    finally { setBusy(false); }
  }
  return { busy, error, setError, run };
}
export function Spinner() { return <LoaderCircle className="spinner" size={18} aria-hidden="true" />; }
export function ErrorNotice({ children }: { children: ReactNode }) {
  return children ? <div role="alert" className="notice error"><AlertCircle size={19} /><span>{children}</span></div> : null;
}
export function Empty({ title, children, action }: { title: string; children: ReactNode; action?: ReactNode }) {
  return <div className="empty"><Inbox size={34} strokeWidth={1.4} aria-hidden="true" /><h3>{title}</h3><p>{children}</p>{action}</div>;
}
export function PageHeading({ title, description, action }: { title: string; description: string; action?: ReactNode }) {
  return <header className="page-heading"><div><h1>{title}</h1><p>{description}</p></div>{action && <div className="actions">{action}</div>}</header>;
}
export function Markdown({ text }: { text?: string }) {
  return <Suspense fallback={<p className="muted" role="status">正在排版内容…</p>}><MarkdownContent text={text} /></Suspense>;
}
