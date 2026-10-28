import { useSyncExternalStore } from 'react';
import type { Snapshot, PracticeState, TaskState } from './types';

interface State { snapshot: Snapshot | null; practice: PracticeState; fatal: string; closing: boolean; contentVersion: number }
let state: State = { snapshot: null, practice: { quiz: null, drafts: {}, grades: {}, busy: false, error: '' },
  fatal: '', closing: false, contentVersion: 0 };
const listeners = new Set<() => void>();
function update(patch: Partial<State>) { state = { ...state, ...patch }; listeners.forEach((listener) => listener()); }

export async function request<T = { ok: boolean; note?: string }>(method: string, params: object = {}): Promise<T> {
  if (!window.trainer) throw new Error('请使用桌面应用打开。');
  try { return await window.trainer.request(method, params as Record<string, unknown>) as T; }
  catch (error) {
    const message = error instanceof Error ? error.message : '操作未完成，请重试。';
    throw new Error(message.replace(/^Error invoking remote method '[^']+': Error: /, ''));
  }
}
export function useDesktop() {
  return useSyncExternalStore((listener) => { listeners.add(listener); return () => { listeners.delete(listener); }; }, () => state);
}

export function connect() {
  if (!window.trainer) { update({ fatal: '请从 Electron 桌面入口启动应用。' }); return () => {}; }
  const unsubscribe = window.trainer.subscribe(({ type, data }) => {
    if (type === 'snapshot') update({ snapshot: data as Snapshot });
    else if (type === 'practice_state') update({ practice: data as PracticeState });
    else if (type === 'fatal') update({ fatal: (data as { message: string }).message });
    else if (type === 'closing') update({ closing: true });
    else if (type === 'bank_progress' && state.snapshot)
      update({ snapshot: { ...state.snapshot, bank_task: data as TaskState } });
    else if (type === 'content_changed') update({ contentVersion: state.contentVersion + 1 });
  });
  request<Snapshot>('snapshot').then((snapshot) => update({ snapshot })).catch((error) => update({ fatal: error.message }));
  request<PracticeState>('practice.state').then((practice) => update({ practice })).catch(() => {});
  return unsubscribe;
}
