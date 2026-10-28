import { useEffect, useRef, useState } from 'react';
import { ArrowLeft, ArrowRight, CheckCircle2, RotateCcw, Search, XCircle } from 'lucide-react';
import { request, useDesktop } from './state';
import type { HistoryDetail, HistoryPage as HistoryData } from './types';
import { basename, Empty, ErrorNotice, Markdown, PageHeading, Spinner, useAction } from './components';

export function HistoryPage() {
  const { snapshot, contentVersion } = useDesktop();
  const [filter, setFilter] = useState({ result: 'all', project: '', keyword: '', page: 0 });
  const [data, setData] = useState<HistoryData | null>(null);
  const [detail, setDetail] = useState<HistoryDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const detailRequest = useRef(0);
  const action = useAction();
  useEffect(() => {
    let current = true;
    const timer = setTimeout(() => {
      setLoading(true); setError('');
      request<HistoryData>('history.list', { ...filter, project: filter.project || null }).then((result) => {
        if (!current) return;
        if (!result.ok) throw new Error(result.note);
        setData(result);
      }).catch((error) => { if (current) setError(error.message); }).finally(() => { if (current) setLoading(false); });
    }, filter.keyword ? 250 : 0);
    return () => { current = false; clearTimeout(timer); };
  }, [filter, revision, contentVersion, snapshot?.stats.total_attempts]);
  useEffect(() => { setDetail(null); detailRequest.current++; }, [contentVersion]);
  function open(id: number) {
    const token = ++detailRequest.current;
    action.run(async () => {
      const result = await request<HistoryDetail & { ok: boolean; note: string }>('history.detail', { attempt_id: id });
      if (!result.ok) throw new Error(result.note);
      if (detailRequest.current === token) setDetail(result);
    });
  }
  if (detail) return <>
    <button className="text-link back-link" onClick={() => setDetail(null)}><ArrowLeft size={17} />返回答题记录</button>
    <PageHeading title={detail.snapshot.kp_name || '作答详情'} description={`${detail.time_text}　${basename(detail.project_root || '')}`}
      action={<button disabled={!detail.can_retry || action.busy} title={detail.retry_reason || ''} onClick={() => action.run(() => request('history.repractice', { attempt_id: detail.id }))}><RotateCcw size={17} />重新练习本题</button>} />
    {!detail.can_retry && <p className="notice">{detail.retry_reason}</p>}<ErrorNotice>{action.error}</ErrorNotice>
    <article className="panel history-detail"><span className={`result-label ${detail.correct ? 'success' : 'failure'}`}>{detail.correct ? <CheckCircle2 size={18} /> : <XCircle size={18} />}{detail.correct ? '回答正确' : '回答错误'}　你的选择 {detail.normalized_answer}　正确答案 {detail.snapshot.answer || '未记录'}</span>
      <Markdown text={detail.snapshot.question || '旧记录缺少题干快照。'} />
      <div className="history-options">{Object.entries(detail.snapshot.options || {}).map(([key, text]) => <div key={key}><strong>{key}</strong><Markdown text={text} /></div>)}</div>
      <hr /><h2>答案解析</h2><Markdown text={detail.snapshot.analysis || '旧记录没有解析。'} /></article>
  </>;
  return <>
    <PageHeading title="答题记录" description="每一次作答，都是知识积累的一部分。按项目和知识点回顾练习。"
      action={<button disabled={loading} onClick={() => setRevision((value) => value + 1)}><RotateCcw size={16} />刷新</button>} />
    <div className="filter-bar"><label className="search-input"><Search size={17} /><input aria-label="搜索知识点或题干" placeholder="搜索知识点或题干" value={filter.keyword} onChange={(event) => setFilter({ ...filter, keyword: event.target.value, page: 0 })} /></label>
      <select aria-label="筛选作答结果" value={filter.result} onChange={(event) => setFilter({ ...filter, result: event.target.value, page: 0 })}><option value="all">全部结果</option><option value="correct">回答正确</option><option value="wrong">回答错误</option></select>
      <select aria-label="筛选项目" value={filter.project} onChange={(event) => setFilter({ ...filter, project: event.target.value, page: 0 })}><option value="">全部项目</option>{data?.projects.filter(Boolean).map((root) => <option key={root} value={root}>{basename(root)}</option>)}</select>
    </div><ErrorNotice>{error || action.error}</ErrorNotice>
    <section className="panel" aria-busy={loading}>{loading && <div className="loading-inline"><Spinner />正在查询记录…</div>}
      {!data?.items.length ? <Empty title="没有符合条件的作答记录">{filter.keyword || filter.result !== 'all' || filter.project ? '试试其他筛选条件。' : '完成第一道题后，可以在这里查看答案与解析。'}</Empty> : <div className="history-list">{data.items.map((item) => <button className="history-row" key={item.id} disabled={action.busy} onClick={() => open(item.id)}>
        {item.correct ? <CheckCircle2 className="success-text" size={22} aria-label="答对" /> : <XCircle className="danger-text" size={22} aria-label="答错" />}
        <span><strong>{item.knowledge_point || '未记录知识点'}</strong><small>{item.project_name} · {item.time_text}</small></span><ArrowRight size={17} />
      </button>)}</div>}
      <div className="pagination"><span>共 {data?.total || 0} 条记录</span><div className="actions"><button aria-label="上一页" disabled={!data?.page || loading} onClick={() => setFilter({ ...filter, page: Math.max(0, (data?.page || 0) - 1) })}><ArrowLeft size={16} /></button>
        <span>第 {(data?.page || 0) + 1} / {Math.max(1, Math.ceil((data?.total || 0) / 20))} 页</span>
        <button aria-label="下一页" disabled={!data || (data.page + 1) * 20 >= data.total || loading} onClick={() => setFilter({ ...filter, page: (data?.page || 0) + 1 })}><ArrowRight size={16} /></button></div></div>
    </section>
  </>;
}
