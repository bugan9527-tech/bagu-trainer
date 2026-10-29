import { useEffect, useState } from 'react';
import { CheckCircle2, FolderOpen, Pause, Play, Plus, Save, Trash2, Zap } from 'lucide-react';
import { request, useDesktop } from './state';
import type { ModelForm, Preferences } from './types';
import { basename, date, Empty, ErrorNotice, PageHeading, Spinner, useAction } from './components';

const projectLabels: Record<string, string> = { initializing: '建立基线', watching: '监听中', paused: '已暂停',
  generating: '正在出题', quiz_ready: '新题已就绪', unavailable: '目录不可用', not_ready: '等待准备',
  waiting: '等待文件稳定', generation_unavailable: '出题未完成', project_unavailable: '目录不可用' };

export function ProjectsPage() {
  const { snapshot } = useDesktop();
  const [projects, setProjects] = useState(snapshot?.projects || []);
  const [saved, setSaved] = useState('');
  const action = useAction();
  if (!snapshot) return null;
  const dirty = JSON.stringify(projects) !== JSON.stringify(snapshot.projects);
  return <>
    <PageHeading title="监听项目" description="选择你正在开发的项目，把后续代码改动转化为练习。"
      action={<button className="primary" disabled={action.busy} onClick={() => action.run(async () => {
        const path = await request<string | null>('native.pickProject');
        if (path) { setProjects((items) => [...new Set([...items, path])]); setSaved(''); }
      })}><Plus size={18} />添加项目</button>} />
    <ErrorNotice>{action.error}</ErrorNotice>
    <section className="panel"><div className="section-heading"><h2>项目目录 <span className="count">{projects.length}</span></h2>
      <button disabled={action.busy || !snapshot.ready} onClick={() => action.run(() => request('monitor.pause', { paused: !snapshot.paused }))}>{snapshot.paused ? <Play size={16} /> : <Pause size={16} />}{snapshot.paused ? '恢复监听' : '暂停监听'}</button></div>
      {!projects.length ? <Empty title="添加第一个代码项目">选择实际代码所在的文件夹。保存后先建立基线，之后的修改才会触发练习。</Empty> : <div className="project-list">{projects.map((root) => {
        const status = snapshot.project_states[root] || Object.entries(snapshot.project_states).find(([path]) => path.toLowerCase() === root.toLowerCase())?.[1];
        return <div className="project-row" key={root}><span className="project-icon"><FolderOpen size={23} /></span><div className="project-info"><strong>{basename(root)}</strong><p className="path">{root}</p>
          <small>{status?.reason || (status?.last_success_at ? `最近生成：${date(status.last_success_at)}` : '启动或添加时先建立文件基线。')}</small></div>
          <span className={`tag ${status?.state === 'watching' ? 'good' : ''}`}>{dirty && !snapshot.projects.includes(root) ? '待保存' : projectLabels[status?.state || 'not_ready'] || status?.state}</span>
          <button className="icon-button danger-text" aria-label={`移除项目 ${basename(root)}`} disabled={action.busy} onClick={() => { setProjects(projects.filter((path) => path !== root)); setSaved(''); }}><Trash2 size={18} /></button></div>;
      })}</div>}
      <div className="panel-footer"><span className="muted" role="status">{dirty ? '有未保存的项目变更' : saved || '移除监听不会删除项目文件或已有答题记录。'}</span><button className="primary" disabled={!dirty || action.busy} onClick={() => action.run(async () => {
        await request('projects.save', { projects }); setSaved('项目已保存并应用。');
      })}>{action.busy ? <Spinner /> : <Save size={16} />}保存并应用</button></div>
    </section><p className="footnote">改动稳定约 15 秒后开始匹配。暂停监听时，仍可主动练习已有题目。</p>
  </>;
}

