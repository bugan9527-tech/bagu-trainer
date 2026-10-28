export interface Quiz {
  quiz_id: string; kp_id: string; knowledge_point: string; question: string;
  options: Record<string, string>; project_root?: string; difficulty?: string;
  status?: string; review?: boolean; manual_review?: boolean; generated_by?: string;
}
export interface Grade {
  ok: boolean; correct: boolean; correct_answer: string; analysis: string;
  normalized_answer: string; schedule: { next_review_at: number; mastery: number; interval_days: number };
}
export interface PracticeState {
  quiz: Quiz | null; drafts: Record<string, string>; grades: Record<string, Grade>;
  busy: boolean; error: string;
}
export interface TaskState {
  state: string; message: string; stage?: string; completed?: number; total?: number; form_revision?: number;
  sources?: Record<string, { state: string; stage: string; reason: string }>;
}
export interface ModelForm { base_url: string; model: string; api_key: string; timeout_sec: number }
export interface Preferences {
  max_quizzes_per_hit: number; context_char_limit: number; session_ingest: boolean; quiz_ttl_seconds: number;
}
export interface Snapshot {
  version: string; ready: boolean; error: string; status: string; paused: boolean;
  checks: { name: string; ready: boolean; detail: string }[];
  projects: string[]; project_states: Record<string, { state: string; reason: string; last_success_at?: number }>;
  pending: Quiz[]; stats: { total_attempts: number; accuracy: number; due_reviews: number;
    weak_knowledge_points?: { kp_id: string; knowledge_point: string; mastery: number }[] };
  model: Omit<ModelForm, 'api_key'> & { key_source: string };
  model_task: TaskState; bank_task: TaskState;
  sources: { repository: string; name_zh: string; commit: string; license: string; evidence_url: string }[];
  settings: Preferences; user_data: string; bank_count: number;
}
export interface HistoryItem { id: number; quiz_id: string; answered_at: number; correct: boolean;
  knowledge_point: string; project_root: string; project_name: string; time_text: string }
export interface HistoryPage { ok: boolean; note?: string; items: HistoryItem[]; total: number; page: number; projects: string[] }
export interface HistoryDetail extends HistoryItem {
  snapshot: { kp_name?: string; question?: string; options?: Record<string, string>; answer?: string; analysis?: string };
  normalized_answer: string; can_retry: boolean; retry_reason?: string;
}
export interface CleanupPlan { token: string; repositories: string[]; targets: string[];
  estimated_bytes: number; last_source: boolean; learning: Record<string, unknown> }
export interface Inventory { sources: Record<string, { local_state: string; used: string; reason: string;
  bytes_by_category: Record<string, number> }>; totals: Record<string, number>; warnings: string[] }
export type Page = 'dashboard' | 'practice' | 'history' | 'projects' | 'bank' | 'model' | 'settings';
export interface DesktopEvent { type: string; data: unknown }
declare global {
  interface Window { trainer?: {
    request: (method: string, params?: Record<string, unknown>) => Promise<unknown>;
    subscribe: (callback: (event: DesktopEvent) => void) => () => void;
  } }
}
