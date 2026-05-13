export type Project = {
  project_id: string;
  title: string;
  description?: string;
  recording_count: number;
  file_count?: number;
  total_duration_seconds: number;
  owner_id?: string;
  owner_name?: string;
  is_shared?: boolean;
  access_role?: 'admin' | 'owner' | 'member' | 'shared' | '';
  updated_at: string;
};

export type Recording = {
  file_id?: string;
  recording_id: string;
  project_id: string;
  source_project_id?: string;
  reference_id?: string | null;
  reference_status?: string;
  source?: 'own' | 'reference';
  file_name: string;
  file_type?: 'audio' | 'pdf' | 'excel' | 'docx' | 'text' | 'markdown';
  status: string;
  extraction_status?: string;
  extracted_char_count?: number;
  extraction_engine?: string;
  extraction_warnings?: string[];
  duration_seconds: number;
  file_size_bytes: number;
  template_type: string;
  summary_stale: boolean;
  created_at?: string;
  updated_at?: string;
  current_job_type?: string;
  current_job_status?: string;
  current_job_created_at?: string;
  current_job_started_at?: string;
  latest_failed_job_id?: string;
  latest_failed_job_type?: string;
  latest_failed_job_error_code?: string;
  latest_failed_job_error_message?: string;
  latest_failed_job_finished_at?: string;
};

export type TranscriptSegment = {
  segment_id: string;
  speaker: string;
  start_time_ms: number;
  end_time_ms: number;
  text: string;
  raw_text?: string;
  edited?: boolean;
};

export type Job = {
  job_id: string;
  project_id?: string;
  recording_id?: string;
  job_type: string;
  status: string;
  progress: number;
  external_task_id?: string;
  metadata?: Record<string, unknown>;
  error_code?: string;
  error_message?: string;
  created_at?: string;
  started_at?: string;
  finished_at?: string;
};

export type QAItem = {
  qa_session_id: string;
  question: string;
  answer: string;
  status: string;
  recording_ids: string[];
  sources: Array<{ file_name: string; start_time_ms: number; quote: string }>;
  created_at: string;
};

export type QAThread = {
  thread_id: string;
  project_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  last_message_at: string;
  messages?: QAMessage[];
};

export type QAMessage = {
  message_id: string;
  thread_id: string;
  role: 'user' | 'assistant';
  content: string;
  reasoning_content?: string;
  selected_recording_ids: string[];
  selected_file_ids?: string[];
  sources: Array<{ file_name: string; start_time_ms: number; quote: string }>;
  status: string;
  created_at: string;
};

export type User = {
  user_id: string;
  username: string;
  display_name: string;
  role: 'admin' | 'user';
  status: 'active' | 'disabled' | 'deleted';
  quota?: UserQuota;
};

export type UserQuota = {
  daily_asr_seconds: number;
  monthly_asr_seconds: number;
  daily_qa_tokens: number;
  monthly_qa_tokens: number;
};

export type MeUsage = {
  user: User;
  quota: UserQuota;
  today: { asr_seconds: number; qa_tokens: number };
  month: { asr_seconds: number; qa_tokens: number };
};

export type AiNodeSettings = {
  model: string;
  url: string;
  key: string;
  key_configured: boolean;
};

export type StorageSettings = {
  provider: 'local' | 'railway_bucket' | 's3_compatible';
  bucket_name: string;
  endpoint: string;
  region: string;
  path_prefix: string;
  access_key_id: string;
  secret_access_key: string;
  access_key_configured: boolean;
  secret_key_configured: boolean;
};

export type UploadSettings = {
  audio_max_upload_size_mb: number;
  audio_min_duration_seconds: number;
  audio_max_duration_seconds: number;
  document_max_batch_count: number;
  document_max_upload_size_mb: number;
};

export type AppSettings = {
  basic: UploadSettings & {
    max_upload_size_mb: number;
    max_recording_duration_hours: number;
  };
  ai: Record<'asr' | 'clean' | 'summary' | 'qa', AiNodeSettings>;
  storage: StorageSettings;
  saved?: boolean;
};

export type AiTestResult = {
  node: 'asr' | 'clean' | 'summary' | 'qa';
  status: 'passed';
  message: string;
  latency_ms: number;
  model: string;
  url: string;
};

export type StorageTestResult = {
  status: 'passed';
  message: string;
  provider: string;
  bucket_name: string;
  endpoint: string;
};
