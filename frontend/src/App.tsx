import { useEffect, useState } from 'react';
import { ArrowRight, BookOpen, Check, CheckCircle2, ChevronRight, Database, FolderOpen, History,
  LayoutDashboard, Pause, Play, RefreshCw, Settings2, Sparkles, Terminal, Zap } from 'lucide-react';
import { request, useDesktop } from './state';
import type { Page } from './types';
import { basename, ErrorNotice, PageHeading, Spinner, useAction } from './components';
import { AnswerWindow, PracticePage } from './Practice';
import { HistoryPage } from './History';
import { ModelPage, ProjectsPage, SettingsPage } from './SetupPages';
import { BankPage } from './Bank';

const pages = [
  { id: 'dashboard', name: '工作台', icon: LayoutDashboard },
  { id: 'practice', name: '练习中心', icon: BookOpen },
  { id: 'history', name: '答题记录', icon: History },
  { id: 'projects', name: '监听项目', icon: FolderOpen },
  { id: 'bank', name: '题库管理', icon: Database },
  { id: 'model', name: '模型配置', icon: Zap },
  { id: 'settings', name: '应用设置', icon: Settings2 },
] as const;

function Dashboard({ navigate }: { navigate: (page: Page) => void }) {
  const { snapshot: s, practice } = useDesktop();
  const action = useAction();
  if (!s) return null;
  return <>
    <PageHeading title="今天的练习" description="让正在写的项目，成为下一场面试的准备。"
      action={<button disabled={action.busy} onClick={() => action.run(() => request('prepare'))}><RefreshCw size={16} />重新检查</button>} />
    <ErrorNotice>{s.error || action.error}</ErrorNotice>
    <section className="learning-focus"><div className="focus-main"><div className="focus-symbol"><BookOpen size={28} strokeWidth={1.7} /></div>
      <div><span className="focus-label">{s.ready ? s.pending.length ? '准备好，进入练习' : '保持思考，继续积累' : '开始之前，完成准备'}</span>
        <h2>{s.pending.length ? <>{s.pending.length} 道情景题，等你解答</> : s.ready ? '下一道题，来自你的下一次改动' : '连接项目与知识'}</h2>
        <p>{s.ready ? '结合真实代码场景练习，用答案解析补齐知识，再按计划复习。' : '准备题库、连接模型、选择项目。完成后，应用会自动开始监听。'}</p>
        <button className="primary" disabled={practice.busy} onClick={() => {
          if (!s.ready || !s.pending.length && !s.stats.due_reviews) navigate(s.ready ? 'projects' : 'bank');
          else action.run(() => request('practice.resume'));
        }}>{s.ready ? s.pending.length || s.stats.due_reviews ? '开始练习' : '查看监听项目' : '准备题库'}<ArrowRight size={18} /></button>
      </div></div><div className="focus-path" aria-hidden="true"><span><Terminal size={19} />代码改动</span><ChevronRight size={16} /><span><Sparkles size={19} />情景出题</span><ChevronRight size={16} /><span><CheckCircle2 size={19} />间隔复习</span></div></section>
    <div className="stats-strip"><div><span>累计作答</span><strong>{s.stats.total_attempts.toLocaleString()}<small>次</small></strong></div>
      <div><span>正确率</span><strong>{s.stats.total_attempts ? Math.round(s.stats.accuracy * 100) : '—'}<small>{s.stats.total_attempts ? '%' : ''}</small></strong></div>
      <div><span>到期复习</span><strong>{s.stats.due_reviews}<small>个知识点</small></strong></div><div><span>监听项目</span><strong>{s.projects.length}<small>个</small></strong></div></div>
    <div className="dashboard-columns"><section className="panel"><div className="section-heading"><h2>运行准备</h2><span className={`tag ${s.ready ? 'good' : ''}`}>{s.ready ? '已就绪' : '待完成'}</span></div>
      <div className="readiness-list">{s.checks.map((item) => <button key={item.name} className="readiness-row" onClick={() => navigate(({ bank: 'bank', model: 'model', projects: 'projects' } as Record<string, Page>)[item.name])}>
        <span className={`check-icon ${item.ready ? 'done' : ''}`}>{item.ready ? <Check size={16} /> : <span />}</span>
        <span><strong>{({ bank: '本地题库', model: '模型连接', projects: '监听项目' } as Record<string, string>)[item.name]}</strong><small>{item.detail}</small></span><ChevronRight size={17} />
      </button>)}</div></section>
      <section className="panel monitor-panel"><div className="section-heading"><h2>项目动态</h2><span className={`status-dot ${s.ready && !s.paused ? 'active' : ''}`} /></div>
        <p className="monitor-status">{s.paused ? '监听已暂停，仍可练习已有题目。' : s.status}</p>
        <div className="mini-projects">{s.projects.slice(0, 3).map((root) => <div key={root}><FolderOpen size={16} /><span>{basename(root)}</span></div>)}</div>
        <div className="actions"><button disabled={!s.ready || action.busy} onClick={() => action.run(() => request('monitor.pause', { paused: !s.paused }))}>{s.paused ? <Play size={16} /> : <Pause size={16} />}{s.paused ? '恢复监听' : '暂停监听'}</button>
          <button disabled={!s.ready || action.busy} onClick={() => action.run(() => request('monitor.retry'))}>重试出题</button></div>
      </section></div>
    {!!s.stats.weak_knowledge_points?.length && <section className="panel weak-points"><div className="section-heading"><h2>值得再练的知识点</h2><button className="text-link" onClick={() => navigate('history')}>查看记录<ArrowRight size={16} /></button></div>
      {s.stats.weak_knowledge_points.map((item) => <div key={item.kp_id}><span>{item.knowledge_point}</span><meter min="0" max="1" value={item.mastery} aria-label={`${item.knowledge_point}掌握度`} /><strong>{Math.round(item.mastery * 100)}%</strong></div>)}</section>}
  </>;
}

