import { useEffect } from 'react';
import { ArrowLeft, ArrowRight, CheckCircle2, CornerDownLeft, XCircle } from 'lucide-react';
import { request, useDesktop } from './state';
import { basename, date, Empty, ErrorNotice, Markdown, PageHeading, Spinner, useAction } from './components';

export function PracticePage() {
  const { snapshot, practice } = useDesktop();
  const action = useAction();
  if (!snapshot) return null;
  return <>
    <PageHeading title="练习中心" description="从项目改动中生成的情景题。选择一题，在独立窗口专注作答。"
      action={<button className="primary" disabled={!snapshot.ready || practice.busy} onClick={() => action.run(async () => {
        const result = await request<{ quiz: unknown }>('practice.resume');
        if (!result.quiz) action.setError('当前没有待答题或到期复习，继续修改项目后再来看看。');
      })}>继续练习 <ArrowRight size={17} /></button>} />
    <ErrorNotice>{action.error}</ErrorNotice>
    <section className="panel"><div className="section-heading"><h2>待答题目 <span className="count">{snapshot.pending.length}</span></h2>
      <span className="muted">新题到达时保留当前页面</span></div>
      {!snapshot.pending.length ? <Empty title={snapshot.ready ? '暂时没有待答题' : '完成准备后开始练习'}>
        {snapshot.ready ? '在监听项目中保存代码改动，匹配到知识点且生成成功后，题目会出现在这里。' : '请在题库管理、模型配置和监听项目中完成准备。已有答题记录仍可查看。'}
      </Empty> : <div className="quiz-list">{snapshot.pending.map((quiz) => <button key={quiz.quiz_id} className="quiz-row" disabled={practice.busy}
        onClick={() => action.run(() => request('practice.enter', { quiz_id: quiz.quiz_id }))}>
        <span className="quiz-marker"><CornerDownLeft size={21} /></span><span className="quiz-row-content"><span className="quiz-meta">
          <span className="tag">{quiz.review ? '复习' : '情景练习'}</span><span>{basename(quiz.project_root || '')}</span></span>
          <strong>{quiz.knowledge_point}</strong><span className="question-preview">{quiz.question}</span></span>
        <span className="row-action">{practice.drafts[quiz.quiz_id] ? '继续作答' : '进入本题'}<ArrowRight size={16} /></span>
      </button>)}</div>}
    </section>
  </>;
}

export function AnswerWindow() {
  const { practice, snapshot } = useDesktop();
  const action = useAction();
  const quiz = practice.quiz;
  const answer = quiz ? practice.drafts[quiz.quiz_id] || '' : '';
  const grade = quiz ? practice.grades[quiz.quiz_id] : undefined;
  const pending = snapshot?.pending.some((item) => item.quiz_id !== quiz?.quiz_id);
  const submit = () => quiz && answer && !grade && !practice.busy && action.run(() =>
    request('practice.submit', { quiz_id: quiz.quiz_id, answer }));
  const next = () => quiz && !practice.busy && action.run(() => request('practice.next', { quiz_id: quiz.quiz_id }));

  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if ((event.target as HTMLElement).closest('input,textarea,select,[contenteditable="true"]')) return;
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); submit(); }
      else if (event.altKey && event.key.toLowerCase() === 'n') { event.preventDefault(); next(); }
      else if (!event.ctrlKey && !event.metaKey && !event.altKey && /^[a-d]$/i.test(event.key) && quiz && !grade && !practice.busy) {
        event.preventDefault(); action.run(() => request('practice.select', { quiz_id: quiz.quiz_id, answer: event.key.toUpperCase() }));
      }
    };
    window.addEventListener('keydown', keydown);
    return () => window.removeEventListener('keydown', keydown);
  });

  if (!quiz) return <Empty title="选择一道题开始练习" action={<button onClick={() => action.run(() => request('native.hidePractice'))}>返回工作台</button>}>
    在主窗口的练习中心选择题目，或点击“继续练习”。</Empty>;
  return <div className="answer-layout">
    <header className="answer-header"><button className="text-link" onClick={() => action.run(() => request('native.hidePractice'))}><ArrowLeft size={17} />收起练习</button>
      <span className="muted">{basename(quiz.project_root || '')}</span><span className="tag">{quiz.review ? '复习' : '情景练习'}</span></header>
    <main className="answer-scroll"><article className="answer-content">
      <p className="knowledge-label">{quiz.knowledge_point}</p><h1 className="sr-only">{quiz.knowledge_point}练习题</h1>
      <Markdown text={quiz.question} />
      <fieldset className="options" disabled={practice.busy || !!grade}><legend className="sr-only">选择答案</legend>
        {Object.entries(quiz.options).map(([letter, text]) => <label key={letter} className={`option ${answer === letter ? 'selected' : ''} ${grade && grade.correct_answer === letter ? 'correct' : ''} ${grade && answer === letter && !grade.correct ? 'wrong' : ''}`}>
          <input type="radio" name="answer" value={letter} checked={answer === letter} onChange={() => action.run(() => request('practice.select', { quiz_id: quiz.quiz_id, answer: letter }))} />
          <span className="option-letter">{letter}</span><span className="option-text"><Markdown text={text} /></span>
          {grade && grade.correct_answer === letter && <CheckCircle2 size={19} aria-label="正确答案" />}
        </label>)}
      </fieldset>
      {grade && <section className={`grade-panel ${grade.correct ? 'success' : 'failure'}`} aria-live="polite">
        <h2>{grade.correct ? <CheckCircle2 size={22} /> : <XCircle size={22} />}{grade.correct ? '回答正确' : `正确答案是 ${grade.correct_answer}`}</h2>
        <Markdown text={grade.analysis} /><div className="review-note">下次复习：{date(grade.schedule.next_review_at)}<span>掌握度 {Math.round(grade.schedule.mastery * 100)}%</span></div>
      </section>}
      <ErrorNotice>{practice.error || action.error}</ErrorNotice>
    </article></main>
    <footer className="answer-footer"><span className="muted">{grade ? '本次作答已保存' : answer ? `已选择 ${answer}` : '选择一个答案'}<small>{grade ? 'Alt+N 下一题' : 'A–D 选择 · Ctrl+Enter 提交'}</small></span>
      <div className="actions"><button disabled={practice.busy} onClick={next}>{pending ? '下一题' : '结束练习'}<ArrowRight size={17} /></button>
        {!grade && <button className="primary" disabled={!answer || practice.busy} onClick={submit}>{practice.busy ? <Spinner /> : <CheckCircle2 size={18} />}提交答案</button>}</div>
    </footer>
  </div>;
}