export function ModelPage() {
  const { snapshot } = useDesktop();
  const [form, setForm] = useState<ModelForm>({ base_url: snapshot?.model.base_url || '', model: snapshot?.model.model || '', api_key: '', timeout_sec: snapshot?.model.timeout_sec || 30 });
  const [revision, setRevision] = useState(0);
  const action = useAction();
  if (!snapshot) return null;
  const task = snapshot.model_task;
  const testing = task.state === 'testing';
  const verified = task.state === 'verified' && task.form_revision === revision;
  function change<K extends keyof ModelForm>(key: K, value: ModelForm[K]) {
    setForm((current) => ({ ...current, [key]: value })); setRevision((value) => value + 1);
  }
  const sources: Record<string, string> = { env: '环境变量', keyring: 'Windows 凭据管理器', legacy: '旧配置文件', none: '尚未配置' };
  return <>
    <PageHeading title="模型配置" description="连接你自己的模型接口，为项目生成有来源依据的情景题。" />
    <div className="settings-columns"><section className="panel form-panel"><h2>连接设置</h2>
      <form onSubmit={(event) => { event.preventDefault(); action.run(() => request('model.test', { ...form, form_revision: revision })); }}>
        <label>接口基础地址<input type="url" required value={form.base_url} placeholder="https://服务商地址/v1" disabled={testing} onChange={(event) => change('base_url', event.target.value)} /><small>通常以 /v1 结尾，不包含 /chat/completions。</small></label>
        <label>模型名称<input required value={form.model} placeholder="服务商提供的模型标识" disabled={testing} onChange={(event) => change('model', event.target.value)} /></label>
        <label>API Key<input type="password" autoComplete="off" spellCheck={false} value={form.api_key} placeholder={snapshot.model.key_source !== 'none' ? '已有 Key，可留空沿用' : '输入 API Key'} disabled={testing} onChange={(event) => change('api_key', event.target.value)} /><small>当前来源：{sources[snapshot.model.key_source] || snapshot.model.key_source}。保存后使用系统凭据管理器。</small></label>
        <label>超时（秒）<input type="number" min="1" max="120" required value={form.timeout_sec} disabled={testing} onChange={(event) => change('timeout_sec', Number(event.target.value))} /><small>出题和关联审核共用此时间预算，最多 120 秒。</small></label>
        <ErrorNotice>{action.error}</ErrorNotice>
        <div className="form-actions"><button type="submit" disabled={testing || action.busy}>{testing ? <Spinner /> : <Zap size={17} />}{testing ? '正在测试' : '测试连接'}</button>
          {testing && <button type="button" onClick={() => action.run(() => request('model.cancel'))}>取消测试</button>}
          <button type="button" className="primary" disabled={!verified || action.busy} onClick={() => action.run(async () => {
            await request('model.save', form); setForm((current) => ({ ...current, api_key: '' }));
          })}><Save size={17} />保存配置</button></div>
      </form></section>
      <aside className="context-panel"><div className="context-icon"><Zap size={24} /></div><h2>先验证，再开始出题</h2><p>主动测试会发送一次小请求，可能产生服务商费用。通过后保存，新的配置立即生效。</p>
        <div className={`connection-status ${verified || task.state === 'saved' ? 'success' : ''}`} role="status">{testing ? <Spinner /> : verified || task.state === 'saved' ? <CheckCircle2 size={20} /> : <span className="status-dot" />}
          <span>{task.state === 'verified' && !verified ? '配置已修改，请重新测试连接。' : task.message}</span></div>
        <hr /><h3>你的项目数据</h3><p>出题时会向所配置的接口发送经过筛选和脱敏的代码上下文及题库来源正文。请选择符合项目数据要求的服务商。</p>
      </aside></div>
  </>;
}

export function SettingsPage() {
  const { snapshot } = useDesktop();
  const [preferences, setPreferences] = useState<Preferences>(snapshot!.settings);
  const [startup, setStartup] = useState({ enabled: false, supported: false });
  const [saved, setSaved] = useState('');
  const action = useAction();
  useEffect(() => { request<typeof startup>('native.startupStatus').then(setStartup).catch(() => {}); }, []);
  if (!snapshot) return null;
  function change<K extends keyof Preferences>(key: K, value: Preferences[K]) { setPreferences((current) => ({ ...current, [key]: value })); setSaved(''); }
  return <>
    <PageHeading title="应用设置" description="按自己的节奏安排练习，管理本机数据与启动方式。" />
    <section className="panel form-panel wide-form"><h2>练习偏好</h2><form onSubmit={(event) => { event.preventDefault(); action.run(async () => { await request('settings.save', preferences); setSaved('偏好已保存并生效。'); }); }}>
      <label>每次命中最多生成几道题<input type="number" min="1" max="5" required value={preferences.max_quizzes_per_hit} onChange={(event) => change('max_quizzes_per_hit', Number(event.target.value))} /></label>
      <label>上下文字符上限<input type="number" min="500" max="12000" required value={preferences.context_char_limit} onChange={(event) => change('context_char_limit', Number(event.target.value))} /><small>限制传入匹配与出题流程的上下文长度。</small></label>
      <label>待答题有效期（秒）<input type="number" min="0" max="31536000" required value={preferences.quiz_ttl_seconds} onChange={(event) => change('quiz_ttl_seconds', Number(event.target.value))} /><small>0 表示保留未答题；容量上限仍然生效。</small></label>
      <label className="checkbox-row"><input type="checkbox" checked={preferences.session_ingest} onChange={(event) => change('session_ingest', event.target.checked)} /><span>允许读取可确认项目归属的 Trae 会话<small>开启后，符合范围要求的会话片段可能作为出题上下文。默认关闭。</small></span></label>
      <div className="form-actions"><button type="submit" className="primary" disabled={action.busy}><Save size={17} />保存偏好</button><span className="success-text" role="status">{saved}</span></div>
    </form></section>
    <section className="panel settings-section"><h2>登录自启动</h2><p>登录 Windows 后最小化启动训练器。</p><label className="checkbox-row"><input type="checkbox" checked={startup.enabled} disabled={!startup.supported || action.busy} onChange={(event) => action.run(async () => {
      const enabled = event.target.checked; const result = await request<{ enabled: boolean }>('native.startup', { enabled }); setStartup({ ...startup, enabled: result.enabled });
    })} /><span>登录后启动{!startup.supported && <small>打包后的应用支持此设置。</small>}</span></label></section>
    <section className="panel settings-section"><h2>个人数据</h2><p className="path">{snapshot.user_data}</p><p className="muted">配置、题库与学习记录保存在本机。更新程序时继续使用相同的数据目录。</p><button onClick={() => action.run(() => request('native.openData'))}><FolderOpen size={17} />打开数据目录</button></section>
    <section className="panel settings-section"><h2>完整卸载</h2><p className="muted">退出后在独立工具中核对范围，清除受管配置、题库、学习记录和本机凭据。此操作不能用于保留数据的升级。</p>
      <button className="danger" disabled={action.busy} onClick={() => action.run(() => request('native.uninstall'))}><Trash2 size={17} />卸载应用并清除全部数据…</button></section>
    <ErrorNotice>{action.error}</ErrorNotice><p className="footnote">AI 八股训练器 {snapshot.version} · Electron 桌面版</p>
  </>;
}