export function App() {
  const { snapshot, fatal, closing } = useDesktop();
  const answer = window.location.hash === '#answer';
  const [page, setPage] = useState<Page>('dashboard');
  useEffect(() => window.trainer?.subscribe(({ type, data }) => {
    if (type === 'show' && pages.some((item) => item.id === data)) setPage(data as Page);
  }), []);
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'r' &&
          !(event.target as HTMLElement).closest('input,textarea,select,[contenteditable="true"]')) {
        event.preventDefault();
        request('native.continuePractice').catch(() => setPage('practice'));
      }
    };
    window.addEventListener('keydown', keydown);
    return () => window.removeEventListener('keydown', keydown);
  }, []);
  if (fatal) return <div className="startup-screen"><Terminal size={34} /><h1>训练器暂时无法连接</h1><p role="alert">{fatal}</p><p className="muted">检查运行环境或退出已有训练器后，重新启动应用。</p></div>;
  if (!snapshot) return <div className="startup-screen"><Spinner /><h1>正在准备学习工作台</h1><p>读取本机配置与学习记录…</p></div>;
  return <>{answer ? <AnswerWindow /> : <div className="app-shell"><aside className="sidebar"><div className="brand"><span className="brand-icon"><Terminal size={24} /></span><div><strong>八股训练器</strong><span>从代码到知识</span></div></div>
      <nav aria-label="主导航">{pages.map(({ id, name, icon: Icon }, index) => <div key={id} className={index === 3 ? 'nav-divider' : ''}><button className={`nav-item ${page === id ? 'current' : ''}`} aria-current={page === id ? 'page' : undefined} onClick={() => setPage(id)}><Icon size={19} /><span>{name}</span>{id === 'practice' && snapshot.pending.length > 0 && <span className="nav-badge">{snapshot.pending.length}</span>}</button></div>)}</nav>
      <div className="sidebar-bottom"><span className={`status-dot ${snapshot.ready && !snapshot.paused ? 'active' : ''}`} /><span>{snapshot.ready ? snapshot.paused ? '监听已暂停' : '学习引擎已就绪' : '等待完成准备'}</span><small>v{snapshot.version}</small></div></aside>
      <div className="workspace"><div className="workspace-bar"><span>我的学习空间</span><span className="workspace-hint">代码驱动的面试练习</span></div>
        <main className="workspace-content" id="main-content">{pages.map(({ id }) => <div key={id} hidden={page !== id}>
          {id === 'dashboard' ? <Dashboard navigate={setPage} /> : id === 'practice' ? <PracticePage /> : id === 'history' ? <HistoryPage /> : id === 'projects' ? <ProjectsPage /> : id === 'bank' ? <BankPage /> : id === 'model' ? <ModelPage /> : <SettingsPage />}
        </div>)}</main></div></div>}
    {closing && <div className="closing-overlay" role="alertdialog" aria-modal="true" aria-label="正在退出"><div><Spinner /><h2>正在保存并退出</h2><p>等待进行中的写入完成，并取消后台任务。</p></div></div>}
  </>;
}
