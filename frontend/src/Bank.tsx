import { useEffect, useRef, useState } from 'react';
import { AlertTriangle, Check, Database, Download, HardDrive, RefreshCw, Trash2, X } from 'lucide-react';
import { request, useDesktop } from './state';
import type { CleanupPlan, Inventory } from './types';
import { bytes, ErrorNotice, PageHeading, Spinner, useAction } from './components';

export function BankPage() {
  const { snapshot, contentVersion } = useDesktop();
  const [selected, setSelected] = useState<string[]>([]);
  const [timeout, setTimeoutValue] = useState(1800);
  const [inventory, setInventory] = useState<Inventory | null>(null);
  const [plan, setPlan] = useState<CleanupPlan | null>(null);
  const [unknown, setUnknown] = useState(false);
  const [notice, setNotice] = useState('');
  const modal = useRef<HTMLDialogElement>(null);
  const action = useAction();
  useEffect(() => { setInventory(null); }, [contentVersion, snapshot?.bank_task.state]);
  useEffect(() => { if (plan) modal.current?.showModal(); else modal.current?.close(); }, [plan]);
  if (!snapshot) return null;
  const task = snapshot.bank_task;
  const building = ['building', 'applying', 'cleaning'].includes(task.state);
  const progress = task.total ? Math.min(100, Math.round((task.completed || 0) / task.total * 100)) : undefined;
  const localLabels: Record<string, string> = { missing: '未下载', verified: '缓存可复用', legacy: '版本快照', incomplete: '缓存不完整', invalid: '缓存损坏', unknown: '待校验' };
  function preview(kind: string) { action.run(async () => { const result = await request<CleanupPlan>('bank.plan', { kind, repositories: selected }); setPlan(result); setUnknown(false); }); }
  return <>
    <PageHeading title="题库管理" description="从公开来源构建本地知识库，为情景题提供可核对的依据。"
      action={<button disabled={action.busy || building} onClick={() => action.run(async () => setInventory(await request<Inventory>('bank.inventory')))}><RefreshCw size={16} />刷新状态与空间</button>} />
    <div className="bank-summary"><Database size={25} /><div><strong>{snapshot.bank_count.toLocaleString()} <span>个知识点</span></strong><p>本地题库 · 下载与构建成功后自动应用</p></div><span className="tag">固定来源版本</span></div>
    <ErrorNotice>{action.error}</ErrorNotice>
    <section className="panel"><div className="section-heading"><h2>题库来源</h2><span className="muted">已选择 {selected.length} 项</span></div>
      <div className="source-list">{snapshot.sources.map((source) => {
        const local = inventory?.sources[source.repository];
        const progress = task.sources?.[source.repository];
        return <label className={`source-row ${selected.includes(source.repository) ? 'is-selected' : ''}`} key={source.repository}>
          <input type="checkbox" disabled={building} checked={selected.includes(source.repository)} onChange={(event) => setSelected(event.target.checked ? [...selected, source.repository] : selected.filter((value) => value !== source.repository))} />
          <span className="source-info"><strong>{source.name_zh || source.repository}</strong><span>{source.repository}</span><small>{source.license || '许可待确认'} · {source.commit.slice(0, 8)}</small>{local?.reason && <small>{local.reason}</small>}</span>
          <span className="source-status"><span className={`tag ${local?.local_state === 'verified' ? 'good' : ''}`}>{progress ? ({ waiting: '等待处理', downloading: '正在准备', completed: '来源就绪', failed: '来源失败', cancelled: '已取消' } as Record<string, string>)[progress.state] || progress.state : local ? localLabels[local.local_state] || local.local_state : '点击刷新查看'}</span>{local && <small>{local.used} · {bytes(Object.values(local.bytes_by_category).reduce((sum, value) => sum + value, 0))}</small>}{progress?.reason && <small>{progress.reason}</small>}</span>
        </label>;
      })}</div>
      <div className="bank-controls"><label className="compact-field">任务总时限（秒）<input type="number" min="30" max="86400" value={timeout} onChange={(event) => setTimeoutValue(Number(event.target.value))} disabled={building} /></label>
        <button className="primary" disabled={!selected.length || building || action.busy || timeout < 30 || timeout > 86400} onClick={() => action.run(() => request('bank.build', { repositories: selected, timeout }))}><Download size={17} />下载并构建</button>
        {building && <button disabled={task.state !== 'building'} onClick={() => action.run(() => request('bank.cancel'))}><X size={16} />取消构建</button>}</div>
      {task.state === 'failed' && Object.values(task.sources || {}).some((source) => source.state === 'failed') && <div className="bank-retry"><button disabled={action.busy} onClick={() => action.run(() => request('bank.build', { retry_failed: true, timeout }))}><RefreshCw size={16} />仅重试失败项</button></div>}
      <div className="build-status" role="status">{building && <Spinner />}<span>{task.message}</span>{building && <progress max="100" {...(progress !== undefined ? { value: progress } : {})} aria-label="题库构建进度" />}</div>
    </section>
    <p className="footnote">构建会从所选公开仓库下载固定版本；普通重建复用已校验的缓存。第三方内容的版权与许可证独立适用。</p>
    <section className="panel settings-section"><h2><HardDrive size={19} />存储管理</h2>{inventory && <div className="storage-summary">{Object.entries(inventory.totals).map(([key, value]) => <span key={key}>{({ cache: '来源缓存', current: '当前版本', history: '历史版本', temporary: '临时文件' } as Record<string, string>)[key] || key}<strong>{bytes(value)}</strong></span>)}</div>}
      <div className="actions"><button disabled={building || action.busy} onClick={() => preview('history')}>清理历史版本</button><button className="danger" disabled={building || action.busy || !selected.length} onClick={() => preview('sources')}><Trash2 size={16} />删除所选来源</button></div>
      {notice && <p role="status">{notice}</p>}{inventory?.warnings.map((warning) => <p key={warning} className="notice">{warning}</p>)}</section>
    <dialog ref={modal} className="confirm-dialog" onCancel={() => setPlan(null)} aria-labelledby="cleanup-title"><form onSubmit={(event) => { event.preventDefault(); action.run(async () => {
      const result = await request<{ ok: boolean; note: string }>('bank.clean', { token: plan?.token, include_unknown: unknown }); setNotice(result.note); setPlan(null); setInventory(null);
    }); }}><div className="dialog-heading"><AlertTriangle size={23} /><h2 id="cleanup-title">确认清理范围</h2></div>
      <p>预计释放 {bytes(plan?.estimated_bytes || 0)}。删除来源会同时删除与该来源关联的学习数据。</p>
      {plan?.last_source && <p className="notice error">这是最后一个来源。删除后将停用监听与练习，重新构建题库后恢复。</p>}
      {!!plan?.repositories.length && <p>{plan.repositories.join('、')}</p>}
      <ul className="cleanup-targets">{plan?.targets.map((target) => <li key={target}>{target}</li>)}</ul>
      {plan && Object.keys(plan.learning).length > 0 && <details><summary>关联学习记录</summary><pre>{JSON.stringify(plan.learning, null, 2)}</pre></details>}
      <label className="checkbox-row"><input type="checkbox" checked={unknown} onChange={(event) => setUnknown(event.target.checked)} /><span>同时删除来源无法确认的旧学习记录</span></label><ErrorNotice>{action.error}</ErrorNotice>
      <div className="form-actions"><button type="button" disabled={action.busy} onClick={() => setPlan(null)}>取消</button><button type="submit" className="danger" disabled={action.busy}>{action.busy ? <Spinner /> : <Trash2 size={16} />}确认清理</button></div>
    </form></dialog>
  </>;
}
