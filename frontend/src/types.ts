export type Project = {
  project_id: string;
  title: string;
  description?: string;
  recording_count: number;
  total_duration_seconds: number;
  updated_at: string;
};

export type Recording = {
  recording_id: string;
  project_id: string;
  file_name: string;
  status: string;
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
  selected_recording_ids: string[];
  sources: Array<{ file_name: string; start_time_ms: number; quote: string }>;
  status: string;
  created_at: string;
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

export type AppSettings = {
  basic: {
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
