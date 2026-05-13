import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, PointerEvent as ReactPointerEvent } from 'react';
import { Button, Card, Checkbox, Dropdown, Form, Input, InputNumber, Layout, List, Modal, Progress, Radio, Select, Space, Table, Tabs, Tag, Typography, Upload, message } from 'antd';
import { DeleteOutlined, DownloadOutlined, DownOutlined, EditOutlined, InboxOutlined, MoreOutlined, ReloadOutlined, SettingOutlined, UploadOutlined } from '@ant-design/icons';
import type { MenuProps, UploadFile } from 'antd';
import { api, clearToken, formatDuration, formatMs, getToken, setToken } from './api';
import type { AiTestResult, AppSettings, Job, MeUsage, Project, QAMessage, QAThread, Recording, StorageTestResult, TranscriptSegment, User, UserQuota } from './types';

const { Header, Content } = Layout;
const { Title, Text, Paragraph } = Typography;

const PROJECT_LAYOUT_STORAGE_KEY = 'ai_asr_project_layout_v1';
const PROJECT_LAYOUT_DEFAULT = { left: 280, right: 430 };
const LEFT_PANEL_MIN = 220;
const LEFT_PANEL_MAX = 460;
const RIGHT_PANEL_MIN = 300;
const RIGHT_PANEL_MAX = 930;
const MIDDLE_PANEL_MIN = 480;
const RESIZE_HANDLE_TOTAL_WIDTH = 16;

type ProjectColumnWidths = typeof PROJECT_LAYOUT_DEFAULT;
type ResizeDivider = 'left' | 'right';
type SegmentSaveOptions = { replaceSameSpeaker?: boolean };
type SegmentUpdateResult = { summary_stale?: boolean; updated_count?: number };
type RecordingStatus =
  | 'created'
  | 'uploading'
  | 'queued'
  | 'asr_processing'
  | 'asr_completed'
  | 'cleaning'
  | 'cleaning_completed'
  | 'summary_generating'
  | 'extracting'
  | 'extracted'
  | 'completed'
  | 'failed';

const RECORDING_STATUS_LABELS: Record<string, string> = {
  // Backend canonical statuses. Keep these in sync with backend/app/main.py and backend/app/tasks.py.
  created: '草稿',
  uploading: '上传中',
  queued: '排队中',
  asr_processing: '识别中',
  asr_completed: '识别完成',
  cleaning: '清洁稿生成中',
  cleaning_completed: '清洁稿完成',
  summary_generating: '纪要生成中',
  extracting: '文字提取中',
  extracted: '文字提取完成',
  completed: '处理完成',
  failed: '处理失败',
  // UI-only aliases kept for compatibility with visual refactors; business logic normalizes them first.
  uploaded: '已上传',
  pending: '排队中',
  transcribing: '识别中',
  summarizing: '纪要生成中',
  processing: '处理中',
};

const RECORDING_STATUS_ALIASES: Record<string, RecordingStatus> = {
  uploaded: 'queued',
  pending: 'queued',
  transcribing: 'asr_processing',
  summarizing: 'summary_generating',
  processing: 'queued',
};

const PROCESSING_RECORDING_STATUSES = new Set<RecordingStatus>([
  'uploading',
  'queued',
  'asr_processing',
  'asr_completed',
  'cleaning',
  'cleaning_completed',
  'summary_generating',
  'extracting',
]);

const JOB_TYPE_LABELS: Record<string, string> = {
  asr_transcription: 'ASR 转写',
  clean_transcript: '清洁稿生成',
  summary_generation: '纪要生成',
  extract_text: '文字提取',
  qa_answer: '问答生成',
  export: '导出',
};

const JOB_STATUS_LABELS: Record<string, string> = {
  queued: '排队中',
  running: '运行中',
  succeeded: '已完成',
  failed: '失败',
};

function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), Math.max(min, max));
}

function loadProjectColumnWidths(): ProjectColumnWidths {
  try {
    const saved = JSON.parse(localStorage.getItem(PROJECT_LAYOUT_STORAGE_KEY) || '{}');
    return {
      left: Number(saved.left) || PROJECT_LAYOUT_DEFAULT.left,
      right: Number(saved.right) || PROJECT_LAYOUT_DEFAULT.right
    };
  } catch {
    return PROJECT_LAYOUT_DEFAULT;
  }
}

function normalizeProjectColumnWidths(widths: ProjectColumnWidths, containerWidth: number): ProjectColumnWidths {
  const rightMax = Math.min(RIGHT_PANEL_MAX, containerWidth - LEFT_PANEL_MIN - MIDDLE_PANEL_MIN - RESIZE_HANDLE_TOTAL_WIDTH);
  const right = clamp(widths.right, RIGHT_PANEL_MIN, rightMax);
  const leftMax = Math.min(LEFT_PANEL_MAX, containerWidth - right - MIDDLE_PANEL_MIN - RESIZE_HANDLE_TOTAL_WIDTH);
  const left = clamp(widths.left, LEFT_PANEL_MIN, leftMax);
  return { left, right };
}

function normalizeRecordingStatus(status?: string): RecordingStatus | '' {
  if (!status) return '';
  return (RECORDING_STATUS_ALIASES[status] || status) as RecordingStatus;
}

function recordingStatusLabel(status?: string) {
  return RECORDING_STATUS_LABELS[status || ''] || RECORDING_STATUS_LABELS[normalizeRecordingStatus(status)] || status || '-';
}

function recordingStatusClass(status?: string) {
  return normalizeRecordingStatus(status) || status || 'unknown';
}

function jobTypeLabel(type: string) {
  return JOB_TYPE_LABELS[type] || type || '-';
}

function jobStatusLabel(status: string) {
  return JOB_STATUS_LABELS[status] || status || '-';
}

function isRecordingProcessing(status?: string) {
  const normalized = normalizeRecordingStatus(status);
  return Boolean(normalized && PROCESSING_RECORDING_STATUSES.has(normalized));
}

function isRecordingPlayable(status?: string) {
  const normalized = normalizeRecordingStatus(status);
  return Boolean(normalized && !['created', 'uploading', 'failed'].includes(normalized));
}

function isRecordingReadyForQa(status?: string) {
  return normalizeRecordingStatus(status) === 'completed';
}

function defaultQaSelection(recordings: Recording[]) {
  return recordings.filter((item) => isRecordingReadyForQa(item.status) && item.reference_status !== 'source_unshared' && item.reference_status !== 'source_deleted' && item.reference_status !== 'file_deleted').slice(0, 10).map(fileKey);
}

function fileKey(item: Recording) {
  return item.file_id || item.recording_id;
}

function isAudioFile(item?: Recording | null) {
  return !item?.file_type || item.file_type === 'audio';
}

function fileTypeLabel(type?: string) {
  return ({
    audio: '音频',
    pdf: 'PDF',
    excel: 'Excel',
    docx: 'Word',
    text: 'TXT',
    markdown: 'Markdown',
  } as Record<string, string>)[type || 'audio'] || type || '文件';
}

function elapsedSince(value?: string, now = Date.now()) {
  if (!value) return '';
  const start = new Date(value).getTime();
  if (Number.isNaN(start)) return '';
  const seconds = Math.max(0, Math.floor((now - start) / 1000));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function formatFileSize(bytes?: number) {
  if (!bytes) return '';
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
}

function recordingTimerStart(recording?: Recording | null) {
  return recording?.current_job_started_at || recording?.current_job_created_at || recording?.updated_at || recording?.created_at;
}

function isSameIdSet(left: string[], right: string[]) {
  return left.length === right.length && left.every((id) => right.includes(id));
}

type QaStreamEvent = { event: string; data: Record<string, any> };

async function postQaStream(threadId: string, payload: { file_ids: string[]; question: string }, onEvent: (event: QaStreamEvent) => void) {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(`/api/qa-threads/${threadId}/messages/stream`, {
    method: 'POST',
    headers,
    body: JSON.stringify(payload),
  });
  if (!response.ok || !response.body) {
    const data = await response.json().catch(() => null);
    throw new Error(data?.error?.message || data?.detail || '问题提交失败');
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    buffer = parseQaStreamBuffer(buffer, onEvent);
  }
  buffer += decoder.decode();
  parseQaStreamBuffer(buffer, onEvent, true);
}

function parseQaStreamBuffer(buffer: string, onEvent: (event: QaStreamEvent) => void, flush = false) {
  let normalized = buffer.replace(/\r\n/g, '\n');
  let index = normalized.indexOf('\n\n');
  while (index >= 0) {
    const block = normalized.slice(0, index);
    normalized = normalized.slice(index + 2);
    emitQaStreamBlock(block, onEvent);
    index = normalized.indexOf('\n\n');
  }
  if (flush && normalized.trim()) emitQaStreamBlock(normalized, onEvent);
  return normalized;
}

function emitQaStreamBlock(block: string, onEvent: (event: QaStreamEvent) => void) {
  const lines = block.split('\n');
  const event = lines.find((line) => line.startsWith('event:'))?.replace(/^event:\s*/, '') || 'message';
  const data = lines.filter((line) => line.startsWith('data:')).map((line) => line.replace(/^data:\s*/, '')).join('\n');
  if (!data) return;
  onEvent({ event, data: JSON.parse(data) });
}

function parseHash(): { view: 'home' | 'project' | 'settings' | 'admin'; projectId: string | null } {
  const hash = window.location.hash;
  const match = hash.match(/^#\/project\/([^/]+)$/);
  if (match) return { view: 'project', projectId: match[1] };
  if (hash === '#/settings') return { view: 'settings', projectId: null };
  if (hash === '#/admin') return { view: 'admin', projectId: null };
  return { view: 'home', projectId: null };
}

function App() {
  const [token, setTokenState] = useState(getToken());
  const [{ view, projectId }, setNav] = useState(parseHash);
  const [me, setMe] = useState<User | null>(null);
  const [usage, setUsage] = useState<MeUsage | null>(null);

  useEffect(() => {
    const onHashChange = () => setNav(parseHash());
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  const loadMe = useCallback(async () => {
    if (!getToken()) return;
    const [user, usageData] = await Promise.all([
      api<User>('/api/auth/me'),
      api<MeUsage>('/api/me/usage').catch(() => null),
    ]);
    setMe(user);
    if (usageData) setUsage(usageData);
  }, []);

  useEffect(() => { void loadMe().catch(() => undefined); }, [token, loadMe]);

  const navigate = (next: { view: 'home' | 'project' | 'settings' | 'admin'; projectId?: string }) => {
    if (next.view === 'project' && next.projectId) {
      window.location.hash = `#/project/${next.projectId}`;
    } else if (next.view === 'settings') {
      window.location.hash = '#/settings';
    } else if (next.view === 'admin') {
      window.location.hash = '#/admin';
    } else {
      window.location.hash = '';
    }
    setNav({ view: next.view, projectId: next.projectId ?? null });
  };

  if (!token) return <Login onLogin={(next) => { setToken(next); setTokenState(next); void loadMe(); }} />;

  return (
    <Layout className="app-shell">
      {view === 'home' && <Home me={me} usage={usage} onOpenProject={(id) => navigate({ view: 'project', projectId: id })} onSettings={() => navigate({ view: 'settings' })} onAdmin={() => navigate({ view: 'admin' })} onLogout={() => { clearToken(); setTokenState(''); setMe(null); setUsage(null); window.location.hash = ''; }} />}
      {view === 'project' && projectId && <ProjectPage projectId={projectId} onBack={() => navigate({ view: 'home' })} />}
      {view === 'settings' && <SettingsPage onBack={() => navigate({ view: 'home' })} />}
      {view === 'admin' && <AdminPage onBack={() => navigate({ view: 'home' })} />}
    </Layout>
  );
}

function Login({ onLogin }: { onLogin: (token: string) => void }) {
  const [loading, setLoading] = useState(false);
  const onFinish = async (values: { username: string; password: string }) => {
    setLoading(true);
    try {
      const data = await api<{ token: string }>('/api/auth/login', { method: 'POST', body: JSON.stringify(values) });
      onLogin(data.token);
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setLoading(false);
    }
  };
  return (
    <div className="login-page">
      <aside className="login-stage">
        <div className="login-stage-top">
          <div className="login-mark">
            <span className="login-mark-cn">中</span>
          </div>
          <div className="login-stage-brand">
            <div className="login-stage-brand-cn">中大咨询</div>
            <div className="login-stage-brand-en">MPGroup · Management Consulting</div>
          </div>
        </div>
        <div className="login-stage-center">
          <p className="login-quote-en">Listen to every strategic signal.</p>
          <h1 className="login-quote-cn">听见每一次访谈中的<br />战略信号。</h1>
          <div className="login-quote-divider" />
          <p className="login-quote-sub">中大咨询 · 顾问访谈智能工作台</p>
        </div>
        <div className="login-stage-bottom">
          <div className="login-stage-stat">
            <span className="login-stage-stat-num">1993</span>
            <span className="login-stage-stat-label">至今深耕管理咨询行业</span>
          </div>
          <div className="login-stage-stat">
            <span className="login-stage-stat-num">30+</span>
            <span className="login-stage-stat-label">服务行业领域</span>
          </div>
        </div>
      </aside>
      <section className="login-form-side">
        <div className="login-form-topbar">
          <span className="login-form-topbar-text">中文 / EN</span>
        </div>
        <div className="login-form-wrap">
          <div className="login-form-heading">
            <h2 className="login-form-title">洞见</h2>
            <p className="login-form-subtitle">MP&nbsp;Insight</p>
            <span className="login-form-rule" />
            <p className="login-form-hint">欢迎回来，请使用管理员分配的账号登录。<br />内测期间默认账号：<em>admin</em> / <em>mp2026</em></p>
          </div>
          <Form className="login-form" layout="vertical" initialValues={{ username: 'admin', password: 'mp2026' }} onFinish={onFinish}>
            <Form.Item name="username" label="账号" rules={[{ required: true, message: '请输入账号' }]}>
              <Input size="large" variant="borderless" className="login-input" placeholder="请输入账号" />
            </Form.Item>
            <Form.Item name="password" label="密码" rules={[{ required: true, message: '请输入密码' }]}>
              <Input.Password size="large" variant="borderless" className="login-input" placeholder="请输入密码" />
            </Form.Item>
            <Button type="primary" htmlType="submit" loading={loading} block size="large" className="login-submit">
              <span className="login-submit-text">登&nbsp;&nbsp;录</span>
              <span className="login-submit-arrow" aria-hidden>→</span>
            </Button>
          </Form>
          <div className="login-form-foot">
            <span>© 2026 中大咨询集团</span>
            <span className="login-form-foot-dot">·</span>
            <span>MP Insight 内测版本</span>
          </div>
        </div>
      </section>
    </div>
  );
}

function Home({ me, usage, onOpenProject, onSettings, onAdmin, onLogout }: { me: User | null; usage: MeUsage | null; onOpenProject: (id: string) => void; onSettings: () => void; onAdmin: () => void; onLogout: () => void }) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [keyword, setKeyword] = useState('');
  const [createOpen, setCreateOpen] = useState(false);
  const [editProject, setEditProject] = useState<Project | null>(null);
  const [projectName, setProjectName] = useState('');

  const load = useCallback(async () => {
    const data = await api<{ items: Project[] }>(`/api/projects?keyword=${encodeURIComponent(keyword)}`);
    setProjects(data.items);
  }, [keyword]);

  useEffect(() => { void load(); }, [load]);

  const createProject = async () => {
    if (!projectName.trim()) return message.warning('请输入项目名称');
    const project = await api<Project>('/api/projects', { method: 'POST', body: JSON.stringify({ title: projectName.trim() }) });
    setProjectName('');
    setCreateOpen(false);
    onOpenProject(project.project_id);
  };

  const updateProject = async () => {
    if (!editProject || !projectName.trim()) return;
    await api(`/api/projects/${editProject.project_id}`, { method: 'PATCH', body: JSON.stringify({ title: projectName.trim() }) });
    setEditProject(null);
    setProjectName('');
    void load();
  };

  const deleteProject = (project: Project) => {
    Modal.confirm({
      title: '确认硬删除项目？',
      content: '将删除项目下所有录音、文件、纪要和问答历史。',
      okText: '确认删除',
      okButtonProps: { danger: true },
      onOk: async () => { await api(`/api/projects/${project.project_id}`, { method: 'DELETE' }); message.success('项目已删除'); void load(); }
    });
  };

  const actionMenu = (project: Project): MenuProps => ({
    items: [
      { key: 'rename', label: '修改项目名称' },
      { key: 'delete', label: '删除项目', danger: true }
    ],
    onClick: ({ key, domEvent }) => {
      domEvent.stopPropagation();
      if (key === 'rename') { setEditProject(project); setProjectName(project.title); }
      if (key === 'delete') deleteProject(project);
    }
  });

  const sortedProjects = useMemo(
    () => [...projects].sort((a, b) => new Date(b.updated_at || '').getTime() - new Date(a.updated_at || '').getTime()),
    [projects],
  );

  return (
    <div className="home-shell">
      <header className="home-topbar">
        <div className="home-topbar-brand">
          <div className="home-topbar-logo">中</div>
          <span className="home-topbar-title">洞见 · MP Insight</span>
        </div>
        <div className="home-topbar-actions">
          <button className="topbar-btn" onClick={onSettings}><SettingOutlined /> 设置</button>
          <UserUsageDropdown me={me} usage={usage} onAdmin={onAdmin} onLogout={onLogout} />
        </div>
      </header>

      <div className="home-hero">
        <div className="home-hero-inner">
          <h1 className="home-hero-title">洞见 · 顾问访谈智能工作台</h1>
          <p className="home-hero-sub">上传访谈录音，自动转写、生成纪要，并基于内容进行智能问答</p>
          <div className="home-search-row">
            <Input.Search
              className="home-search-antd"
              placeholder="搜索项目......"
              allowClear
              onSearch={setKeyword}
              onChange={(e) => { if (!e.target.value) setKeyword(''); }}
            />
            <button className="home-new-btn" onClick={() => { setProjectName(''); setCreateOpen(true); }}>
              <span>+</span> 新建项目
            </button>
          </div>
        </div>
      </div>

      <div className="home-body">
        {projects.length === 0 ? (
          <div className="home-empty">
            <div className="home-empty-icon">
              <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M3 12a9 9 0 1 0 18 0 9 9 0 0 0-18 0"/><path d="M12 8v4"/><path d="M12 16h.01"/>
              </svg>
            </div>
            <p className="home-empty-title">还没有项目</p>
            <p className="home-empty-sub">点击「新建项目」开始你的第一个录音分析项目</p>
          </div>
        ) : (
          <Table
            rowKey="project_id"
            className="project-table home-project-table"
            dataSource={sortedProjects}
            pagination={false}
            onRow={(record) => ({ onClick: () => onOpenProject(record.project_id) })}
            columns={[
              { title: '项目', dataIndex: 'title', render: (value: string) => <Text strong>{value}</Text> },
              { title: '文件数量', dataIndex: 'file_count', width: 120, render: (value: number, record: Project) => value ?? record.recording_count ?? 0 },
              { title: '总时长(h)', dataIndex: 'total_duration_seconds', width: 140, render: (value: number) => ((value || 0) / 3600).toFixed(1) },
              { title: '最近更新', dataIndex: 'updated_at', width: 160, render: formatDate },
              { title: '', width: 60, render: (_, record) => <Dropdown menu={actionMenu(record)} trigger={['click']}><Button type="text" icon={<MoreOutlined />} onClick={(e) => e.stopPropagation()} /></Dropdown> },
            ]}
          />
        )}
      </div>

      <Modal
        title="新建项目"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={createProject}
        okText="确认创建 →"
        cancelText="取消"
        width={480}
        okButtonProps={{ disabled: !projectName.trim() || projectName.length > 50 }}
      >
        <div className="project-modal-body">
          <div className="project-modal-label">项目名称</div>
          <Input
            size="large"
            placeholder="例：2025年Q2用户访谈"
            value={projectName}
            onChange={(e) => setProjectName(e.target.value)}
            onPressEnter={createProject}
            maxLength={50}
            showCount
          />
        </div>
      </Modal>
      <Modal
        title="修改项目名称"
        open={!!editProject}
        onCancel={() => setEditProject(null)}
        onOk={updateProject}
        okText="保存修改"
        cancelText="取消"
        width={480}
        okButtonProps={{ disabled: !projectName.trim() || projectName.length > 50 }}
      >
        <div className="project-modal-body">
          <div className="project-modal-label">项目名称</div>
          <Input
            size="large"
            placeholder="输入项目名称"
            value={projectName}
            onChange={(e) => setProjectName(e.target.value)}
            onPressEnter={updateProject}
            maxLength={50}
            showCount
          />
        </div>
      </Modal>
    </div>
  );
}

function UserUsageDropdown({ me, usage, onAdmin, onLogout }: { me: User | null; usage: MeUsage | null; onAdmin: () => void; onLogout: () => void }) {
  const quota = usage?.quota;
  const dailyAsrLimit = quota?.daily_asr_seconds || 0;
  const monthlyAsrLimit = quota?.monthly_asr_seconds || 0;
  const dailyQaLimit = quota?.daily_qa_tokens || 0;
  const monthlyQaLimit = quota?.monthly_qa_tokens || 0;
  const card = <div className="usage-dropdown-card">
    <Text strong>{me?.display_name || me?.username || '当前用户'}</Text>
    <Text type="secondary">{me?.role === 'admin' ? '管理员' : '普通用户'}</Text>
    <UsageProgress label="今日 ASR" value={usage?.today.asr_seconds || 0} limit={dailyAsrLimit} formatter={formatDuration} />
    <UsageProgress label="本月 ASR" value={usage?.month.asr_seconds || 0} limit={monthlyAsrLimit} formatter={formatDuration} />
    <UsageProgress label="今日问答 Token" value={usage?.today.qa_tokens || 0} limit={dailyQaLimit} />
    <UsageProgress label="本月问答 Token" value={usage?.month.qa_tokens || 0} limit={monthlyQaLimit} />
    <Space direction="vertical" style={{ width: '100%' }}>
      {me?.role === 'admin' && <Button block onClick={onAdmin}>管理后台</Button>}
      <Button block onClick={onLogout}>退出</Button>
    </Space>
  </div>;
  return <Dropdown dropdownRender={() => card} trigger={['click']} placement="bottomRight">
    <button className="topbar-btn topbar-btn-ghost">{me?.display_name || me?.username || '用户'} <DownOutlined /></button>
  </Dropdown>;
}

function UsageProgress({ label, value, limit, formatter }: { label: string; value: number; limit: number; formatter?: (value: number) => string }) {
  const percent = limit ? Math.min(100, Math.round((value / limit) * 100)) : 0;
  const valueLabel = formatter ? formatter(value) : `${value}`;
  const limitLabel = limit ? (formatter ? formatter(limit) : `${limit}`) : '不限';
  return <div className="usage-progress-row">
    <div className="usage-progress-label"><Text>{label}</Text><Text type="secondary">{valueLabel} / {limitLabel}</Text></div>
    <Progress percent={percent} size="small" showInfo={false} status={limit && percent >= 90 ? 'exception' : 'normal'} />
  </div>;
}

function ProjectPage({ projectId, onBack }: { projectId: string; onBack: () => void }) {
  const [project, setProject] = useState<Project | null>(null);
  const [recordings, setRecordings] = useState<Recording[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [checkedIds, setCheckedIds] = useState<string[]>([]);
  const [segments, setSegments] = useState<TranscriptSegment[]>([]);
  const [extractedText, setExtractedText] = useState('');
  const [fileDetail, setFileDetail] = useState<Recording | null>(null);
  const [showRaw, setShowRaw] = useState(false);
  const [summary, setSummary] = useState<any>(null);
  const [threads, setThreads] = useState<QAThread[]>([]);
  const [currentThreadId, setCurrentThreadId] = useState<string | null>(null);
  const [messages, setMessages] = useState<QAMessage[]>([]);
  const [qaQuestion, setQaQuestion] = useState('');
  const [qaSubmitting, setQaSubmitting] = useState(false);
  const [qaStreaming, setQaStreaming] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [queueOpen, setQueueOpen] = useState(false);
  const [membersOpen, setMembersOpen] = useState(false);
  const [sharedOpen, setSharedOpen] = useState(false);
  const [columnWidths, setColumnWidths] = useState<ProjectColumnWidths>(loadProjectColumnWidths);
  const [activeResize, setActiveResize] = useState<ResizeDivider | null>(null);
  const [clockNow, setClockNow] = useState(() => Date.now());
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const workspaceRef = useRef<HTMLDivElement | null>(null);
  const selectionTouchedRef = useRef(false);
  const restoredThreadSelectionRef = useRef<string | null>(null);
  const qaSelectionWarnedRef = useRef(false);

  const selectedRecording = recordings.find((item) => fileKey(item) === selectedId) || null;
  const selectedRecordingId = selectedRecording?.recording_id || null;
  const selectedRecordingPlayable = isAudioFile(selectedRecording) && isRecordingPlayable(selectedRecording?.status);
  const pendingQaAnswer = messages.some((item) => item.role === 'assistant' && ['queued', 'running'].includes(item.status));

  const loadProject = useCallback(async () => {
    const [p, r] = await Promise.all([
      api<Project>(`/api/projects/${projectId}`),
      api<{ items: Recording[] }>(`/api/projects/${projectId}/files?page_size=100`)
    ]);
    setProject(p);
    setRecordings(r.items);
    if (!selectedId && r.items.length) setSelectedId(fileKey(r.items[0]));
    const completedIds = new Set(r.items.filter((item) => isRecordingReadyForQa(item.status)).map(fileKey));
    const defaultIds = defaultQaSelection(r.items);
    if (!selectionTouchedRef.current) {
      setCheckedIds((prev) => isSameIdSet(prev, defaultIds) ? prev : defaultIds);
    } else {
      setCheckedIds((prev) => prev.filter((id) => completedIds.has(id)).slice(0, 10));
    }
  }, [projectId, selectedId]);

  const loadSelected = useCallback(async () => {
    if (!selectedId || !selectedRecording) return;
    setExtractedText('');
    setFileDetail(selectedRecording);
    if (isAudioFile(selectedRecording) && selectedRecording.recording_id) {
      const [transcript, sum] = await Promise.all([
        api<{ segments: TranscriptSegment[] }>(`/api/recordings/${selectedRecording.recording_id}/transcript?source=clean`),
        api<any>(`/api/recordings/${selectedRecording.recording_id}/summary`)
      ]);
      setSegments(transcript.segments || []);
      setSummary(sum);
      return;
    }
    setSegments([]);
    setSummary(null);
    const detail = await api<Recording & { extracted_text: string }>(`/api/files/${selectedRecording.file_id}/extracted-text`);
    setFileDetail(detail);
    setExtractedText(detail.extracted_text || '');
  }, [selectedId, selectedRecording?.file_id, selectedRecording?.recording_id, selectedRecording?.status]);

  const loadThreads = useCallback(async () => {
    const data = await api<{ items: QAThread[] }>(`/api/projects/${projectId}/qa-threads`);
    setThreads(data.items);
    if (!currentThreadId && data.items.length) setCurrentThreadId(data.items[0].thread_id);
  }, [projectId, currentThreadId]);

  const loadThread = useCallback(async () => {
    if (!currentThreadId) { setMessages([]); return; }
    const data = await api<QAThread>(`/api/qa-threads/${currentThreadId}`);
    const nextMessages = data.messages || [];
    setMessages(nextMessages);
    if (restoredThreadSelectionRef.current !== currentThreadId) {
      const lastMessage = [...nextMessages].reverse().find((item) => item.selected_file_ids?.length || item.selected_recording_ids?.length);
      const lastSelection = lastMessage?.selected_file_ids?.length ? lastMessage.selected_file_ids : (lastMessage?.selected_recording_ids || []);
      if (lastSelection.length) {
        selectionTouchedRef.current = true;
        setCheckedIds(lastSelection.slice(0, 10));
      }
      restoredThreadSelectionRef.current = currentThreadId;
    }
  }, [currentThreadId]);

  useEffect(() => { void loadProject(); }, [loadProject]);
  useEffect(() => { void loadSelected(); }, [loadSelected]);
  useEffect(() => { void loadThreads(); }, [loadThreads]);
  useEffect(() => { void loadThread(); }, [loadThread]);

  useEffect(() => {
    const hasRunningRecording = recordings.some((item) => isRecordingProcessing(item.status));
    const hasRunningMessage = !qaStreaming && messages.some((item) => ['queued', 'running'].includes(item.status));
    if (!hasRunningRecording && !hasRunningMessage) return;
    const timer = window.setInterval(() => {
      void loadProject();
      void loadSelected();
      void loadThread();
    }, 3000);
    return () => window.clearInterval(timer);
  }, [recordings, messages, qaStreaming, loadProject, loadSelected, loadThread]);

  useEffect(() => {
    const hasRunningRecording = recordings.some((item) => isRecordingProcessing(item.status));
    const hasRunningMessage = messages.some((item) => ['queued', 'running'].includes(item.status));
    if (!hasRunningRecording && !hasRunningMessage) return;
    setClockNow(Date.now());
    const timer = window.setInterval(() => setClockNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [recordings, messages]);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.pause();
    audio.removeAttribute('src');
    audio.load();
    if (!selectedRecordingId || !selectedRecordingPlayable) return;
    let cancelled = false;
    api<{ url: string }>(`/api/recordings/${selectedRecordingId}/play-url`, { method: 'POST' })
      .then((data) => {
        if (cancelled || !audioRef.current) return;
        audioRef.current.src = data.url;
        audioRef.current.load();
      })
      .catch(() => undefined);
    return () => { cancelled = true; };
  }, [selectedRecordingId, selectedRecordingPlayable]);

  useEffect(() => {
    const normalizeToContainer = () => {
      const width = workspaceRef.current?.getBoundingClientRect().width;
      if (!width) return;
      setColumnWidths((prev) => {
        const next = normalizeProjectColumnWidths(prev, width);
        return next.left === prev.left && next.right === prev.right ? prev : next;
      });
    };
    normalizeToContainer();
    window.addEventListener('resize', normalizeToContainer);
    return () => window.removeEventListener('resize', normalizeToContainer);
  }, []);

  useEffect(() => {
    localStorage.setItem(PROJECT_LAYOUT_STORAGE_KEY, JSON.stringify(columnWidths));
  }, [columnWidths]);

  const startColumnResize = useCallback((divider: ResizeDivider, event: ReactPointerEvent<HTMLDivElement>) => {
    const container = workspaceRef.current;
    if (!container) return;
    event.preventDefault();
    const rect = container.getBoundingClientRect();
    const startX = event.clientX;
    const startWidths = columnWidths;
    setActiveResize(divider);
    document.body.classList.add('is-resizing-columns');

    const onPointerMove = (moveEvent: PointerEvent) => {
      moveEvent.preventDefault();
      const deltaX = moveEvent.clientX - startX;
      const next = divider === 'left'
        ? { left: startWidths.left + deltaX, right: startWidths.right }
        : { left: startWidths.left, right: startWidths.right - deltaX };
      setColumnWidths(normalizeProjectColumnWidths(next, rect.width));
    };

    const stopResize = () => {
      setActiveResize(null);
      document.body.classList.remove('is-resizing-columns');
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('pointerup', stopResize);
      window.removeEventListener('pointercancel', stopResize);
    };

    window.addEventListener('pointermove', onPointerMove, { passive: false });
    window.addEventListener('pointerup', stopResize);
    window.addEventListener('pointercancel', stopResize);
  }, [columnWidths]);

  const resetColumnWidths = useCallback(() => {
    setColumnWidths(PROJECT_LAYOUT_DEFAULT);
  }, []);

  const workspaceStyle = {
    '--left-panel-width': `${columnWidths.left}px`,
    '--right-panel-width': `${columnWidths.right}px`
  } as CSSProperties;

  const jumpTo = async (ms: number) => {
    if (!audioRef.current || !selectedRecording?.recording_id || !isAudioFile(selectedRecording)) return;
    if (!audioRef.current.src) {
      const data = await api<{ url: string }>(`/api/recordings/${selectedRecording.recording_id}/play-url`, { method: 'POST' });
      audioRef.current.src = data.url;
    }
    audioRef.current.currentTime = ms / 1000;
    void audioRef.current.play();
  };

  const saveSegment = async (seg: TranscriptSegment, options?: SegmentSaveOptions) => {
    const result = await api<SegmentUpdateResult>(`/api/transcript-segments/${seg.segment_id}`, {
      method: 'PATCH',
      body: JSON.stringify({
        speaker: seg.speaker,
        text: seg.text,
        // Backend only performs "replace all same speaker" when this snake_case flag is true.
        replace_same_speaker: !!options?.replaceSameSpeaker,
      }),
    });
    if (selectedId) {
      setRecordings((prev) => prev.map((item) => fileKey(item) === selectedId ? { ...item, summary_stale: true } : item));
    }
    setSummary((prev: any) => prev ? { ...prev, stale: true } : prev);
    message.success(result.updated_count && result.updated_count > 1 ? `已保存并替换 ${result.updated_count} 段，纪要已标记为过期` : '已保存，纪要已标记为过期');
    void loadProject();
    void loadSelected();
  };

  const regenerateSummary = async () => {
    if (!selectedRecording?.recording_id || !isAudioFile(selectedRecording)) return;
    await api(`/api/recordings/${selectedRecording.recording_id}/summary/regenerate`, { method: 'POST', body: JSON.stringify({}) });
    message.success('已提交重新生成纪要任务');
    setTimeout(() => { void loadProject(); void loadSelected(); }, 1000);
  };

  const createThread = async () => {
    const data = await api<QAThread>(`/api/projects/${projectId}/qa-threads`, { method: 'POST', body: JSON.stringify({}) });
    setCurrentThreadId(data.thread_id);
    restoredThreadSelectionRef.current = data.thread_id;
    selectionTouchedRef.current = false;
    setCheckedIds(defaultQaSelection(recordings));
    setMessages([]);
    void loadThreads();
  };

  const ask = async () => {
    const question = qaQuestion.trim();
    if (!question) return message.warning('请输入问题');
    if (pendingQaAnswer) return message.warning('AI 正在回答，完成后可继续提问');
    if (qaSubmitting) return;
    const readyIds = new Set(recordings.filter((item) => isRecordingReadyForQa(item.status)).map(fileKey));
    const qaFileIds = checkedIds.filter((id) => readyIds.has(id)).slice(0, 10);
    if (!qaFileIds.length) return message.warning('请先勾选已处理完成的文件');
    const completedCount = readyIds.size;
    const defaultFirstTen = defaultQaSelection(recordings);
    const isUsingDefaultFirstTen = completedCount > 10 && isSameIdSet(qaFileIds, defaultFirstTen);
    if (!qaSelectionWarnedRef.current && isUsingDefaultFirstTen) {
      Modal.confirm({
        title: '确认参考文件范围',
        content: `当前项目共有 ${completedCount} 份已完成录音，本次仅使用默认勾选的前 10 份作为参考材料。为了提高回答准确性，建议你根据问题精准勾选相关录音。是否继续发送？`,
        okText: '继续发送',
        cancelText: '我去调整',
        onOk: () => { qaSelectionWarnedRef.current = true; void ask(); },
      });
      return;
    }
    let threadId = currentThreadId;
    if (!threadId) {
      const thread = await api<QAThread>(`/api/projects/${projectId}/qa-threads`, { method: 'POST', body: JSON.stringify({}) });
      threadId = thread.thread_id;
      setCurrentThreadId(threadId);
    }
    const now = new Date().toISOString();
    const tmpUserId = `tmp_user_${Date.now()}`;
    const tmpAiId = `tmp_ai_${Date.now()}`;
    let userMessageId = tmpUserId;
    let assistantMessageId = tmpAiId;
    setQaQuestion('');
    setMessages((prev) => [
      ...prev,
      { message_id: tmpUserId, thread_id: threadId!, role: 'user', content: question, selected_recording_ids: [], selected_file_ids: qaFileIds, sources: [], status: 'ready', created_at: now },
      { message_id: tmpAiId, thread_id: threadId!, role: 'assistant', content: '', reasoning_content: '', selected_recording_ids: [], selected_file_ids: qaFileIds, sources: [], status: 'running', created_at: now },
    ]);
    setQaSubmitting(true);
    setQaStreaming(true);
    try {
      await postQaStream(threadId, { file_ids: qaFileIds, question }, ({ event, data }) => {
        if (event === 'created') {
          userMessageId = String(data.user_message_id || userMessageId);
          assistantMessageId = String(data.assistant_message_id || assistantMessageId);
          setMessages((prev) => prev.map((item) => {
            if (item.message_id === tmpUserId) return { ...item, message_id: userMessageId };
            if (item.message_id === tmpAiId) return { ...item, message_id: assistantMessageId, status: 'running' };
            return item;
          }));
          setQaSubmitting(false);
          return;
        }
        if (event === 'reasoning' || event === 'content') {
          setMessages((prev) => prev.map((item) => {
            if (item.message_id !== assistantMessageId) return item;
            if (event === 'reasoning') return { ...item, status: 'running', reasoning_content: `${item.reasoning_content || ''}${data.delta || ''}` };
            return { ...item, status: 'running', content: `${item.content || ''}${data.delta || ''}` };
          }));
          return;
        }
        if (event === 'done') {
          setMessages((prev) => prev.map((item) => item.message_id === assistantMessageId ? { ...item, status: 'ready', content: String(data.content || item.content || ''), reasoning_content: String(data.reasoning_content || item.reasoning_content || '') } : item));
          return;
        }
        if (event === 'error') {
          const errorMessage = String(data.message || 'AI 回答失败');
          setMessages((prev) => prev.map((item) => item.message_id === assistantMessageId ? { ...item, status: 'failed', content: item.content || errorMessage } : item));
          throw new Error(errorMessage);
        }
      });
      void loadThreads();
      void loadThread();
    } catch (error) {
      message.error(error instanceof Error ? error.message : '问题提交失败');
      setMessages((prev) => prev.map((item) => item.message_id === assistantMessageId ? { ...item, status: 'failed' } : item));
      void loadThread();
    } finally {
      setQaSubmitting(false);
      setQaStreaming(false);
    }
  };

  const deleteRecording = async (id: string) => {
    Modal.confirm({ title: '确认硬删除文件？', content: '将同时删除原始文件、处理结果和相关任务。若被其他项目引用，需在提示后再次确认。', okText: '确认删除', okButtonProps: { danger: true }, onOk: async () => { const deleted = await deleteFileWithConfirm(id); if (deleted) { message.success('已删除'); setSelectedId(null); void loadProject(); } } });
  };

  const renameRecording = async (id: string, fileName: string) => {
    const nextName = fileName.trim();
    if (!nextName) return message.warning('文件名称不能为空');
    const file = recordings.find((item) => fileKey(item) === id);
    if (file?.recording_id && isAudioFile(file)) {
      await api(`/api/recordings/${file.recording_id}`, { method: 'PATCH', body: JSON.stringify({ file_name: nextName }) });
    } else {
      await api(`/api/files/${id}`, { method: 'PATCH', body: JSON.stringify({ file_name: nextName }) });
    }
    message.success('文件名已保存');
    void loadProject();
  };

  const deleteFileWithConfirm = async (id: string, force = false): Promise<boolean> => {
    try {
      await api(`/api/files/${id}${force ? '?force=true' : ''}`, { method: 'DELETE' });
      return true;
    } catch (err) {
      const text = err instanceof Error ? err.message : '';
      if (!force && text.includes('引用')) {
        return new Promise((resolve) => {
          Modal.confirm({
            title: '该文件已被其他项目引用',
            content: '删除后，其他项目中的引用文件将不可用。是否继续？',
            okText: '继续删除',
            okButtonProps: { danger: true },
            onOk: async () => { resolve(await deleteFileWithConfirm(id, true)); },
            onCancel: () => resolve(false),
          });
        });
      }
      throw err;
    }
  };

  const retryFailedRecording = async (jobId?: string) => {
    if (!jobId) return message.warning('没有可重试的失败任务');
    await api(`/api/jobs/${jobId}/retry`, { method: 'POST' });
    message.success('已提交重试');
    void loadProject();
    void loadSelected();
  };

  const toggleRecordingCheck = (id: string, checked: boolean) => {
    selectionTouchedRef.current = true;
    setCheckedIds((prev) => {
      if (!checked) return prev.filter((item) => item !== id);
      if (prev.includes(id)) return prev;
      const recording = recordings.find((item) => fileKey(item) === id);
      if (!isRecordingReadyForQa(recording?.status)) {
        message.warning('只有处理完成的文件可以用于问答');
        return prev;
      }
      if (prev.length >= 10) {
        message.warning('最多选择 10 份文件用于问答');
        return prev;
      }
      return [...prev, id];
    });
  };

  const deleteProject = () => {
    const doDelete = async (force = false) => {
      try {
        await api(`/api/projects/${projectId}${force ? '?force=true' : ''}`, { method: 'DELETE' });
        message.success('项目已删除');
        onBack();
      } catch (err) {
        const text = err instanceof Error ? err.message : '';
        if (!force && text.includes('引用')) {
          Modal.confirm({ title: '该项目文件已被引用', content: '删除后，其他项目中的引用文件将不可用。是否继续？', okText: '继续删除', okButtonProps: { danger: true }, onOk: () => doDelete(true) });
          return;
        }
        message.error(text || '删除失败');
      }
    };
    Modal.confirm({ title: '确认硬删除项目？', content: '将删除项目下所有录音、文件、纪要和问答历史。', okText: '确认删除', okButtonProps: { danger: true }, onOk: () => doDelete(false) });
  };

  const exportMd = async (type: 'summary' | 'transcript') => {
    if (!selectedRecording?.recording_id || !isAudioFile(selectedRecording)) return;
    const data = await api<{ download_url: string; filename?: string; content?: string }>(`/api/recordings/${selectedRecording.recording_id}/exports`, { method: 'POST', body: JSON.stringify({ export_type: type, format: 'markdown' }) });
    if (data.content !== undefined) {
      downloadTextFile(data.content, data.filename || `${type}.md`);
      return;
    }
    window.open(data.download_url, '_blank');
  };

  const projectMoreMenu: MenuProps = {
    items: [
      { key: 'delete', label: '删除项目', danger: true, icon: <DeleteOutlined /> }
    ],
    onClick: ({ key }) => {
      if (key === 'delete') deleteProject();
    }
  };

  return (
    <div className="project-shell">
      <div className="project-title">
        <Space>
          <Button onClick={onBack}>← 返回首页</Button>
          <Title level={4} style={{ margin: 0 }}>{project?.title || '项目'}</Title>
        </Space>
        <Dropdown menu={projectMoreMenu} trigger={['click']} placement="bottomRight">
          <Button icon={<MoreOutlined />}>更多操作</Button>
        </Dropdown>
      </div>
      <div ref={workspaceRef} className={`workspace-grid ${activeResize ? 'resizing' : ''}`} style={workspaceStyle}>
        <aside className="left-panel panel-scroll">
          <Space className="panel-actions"><Button type="primary" icon={<UploadOutlined />} onClick={() => setUploadOpen(true)}>上传文件</Button><Button onClick={() => setQueueOpen(true)}>处理队列</Button><Button onClick={() => setMembersOpen(true)}>成员</Button><Button onClick={() => setSharedOpen(true)}>共享文件</Button></Space>
          <Text type="secondary">文件数量 {recordings.length}/30</Text>
          {recordings.length >= 30 && <Tag color="orange">已达到建议上限</Tag>}
          <List dataSource={recordings} locale={{ emptyText: '暂无文件' }} renderItem={(rec) => (
            <RecordingListItem
              recording={rec}
              active={fileKey(rec) === selectedId}
              checked={checkedIds.includes(fileKey(rec))}
              checkDisabled={!isRecordingReadyForQa(rec.status) || ['source_unshared', 'source_deleted', 'file_deleted'].includes(rec.reference_status || '')}
              clockNow={clockNow}
              onSelect={() => setSelectedId(fileKey(rec))}
              onCheck={(checked) => toggleRecordingCheck(fileKey(rec), checked)}
              onRename={(name) => renameRecording(fileKey(rec), name)}
              onRetry={(jobId) => retryFailedRecording(jobId)}
              onDelete={() => deleteRecording(fileKey(rec))}
            />
          )} />
          <Text type="secondary">已选 {checkedIds.length} / 最多 10 份用于问答</Text>
        </aside>
        <ColumnResizeHandle side="left" active={activeResize === 'left'} onPointerDown={(event) => startColumnResize('left', event)} onDoubleClick={resetColumnWidths} />
        <main className="middle-panel">
          <div className="middle-toolbar">
            <div className="middle-toolbar-info">
              <Text strong className="toolbar-filename">{selectedRecording?.file_name || '请选择文件'}</Text>
              {selectedRecording && (
                <span className={`status-dot-label status-${recordingStatusClass(selectedRecording.status)}`}>
                  <span className="status-dot" />
                  {recordingStatusLabel(selectedRecording.status)}
                  {isRecordingProcessing(selectedRecording.status) && ` · 已处理 ${elapsedSince(recordingTimerStart(selectedRecording), clockNow)}`}
                </span>
              )}
            </div>
            <Space size={6} className="middle-toolbar-actions">
              {isAudioFile(selectedRecording) && <Button size="small" onClick={() => setShowRaw((v) => !v)}>{showRaw ? '隐藏原始稿' : '显示原始稿'}</Button>}
              {isAudioFile(selectedRecording) && <Button size="small" icon={<DownloadOutlined />} onClick={() => exportMd('transcript')}>导出清洁稿</Button>}
            </Space>
          </div>
          <div className="transcript-list panel-scroll">
            {isAudioFile(selectedRecording)
              ? segments.map((seg) => <SegmentEditor key={seg.segment_id} segment={seg} showRaw={showRaw} onJump={jumpTo} onSave={saveSegment} />)
              : <ExtractedTextView text={extractedText} file={fileDetail || selectedRecording} />}
          </div>
          <div className="player"><audio ref={audioRef} controls /><Text type="secondary" className="player-hint">{selectedRecording ? selectedRecordingPlayable ? '原始音频可播放' : selectedRecording.status === 'uploading' ? '上传完成后可播放' : '暂无可播放音频' : '请选择录音'}</Text></div>
        </main>
        <ColumnResizeHandle side="right" active={activeResize === 'right'} onPointerDown={(event) => startColumnResize('right', event)} onDoubleClick={resetColumnWidths} />
        <aside className="right-panel panel-scroll">
          <Tabs defaultActiveKey="summary" items={[
            { key: 'summary', label: isAudioFile(selectedRecording) ? '纪要' : '文件信息', children: isAudioFile(selectedRecording) ? <SummaryView summary={summary} stale={selectedRecording?.summary_stale || summary?.stale} onExport={() => exportMd('summary')} onRegenerate={regenerateSummary} /> : <FileInfoView file={fileDetail || selectedRecording} /> },
            { key: 'qa', label: '问答', children: <QAView checked={checkedIds} recordings={recordings} threads={threads} currentThreadId={currentThreadId} setCurrentThreadId={setCurrentThreadId} messages={messages} question={qaQuestion} setQuestion={setQaQuestion} onAsk={ask} onNewThread={createThread} submitting={qaSubmitting} waitingForAnswer={pendingQaAnswer} /> }
          ]} />
        </aside>
      </div>
      <UploadModal open={uploadOpen} projectId={projectId} onClose={() => setUploadOpen(false)} onCreated={(id) => { setSelectedId(id); void loadProject(); }} onDone={() => { setUploadOpen(false); void loadProject(); }} />
      <QueueModal open={queueOpen} projectId={projectId} clockNow={clockNow} onClose={() => setQueueOpen(false)} onRefresh={() => { void loadProject(); void loadSelected(); }} />
      <MembersModal open={membersOpen} projectId={projectId} project={project} onClose={() => setMembersOpen(false)} onRefresh={() => { void loadProject(); }} />
      <SharedFilesModal open={sharedOpen} projectId={projectId} onClose={() => setSharedOpen(false)} onAdded={() => { void loadProject(); }} />
    </div>
  );
}

function ColumnResizeHandle({ side, active, onPointerDown, onDoubleClick }: { side: ResizeDivider; active: boolean; onPointerDown: (event: ReactPointerEvent<HTMLDivElement>) => void; onDoubleClick: () => void }) {
  return <div
    role="separator"
    aria-orientation="vertical"
    aria-label={side === 'left' ? '拖拽调整左侧文件栏宽度' : '拖拽调整右侧功能栏宽度'}
    className={active ? 'resize-handle active' : 'resize-handle'}
    onPointerDown={onPointerDown}
    onDoubleClick={onDoubleClick}
    title="拖拽调整栏目宽度，双击恢复默认宽度"
  />;
}

function RecordingListItem({ recording, active, checked, checkDisabled, clockNow, onSelect, onCheck, onRename, onRetry, onDelete }: { recording: Recording; active: boolean; checked: boolean; checkDisabled: boolean; clockNow: number; onSelect: () => void; onCheck: (checked: boolean) => void; onRename: (name: string) => void; onRetry: (jobId?: string) => void; onDelete: () => void }) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(recording.file_name);
  const skipBlurSaveRef = useRef(false);
  const submittedNameRef = useRef('');
  useEffect(() => {
    setName(recording.file_name);
    submittedNameRef.current = '';
  }, [recording.file_name]);
  const failedStage = recording.latest_failed_job_type ? jobTypeLabel(recording.latest_failed_job_type) : '';
  const mediaMeta = [
    fileTypeLabel(recording.file_type),
    isAudioFile(recording) && recording.duration_seconds ? `音频时长 ${formatDuration(recording.duration_seconds)}` : '',
    !isAudioFile(recording) && recording.extracted_char_count ? `提取 ${recording.extracted_char_count} 字` : '',
    formatFileSize(recording.file_size_bytes),
    recording.source === 'reference' ? '引用文件' : '',
  ].filter(Boolean).join(' · ');
  const unavailable = ['source_unshared', 'source_deleted', 'file_deleted'].includes(recording.reference_status || '');
  const save = () => {
    if (skipBlurSaveRef.current) {
      skipBlurSaveRef.current = false;
      return;
    }
    const next = name.trim();
    if (!next || next === recording.file_name) {
      setEditing(false);
      setName(recording.file_name);
      return;
    }
    if (submittedNameRef.current === next) return;
    submittedNameRef.current = next;
    setEditing(false);
    onRename(next);
  };
  const cancelEdit = () => {
    skipBlurSaveRef.current = true;
    setEditing(false);
    setName(recording.file_name);
    window.setTimeout(() => { skipBlurSaveRef.current = false; }, 0);
  };
  return <List.Item className={active ? 'recording active' : 'recording'} onClick={onSelect}>
    <div className="recording-row">
      <Checkbox checked={checked} disabled={checkDisabled} onClick={(e) => e.stopPropagation()} onChange={(e) => onCheck(e.target.checked)} />
      <div className="recording-main">
        <div className="recording-name" onClick={(e) => e.stopPropagation()}>
          {editing ? <Input size="small" value={name} autoFocus onFocus={(e) => e.target.select()} onBlur={save} onChange={(e) => setName(e.target.value)} onPressEnter={save} onKeyDown={(e) => { if (e.key === 'Escape') cancelEdit(); }} /> : <><Text strong className="recording-filename">{recording.file_name}</Text><Button size="small" type="text" icon={<EditOutlined />} onClick={() => setEditing(true)} /></>}
        </div>
        <Space wrap className="recording-status-line">
          <span className={`status-dot-label status-${recordingStatusClass(recording.status)}`}>
            <span className="status-dot" />
            {recordingStatusLabel(recording.status)}
          </span>
          {isRecordingProcessing(recording.status) && <Text type="secondary">已处理 {elapsedSince(recordingTimerStart(recording), clockNow)}</Text>}
          {recording.status === 'failed' && failedStage && <Tag color="red">失败阶段：{failedStage}</Tag>}
        </Space>
        {unavailable && <Tag color="red">来源已不可用</Tag>}
        {recording.status === 'failed' && recording.latest_failed_job_error_message && <Text type="secondary" className="recording-error" title={recording.latest_failed_job_error_message}>{recording.latest_failed_job_error_message}</Text>}
        <Text type="secondary" className="recording-meta">{mediaMeta}</Text>
        {recording.status === 'failed' && <Button size="small" type="link" className="recording-retry" onClick={(e) => { e.stopPropagation(); void onRetry(recording.latest_failed_job_id); }}>重试{failedStage ? ` ${failedStage}` : ''}</Button>}
      </div>
      <Button size="small" danger type="text" icon={<DeleteOutlined />} onClick={(e) => { e.stopPropagation(); void onDelete(); }} />
    </div>
  </List.Item>;
}

function SegmentEditor({ segment, showRaw, onJump, onSave }: { segment: TranscriptSegment; showRaw: boolean; onJump: (ms: number) => void; onSave: (seg: TranscriptSegment, options?: SegmentSaveOptions) => void }) {
  const [editingSpeaker, setEditingSpeaker] = useState(false);
  const [editingText, setEditingText] = useState(false);
  const [draft, setDraft] = useState(segment);
  useEffect(() => setDraft(segment), [segment]);
  const saveSpeaker = () => {
    if (draft.speaker === segment.speaker) {
      setEditingSpeaker(false);
      return;
    }
    Modal.confirm({
      title: '是否同步替换相同发言人？',
      content: `将“${segment.speaker}”改为“${draft.speaker}”。请选择只修改本段，或替换本录音中所有相同发言人。`,
      okText: '全部替换',
      cancelText: '仅修改本段',
      onOk: () => { setEditingSpeaker(false); onSave({ ...segment, speaker: draft.speaker }, { replaceSameSpeaker: true }); },
      onCancel: () => { setEditingSpeaker(false); onSave({ ...segment, speaker: draft.speaker }, { replaceSameSpeaker: false }); },
    });
  };
  const saveText = () => {
    setEditingText(false);
    onSave({ ...segment, text: draft.text });
  };
  return <Card size="small" className="segment-card">
    <div className="segment-layout">
      <Button type="link" className="segment-time" onClick={() => onJump(segment.start_time_ms)}>{formatMs(segment.start_time_ms)}</Button>
      <div className="segment-body">
        <div className="segment-speaker-line">
          {editingSpeaker ? <Space.Compact><Input size="small" value={draft.speaker} onChange={(e) => setDraft({ ...draft, speaker: e.target.value })} onPressEnter={saveSpeaker} /><Button size="small" type="primary" onClick={saveSpeaker}>保存</Button><Button size="small" onClick={() => { setDraft(segment); setEditingSpeaker(false); }}>取消</Button></Space.Compact> : <><Text strong>{segment.speaker}</Text><Button size="small" type="text" icon={<EditOutlined />} onClick={() => setEditingSpeaker(true)} /></>}
        </div>
        <div className="segment-text-line">
          {editingText ? <div className="segment-edit-block"><Input.TextArea className="segment-edit-textarea" variant="borderless" autoSize={{ minRows: 2, maxRows: 12 }} value={draft.text} onChange={(e) => setDraft({ ...draft, text: e.target.value })} /><Space className="segment-edit-actions"><Button size="small" type="primary" onClick={saveText}>保存正文</Button><Button size="small" onClick={() => { setDraft(segment); setEditingText(false); }}>取消</Button></Space></div> : <><Text className="segment-text">{segment.text}</Text><Button size="small" type="text" icon={<EditOutlined />} onClick={() => setEditingText(true)} /></>}
        </div>
        {showRaw && segment.raw_text && <Paragraph className="raw-text">{segment.raw_text}</Paragraph>}
      </div>
    </div>
  </Card>;
}

function SummaryView({ summary, stale, onExport, onRegenerate }: { summary: any; stale?: boolean; onExport: () => void; onRegenerate: () => void }) {
  const markdown = summary?.content?.markdown || '';
  return <Space direction="vertical" style={{ width: '100%' }}>
    {stale && <Space><Tag color="orange">清洁稿已编辑，纪要可能过期</Tag><Button size="small" icon={<ReloadOutlined />} onClick={onRegenerate}>重新生成纪要</Button></Space>}
    <Button icon={<DownloadOutlined />} onClick={onExport}>导出纪要 Markdown</Button>
    <MarkdownLite markdown={markdown || '暂无纪要'} />
  </Space>;
}

function ExtractedTextView({ text, file }: { text: string; file: Recording | null }) {
  if (!file) return <div className="empty-doc-text">请选择文件</div>;
  if (isRecordingProcessing(file.status)) return <div className="empty-doc-text">正在提取文字，请稍后...</div>;
  if (file.status === 'failed') return <div className="empty-doc-text">文字提取失败，请在左侧点击重试。</div>;
  return <div className="extracted-text-panel">
    <pre>{text || '暂无提取文字稿'}</pre>
  </div>;
}

function FileInfoView({ file }: { file: Recording | null }) {
  if (!file) return <Text type="secondary">请选择文件</Text>;
  return <Space direction="vertical" style={{ width: '100%' }}>
    <Tag color="blue">{fileTypeLabel(file.file_type)}</Tag>
    <Paragraph><Text strong>文件名：</Text>{file.file_name}</Paragraph>
    <Paragraph><Text strong>处理状态：</Text>{recordingStatusLabel(file.status)}</Paragraph>
    <Paragraph><Text strong>提取引擎：</Text>{file.extraction_engine || '-'}</Paragraph>
    <Paragraph><Text strong>提取字数：</Text>{file.extracted_char_count || 0}</Paragraph>
    {(file.extraction_warnings || []).map((warning, index) => <Tag color="orange" key={index}>{warning}</Tag>)}
  </Space>;
}

function MarkdownLite({ markdown }: { markdown: string }) {
  return <div className="markdown-body">{markdown.split('\n').map((line, index) => {
    if (line.startsWith('### ')) return <Title level={5} key={index}>{line.slice(4)}</Title>;
    if (line.startsWith('## ')) return <Title level={4} key={index}>{line.slice(3)}</Title>;
    if (line.startsWith('# ')) return <Title level={3} key={index}>{line.slice(2)}</Title>;
    if (line.startsWith('- ')) return <Paragraph key={index}>• {line.slice(2)}</Paragraph>;
    if (/^\d+\.\s/.test(line)) return <Paragraph key={index}>{line}</Paragraph>;
    if (!line.trim()) return <div key={index} className="md-gap" />;
    return <Paragraph key={index}>{line}</Paragraph>;
  })}</div>;
}

function QAView({ checked, recordings, threads, currentThreadId, setCurrentThreadId, messages, question, setQuestion, onAsk, onNewThread, submitting, waitingForAnswer }: { checked: string[]; recordings: Recording[]; threads: QAThread[]; currentThreadId: string | null; setCurrentThreadId: (id: string) => void; messages: QAMessage[]; question: string; setQuestion: (v: string) => void; onAsk: () => void; onNewThread: () => void; submitting: boolean; waitingForAnswer: boolean }) {
  const selectedNames = recordings.filter((r) => checked.includes(fileKey(r))).map((r) => r.file_name);
  const readyCount = recordings.filter((r) => isRecordingReadyForQa(r.status)).length;
  return <Space direction="vertical" style={{ width: '100%' }}>
    <Space.Compact style={{ width: '100%' }}>
      <Button onClick={onNewThread}>新建对话</Button>
      <Select
        value={currentThreadId || undefined}
        placeholder="历史对话"
        suffixIcon={<DownOutlined />}
        style={{ flex: 1 }}
        onChange={setCurrentThreadId}
        options={threads.map((thread) => ({ value: thread.thread_id, label: `${thread.title || '新对话'} ${formatMonthDayTime(thread.last_message_at || thread.updated_at)}` }))}
      />
    </Space.Compact>
    <Tag color="blue">当前勾选：{checked.length} 份</Tag>
    <Paragraph type="secondary">{selectedNames.length ? selectedNames.join(' / ') : readyCount ? '请在左侧勾选已处理完成的参考文件' : '暂无处理完成的文件可用于问答'}</Paragraph>
    <div className="qa-messages">
      {messages.map((item) => <Card key={item.message_id} size="small" className={item.role === 'user' ? 'qa-user' : 'qa-assistant'}>
        <Text strong>{item.role === 'user' ? '用户' : 'AI'}</Text>
        {item.role === 'assistant' && item.reasoning_content && <details className="qa-reasoning" open={item.status === 'running'}>
          <summary>{item.status === 'running' ? '思考过程生成中' : '思考过程'}</summary>
          <pre>{item.reasoning_content}</pre>
        </details>}
        {item.status === 'failed' ? <Paragraph type="danger">{item.content || '生成失败，请稍后重试'}</Paragraph> : <MarkdownLite markdown={item.content || (['queued', 'running'].includes(item.status) ? '生成中...' : item.status)} />}
        {item.role === 'assistant' && <Paragraph type="secondary">本轮参考材料：{item.selected_file_ids?.length || item.selected_recording_ids?.length || 0} 份</Paragraph>}
        {(item.sources || []).slice(0, 3).map((s, idx) => <Paragraph type="secondary" key={idx}>来源：{s.file_name} {formatMs(s.start_time_ms)} - {s.quote}</Paragraph>)}
      </Card>)}
    </div>
    {waitingForAnswer && <Paragraph type="secondary">AI 正在回答，完成后可发送下一条问题；你可以先继续输入。</Paragraph>}
    <Input.TextArea rows={3} placeholder="输入问题" value={question} onChange={(e) => setQuestion(e.target.value)} onPressEnter={(e) => { if (!e.shiftKey) { e.preventDefault(); if (!waitingForAnswer) onAsk(); } }} />
    <Button type="primary" onClick={onAsk} loading={submitting} disabled={waitingForAnswer || !checked.length}>发送</Button>
  </Space>;
}

function UploadModal({ open, projectId, onClose, onCreated, onDone }: { open: boolean; projectId: string; onClose: () => void; onCreated: (fileId: string) => void; onDone: () => void }) {
  const [files, setFiles] = useState<UploadFile[]>([]);
  const [progress, setProgress] = useState(0);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState('');
  const [limits, setLimits] = useState<AppSettings['basic']>({ max_upload_size_mb: 500, max_recording_duration_hours: 3 });
  const [speakerMode, setSpeakerMode] = useState<'2' | '3' | '4' | 'auto'>('2');

  useEffect(() => {
    if (!open) return;
    api<AppSettings>('/api/settings').then((data) => setLimits(data.basic)).catch(() => undefined);
  }, [open]);

  const upload = async () => {
    const file = files[0]?.originFileObj as File | undefined;
    if (!file) return message.warning('请选择文件');
    if (file.size > limits.max_upload_size_mb * 1024 * 1024) return message.error(`文件超过 ${limits.max_upload_size_mb}MB`);
    setUploading(true);
    setUploadError('');
    try {
      const ext = file.name.split('.').pop()?.toLowerCase() || '';
      const audio = ['mp3', 'wav', 'm4a', 'aac', 'flac', 'ogg', 'wma'].includes(ext);
      const duration = audio ? await readAudioDuration(file) : null;
      if (audio && duration && duration > limits.max_recording_duration_hours * 3600) {
        message.error(`文件时长超过 ${limits.max_recording_duration_hours} 小时`);
        return;
      }
      const speakerCount = speakerMode === 'auto' ? 'auto' : speakerMode;
      setProgress(0);
      const session = await api<any>(`/api/projects/${projectId}/files/upload-session`, { method: 'POST', body: JSON.stringify({ file_name: file.name, file_size_bytes: file.size, mime_type: file.type || 'application/octet-stream', extension: ext, duration_seconds: audio && duration ? Math.round(duration) : 0, template_type: 'customer_interview', speaker_count: speakerCount }) });
      onCreated(session.file_id);
      const form = new FormData();
      form.append('file', file);
      form.append('speaker_count', speakerCount);
      let closedAfterClientUpload = false;
      const closeAfterClientUpload = () => {
        if (closedAfterClientUpload) return;
        closedAfterClientUpload = true;
        onDone();
      };
      await postFormWithProgress(`/api/files/${session.file_id}/upload-content`, form, (value) => {
        setProgress(value);
        if (value >= 100) {
          message.loading('文件已上传，正在保存到云存储...', 1.5);
          closeAfterClientUpload();
        }
      });
      message.success('上传完成，已进入处理队列');
      setFiles([]);
      setProgress(0);
      setSpeakerMode('2');
      closeAfterClientUpload();
    } catch (err) {
      const text = err instanceof Error ? err.message : '上传失败';
      setUploadError(text);
      message.error(text);
    } finally {
      setUploading(false);
    }
  };
  const selectedExt = (files[0]?.name || '').split('.').pop()?.toLowerCase() || '';
  const selectedIsAudio = !selectedExt || ['mp3', 'wav', 'm4a', 'aac', 'flac', 'ogg', 'wma'].includes(selectedExt);
  return <Modal title="上传文件" open={open} onCancel={uploading ? undefined : onClose} onOk={upload} okText="开始上传" confirmLoading={uploading} maskClosable={!uploading}>
    <Upload.Dragger beforeUpload={() => false} maxCount={1} fileList={files} onChange={(info) => { setFiles(info.fileList); setUploadError(''); }} disabled={uploading}>
      <p className="ant-upload-drag-icon"><InboxOutlined /></p>
      <p>拖拽文件到此处，或点击选择文件</p>
      <p>支持音频、PDF、Excel、Word docx、TXT/MD，单文件最大 {limits.max_upload_size_mb}MB；音频时长上限 {limits.max_recording_duration_hours} 小时</p>
    </Upload.Dragger>
    {selectedIsAudio && <div className="upload-speaker-setting">
      <Text strong>说话人数量</Text>
      <Radio.Group value={speakerMode} onChange={(event) => setSpeakerMode(event.target.value)} disabled={uploading}>
        <Radio.Button value="2">2</Radio.Button>
        <Radio.Button value="3">3</Radio.Button>
        <Radio.Button value="4">4</Radio.Button>
        <Radio.Button value="auto">智能识别</Radio.Button>
      </Radio.Group>
      {speakerMode === 'auto' && <Text type="warning">该模式下识别时长会显著提升，请谨慎选择</Text>}
    </div>}
    {progress > 0 && <Progress percent={progress} status={progress >= 100 ? 'success' : 'active'} />}
    {progress >= 100 && uploading && <Paragraph type="secondary">文件已上传，正在保存到云存储...</Paragraph>}
    {uploadError && <Text type="danger">{uploadError}</Text>}
  </Modal>;
}

function postFormWithProgress(url: string, body: FormData, onProgress: (value: number) => void) {
  return new Promise<any>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    const token = getToken();
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);
    xhr.upload.onprogress = (event) => { if (event.lengthComputable) onProgress(Math.round((event.loaded / event.total) * 100)); };
    xhr.onload = () => {
      const data = safeParseJson(xhr.responseText);
      if (xhr.status >= 200 && xhr.status < 300 && data?.success !== false) {
        resolve(data?.data ?? {});
        return;
      }
      reject(new Error(data?.error?.message || data?.detail || `上传失败：HTTP ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error('上传失败'));
    xhr.ontimeout = () => reject(new Error('上传超时，请稍后重试或换一个更小的文件测试'));
    xhr.timeout = 10 * 60 * 1000;
    xhr.send(body);
  });
}

function safeParseJson(text: string) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function readAudioDuration(file: File) {
  return new Promise<number | null>((resolve) => {
    const audio = document.createElement('audio');
    const url = URL.createObjectURL(file);
    let settled = false;
    const timer = window.setTimeout(() => finish(null), 5000);
    const finish = (value: number | null) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      URL.revokeObjectURL(url);
      audio.removeAttribute('src');
      audio.load();
      resolve(value);
    };
    audio.preload = 'metadata';
    audio.onloadedmetadata = () => {
      const duration = Number.isFinite(audio.duration) ? audio.duration : null;
      finish(duration);
    };
    audio.onerror = () => finish(null);
    audio.src = url;
  });
}

function QueueModal({ open, projectId, clockNow, onClose, onRefresh }: { open: boolean; projectId: string; clockNow: number; onClose: () => void; onRefresh: () => void }) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const load = useCallback(async () => {
    if (!open) return;
    const data = await api<{ items: Job[] }>(`/api/projects/${projectId}/jobs?page_size=50`);
    setJobs(data.items);
  }, [open, projectId]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (!open) return;
    const timer = window.setInterval(() => { void load(); onRefresh(); }, 3000);
    return () => window.clearInterval(timer);
  }, [open, load, onRefresh]);
  const retry = async (job: Job) => { await api(`/api/jobs/${job.job_id}/retry`, { method: 'POST' }); message.success('已重试'); void load(); onRefresh(); };
  return <Modal title="处理队列" open={open} onCancel={onClose} footer={<Button onClick={onClose}>关闭</Button>} width={900}><Table rowKey="job_id" dataSource={jobs} pagination={false} columns={[
    { title: '任务', dataIndex: 'job_type', render: (value: string) => jobTypeLabel(value) },
    { title: '状态', dataIndex: 'status', render: (value: string, job: Job) => <Space><Tag color={value === 'failed' ? 'red' : ['queued', 'running'].includes(value) ? 'blue' : 'green'}>{jobStatusLabel(value)}</Tag>{['queued', 'running'].includes(value) && <Text type="secondary">{elapsedSince(job.started_at || job.created_at, clockNow)}</Text>}</Space> },
    { title: '进度', dataIndex: 'progress', width: 150, render: (value: number) => <Progress percent={value || 0} size="small" /> },
    { title: '错误', dataIndex: 'error_message' },
    { title: '操作', render: (_, job) => job.status === 'failed' ? <Button onClick={() => retry(job)}>重试</Button> : null }
  ]} /></Modal>;
}

function MembersModal({ open, projectId, project, onClose, onRefresh }: { open: boolean; projectId: string; project: Project | null; onClose: () => void; onRefresh: () => void }) {
  const [members, setMembers] = useState<User[]>([]);
  const [username, setUsername] = useState('');
  const [loading, setLoading] = useState(false);
  const load = useCallback(async () => {
    if (!open) return;
    const data = await api<{ items: User[] }>(`/api/projects/${projectId}/members`);
    setMembers(data.items);
  }, [open, projectId]);
  useEffect(() => { void load(); }, [load]);
  const add = async () => {
    if (!username.trim()) return;
    await api(`/api/projects/${projectId}/members`, { method: 'POST', body: JSON.stringify({ username: username.trim() }) });
    setUsername('');
    message.success('成员已添加');
    void load();
  };
  const remove = async (userId: string) => {
    await api(`/api/projects/${projectId}/members/${userId}`, { method: 'DELETE' });
    message.success('成员已移除');
    void load();
  };
  const toggleSharing = async (shared: boolean, force = false) => {
    setLoading(true);
    try {
      await api(`/api/projects/${projectId}/sharing`, { method: 'PATCH', body: JSON.stringify({ is_shared: shared, force }) });
      message.success(shared ? '已开启项目共享' : '已关闭项目共享');
      onRefresh();
    } catch (err) {
      const text = err instanceof Error ? err.message : '';
      if (!shared && !force && text.includes('引用')) {
        Modal.confirm({
          title: '该项目文件已被引用',
          content: '关闭共享后，其他项目中的引用文件将不可用。是否继续？',
          okText: '继续关闭',
          okButtonProps: { danger: true },
          onOk: () => toggleSharing(false, true),
        });
      } else {
        message.error(text || '操作失败');
      }
    } finally {
      setLoading(false);
    }
  };
  return <Modal title="项目成员与共享" open={open} onCancel={onClose} footer={<Button onClick={onClose}>关闭</Button>} width={760}>
    <Space direction="vertical" style={{ width: '100%' }}>
      <Card size="small" title="项目共享">
        <Space>
          <Tag color={project?.is_shared ? 'green' : 'default'}>{project?.is_shared ? '已共享' : '未共享'}</Tag>
          <Button loading={loading} onClick={() => toggleSharing(!project?.is_shared)}>{project?.is_shared ? '关闭共享' : '开启共享'}</Button>
          <Text type="secondary">开启后，其他用户可搜索该项目文件并添加引用。</Text>
        </Space>
      </Card>
      <Card size="small" title="项目成员">
        <Space.Compact style={{ width: '100%', marginBottom: 12 }}>
          <Input placeholder="输入用户名添加成员" value={username} onChange={(e) => setUsername(e.target.value)} onPressEnter={add} />
          <Button type="primary" onClick={add}>添加</Button>
        </Space.Compact>
        <Table rowKey="user_id" size="small" pagination={false} dataSource={members} columns={[
          { title: '用户名', dataIndex: 'username' },
          { title: '姓名', dataIndex: 'display_name' },
          { title: '角色', dataIndex: 'role', render: (value: string) => value === 'admin' ? '管理员' : '用户' },
          { title: '操作', width: 100, render: (_, row) => row.user_id === project?.owner_id ? <Tag>创建人</Tag> : <Button size="small" danger onClick={() => remove(row.user_id)}>移除</Button> }
        ]} />
      </Card>
    </Space>
  </Modal>;
}

function SharedFilesModal({ open, projectId, onClose, onAdded }: { open: boolean; projectId: string; onClose: () => void; onAdded: () => void }) {
  const [keyword, setKeyword] = useState('');
  const [items, setItems] = useState<Array<Recording & { project_title?: string }>>([]);
  const load = useCallback(async () => {
    if (!open) return;
    const data = await api<{ items: Array<Recording & { project_title?: string }> }>(`/api/shared-files/search?target_project_id=${projectId}&keyword=${encodeURIComponent(keyword)}`);
    setItems(data.items);
  }, [open, projectId, keyword]);
  useEffect(() => { void load(); }, [load]);
  const addReference = async (fileId?: string) => {
    if (!fileId) return;
    await api(`/api/projects/${projectId}/file-references`, { method: 'POST', body: JSON.stringify({ source_file_id: fileId }) });
    message.success('已添加引用文件');
    onAdded();
  };
  return <Modal title="添加共享文件引用" open={open} onCancel={onClose} footer={<Button onClick={onClose}>关闭</Button>} width={820}>
    <Space direction="vertical" style={{ width: '100%' }}>
      <Input.Search placeholder="搜索共享文件" allowClear onSearch={setKeyword} onChange={(e) => { if (!e.target.value) setKeyword(''); }} />
      <Table rowKey="file_id" size="small" pagination={{ pageSize: 8 }} dataSource={items} columns={[
        { title: '文件名', dataIndex: 'file_name' },
        { title: '类型', dataIndex: 'file_type', width: 100, render: fileTypeLabel },
        { title: '来源项目', dataIndex: 'project_title', width: 180 },
        { title: '状态', dataIndex: 'status', width: 120, render: recordingStatusLabel },
        { title: '操作', width: 100, render: (_, row) => <Button size="small" type="primary" onClick={() => addReference(row.file_id)}>添加引用</Button> },
      ]} />
    </Space>
  </Modal>;
}

type AiNodeKey = 'asr' | 'clean' | 'summary' | 'qa';

const AI_SETTING_NODES: Array<{ key: AiNodeKey; label: string; hint: string }> = [
  { key: 'asr', label: 'ASR 语音识别', hint: '录音文件转文字，默认 fun-asr。' },
  { key: 'clean', label: '清洁稿生成', hint: '将原始转写稿整理为可阅读稿，默认 qwen3.5-flash。' },
  { key: 'summary', label: '纪要生成', hint: '根据清洁稿生成 Markdown 访谈纪要，默认 qwen3.5-flash。' },
  { key: 'qa', label: '项目问答', hint: '基于勾选文件和最近 4 轮问答回答问题，默认 qwen3.6-plus。' }
];

type StorageProvider = AppSettings['storage']['provider'];

type SettingsFormValues = {
  basic: AppSettings['basic'];
  ai: Record<AiNodeKey, { model: string; url: string; key?: string }>;
  storage: AppSettings['storage'];
};

function AdminPage({ onBack }: { onBack: () => void }) {
  const [users, setUsers] = useState<User[]>([]);
  const [projectUsage, setProjectUsage] = useState<any[]>([]);
  const [userUsage, setUserUsage] = useState<any[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [createOpen, setCreateOpen] = useState(false);
  const [quotaUser, setQuotaUser] = useState<User | null>(null);
  const [form] = Form.useForm();
  const [quotaForm] = Form.useForm<UserQuota>();
  const load = useCallback(async () => {
    const [userData, projectData, userUsageData, jobData] = await Promise.all([
      api<{ items: User[] }>('/api/admin/users'),
      api<{ items: any[] }>('/api/admin/usage/projects'),
      api<{ items: any[] }>('/api/admin/usage/users'),
      api<{ items: Job[] }>('/api/jobs/recent?page_size=50'),
    ]);
    setUsers(userData.items);
    setProjectUsage(projectData.items);
    setUserUsage(userUsageData.items);
    setJobs(jobData.items);
  }, []);
  useEffect(() => { void load().catch((err) => message.error((err as Error).message)); }, [load]);
  const createUser = async () => {
    const values = await form.validateFields();
    await api('/api/admin/users', { method: 'POST', body: JSON.stringify(values) });
    message.success('用户已创建');
    setCreateOpen(false);
    form.resetFields();
    void load();
  };
  const updateUser = async (user: User, patch: Partial<User>) => {
    await api(`/api/admin/users/${user.user_id}`, { method: 'PATCH', body: JSON.stringify(patch) });
    message.success('用户已更新');
    void load();
  };
  const openQuota = async (user: User) => {
    const quota = await api<UserQuota>(`/api/admin/users/${user.user_id}/quota`);
    setQuotaUser(user);
    quotaForm.setFieldsValue(quota);
  };
  const saveQuota = async () => {
    if (!quotaUser) return;
    const values = await quotaForm.validateFields();
    await api(`/api/admin/users/${quotaUser.user_id}/quota`, { method: 'PATCH', body: JSON.stringify(values) });
    message.success('限额已保存');
    setQuotaUser(null);
    void load();
  };
  return <>
    <Header className="topbar"><Space><Button onClick={onBack}>返回</Button><Title level={3}>管理后台</Title></Space></Header>
    <Content className="home-content settings-content">
      <Tabs items={[
        { key: 'users', label: '用户管理', children: <Card extra={<Button type="primary" onClick={() => setCreateOpen(true)}>新建用户</Button>}>
          <Table rowKey="user_id" dataSource={users} pagination={false} columns={[
            { title: '用户名', dataIndex: 'username' },
            { title: '姓名', dataIndex: 'display_name' },
            { title: '角色', dataIndex: 'role', render: (value: string, row: User) => <Select size="small" value={value} style={{ width: 100 }} onChange={(role) => updateUser(row, { role: role as User['role'] })} options={[{ value: 'user', label: '用户' }, { value: 'admin', label: '管理员' }]} /> },
            { title: '状态', dataIndex: 'status', render: (value: string, row: User) => <Select size="small" value={value} style={{ width: 100 }} onChange={(status) => updateUser(row, { status: status as User['status'] })} options={[{ value: 'active', label: '启用' }, { value: 'disabled', label: '停用' }]} /> },
            { title: '操作', render: (_, row) => <Space><Button size="small" onClick={() => openQuota(row)}>限额</Button><Button size="small" onClick={() => Modal.confirm({ title: '重置密码', content: <Input.Password id="reset-password-input" placeholder="输入新密码" />, onOk: async () => { const input = document.getElementById('reset-password-input') as HTMLInputElement | null; await api(`/api/admin/users/${row.user_id}/reset-password`, { method: 'POST', body: JSON.stringify({ password: input?.value || '' }) }); message.success('密码已重置'); } })}>重置密码</Button></Space> }
          ]} />
        </Card> },
        { key: 'projectUsage', label: '项目用量报表', children: <Card><Table rowKey="project_id" dataSource={projectUsage} pagination={{ pageSize: 10 }} columns={[
          { title: '项目', dataIndex: 'project_name' },
          { title: '文件数', dataIndex: 'file_count' },
          { title: '录音总时长', dataIndex: 'audio_duration_seconds', render: formatDuration },
          { title: '问答次数', dataIndex: 'qa_count' },
          { title: '输入 Token', dataIndex: 'qa_input_tokens' },
          { title: '输出 Token', dataIndex: 'qa_output_tokens' },
        ]} /></Card> },
        { key: 'userUsage', label: '用户用量报表', children: <Card><Table rowKey="user_id" dataSource={userUsage} pagination={{ pageSize: 10 }} columns={[
          { title: '用户', dataIndex: 'display_name' },
          { title: '录音处理量', dataIndex: 'audio_duration_seconds', render: formatDuration },
          { title: 'ASR次数', dataIndex: 'asr_count' },
          { title: '问答次数', dataIndex: 'qa_count' },
          { title: '输入 Token', dataIndex: 'qa_input_tokens' },
          { title: '输出 Token', dataIndex: 'qa_output_tokens' },
        ]} /></Card> },
        { key: 'jobs', label: '任务监控', children: <Card><Table rowKey="job_id" dataSource={jobs} pagination={{ pageSize: 10 }} columns={[
          { title: '任务', dataIndex: 'job_type', render: jobTypeLabel },
          { title: '状态', dataIndex: 'status', render: jobStatusLabel },
          { title: '项目', dataIndex: 'project_id' },
          { title: '文件', dataIndex: 'file_id' },
          { title: '错误', dataIndex: 'error_message' },
        ]} /></Card> },
      ]} />
      <Modal title="新建用户" open={createOpen} onOk={createUser} onCancel={() => setCreateOpen(false)} okText="创建" cancelText="取消">
        <Form form={form} layout="vertical" initialValues={{ role: 'user', status: 'active' }}>
          <Form.Item name="username" label="用户名" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="display_name" label="姓名"><Input /></Form.Item>
          <Form.Item name="password" label="初始密码" rules={[{ required: true }]}><Input.Password /></Form.Item>
          <Form.Item name="role" label="角色"><Select options={[{ value: 'user', label: '用户' }, { value: 'admin', label: '管理员' }]} /></Form.Item>
          <Form.Item name="status" label="状态"><Select options={[{ value: 'active', label: '启用' }, { value: 'disabled', label: '停用' }]} /></Form.Item>
        </Form>
      </Modal>
      <Modal title={`设置限额：${quotaUser?.display_name || ''}`} open={!!quotaUser} onOk={saveQuota} onCancel={() => setQuotaUser(null)} okText="保存" cancelText="取消">
        <Form form={quotaForm} layout="vertical">
          <Form.Item name="daily_asr_seconds" label="每日 ASR 时长上限（秒，0 表示不限）"><InputNumber min={0} style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="monthly_asr_seconds" label="每月 ASR 时长上限（秒，0 表示不限）"><InputNumber min={0} style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="daily_qa_tokens" label="每日问答 Token 上限（0 表示不限）"><InputNumber min={0} style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="monthly_qa_tokens" label="每月问答 Token 上限（0 表示不限）"><InputNumber min={0} style={{ width: '100%' }} /></Form.Item>
        </Form>
      </Modal>
    </Content>
  </>;
}

function SettingsPage({ onBack }: { onBack: () => void }) {
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [saving, setSaving] = useState(false);
  const [testingNode, setTestingNode] = useState<AiNodeKey | null>(null);
  const [testingStorage, setTestingStorage] = useState(false);
  const [testResults, setTestResults] = useState<Partial<Record<AiNodeKey, { status: 'passed' | 'failed'; message: string; latency_ms?: number }>>>({});
  const [storageTestResult, setStorageTestResult] = useState<{ status: 'passed' | 'failed'; message: string } | null>(null);
  const [form] = Form.useForm<SettingsFormValues>();
  const storageProvider = Form.useWatch(['storage', 'provider'], form) as StorageProvider | undefined;
  const storageIsLocal = (storageProvider || 'local') === 'local';

  const applySettingsToForm = (data: AppSettings) => {
    const incomingStorage = data.storage || {};
    const storage = {
      provider: incomingStorage.provider || 'local' as StorageProvider,
      bucket_name: incomingStorage.bucket_name || '',
      endpoint: incomingStorage.endpoint || '',
      region: incomingStorage.region || 'auto',
      path_prefix: incomingStorage.path_prefix || '',
      access_key_id: '',
      secret_access_key: '',
      access_key_configured: Boolean(incomingStorage.access_key_configured),
      secret_key_configured: Boolean(incomingStorage.secret_key_configured)
    };
    form.setFieldsValue({
      basic: data.basic,
      ai: Object.fromEntries(AI_SETTING_NODES.map((node) => [node.key, { model: data.ai[node.key].model, url: data.ai[node.key].url, key: '' }])) as SettingsFormValues['ai'],
      storage: { ...storage, access_key_id: '', secret_access_key: '' }
    });
  };

  const loadSettings = useCallback(async () => {
    try {
      const data = await api<AppSettings>('/api/settings');
      setSettings(data);
      applySettingsToForm(data);
    } catch (err) {
      message.error((err as Error).message);
    }
  }, [form]);

  useEffect(() => { void loadSettings(); }, [loadSettings]);

  const saveSettings = async (values: SettingsFormValues) => {
    setSaving(true);
    try {
      const payload = {
        basic: {
          max_upload_size_mb: Number(values.basic?.max_upload_size_mb || 500),
          max_recording_duration_hours: Number(values.basic?.max_recording_duration_hours || 3)
        },
        ai: Object.fromEntries(AI_SETTING_NODES.map((node) => [node.key, {
          model: values.ai?.[node.key]?.model || '',
          url: values.ai?.[node.key]?.url || '',
          key: values.ai?.[node.key]?.key || ''
        }])),
        storage: {
          provider: values.storage?.provider || 'local',
          bucket_name: values.storage?.bucket_name || '',
          endpoint: values.storage?.endpoint || '',
          region: values.storage?.region || 'auto',
          path_prefix: values.storage?.path_prefix || '',
          access_key_id: values.storage?.access_key_id || '',
          secret_access_key: values.storage?.secret_access_key || ''
        }
      };
      const data = await api<AppSettings>('/api/settings', { method: 'PATCH', body: JSON.stringify(payload) });
      setSettings(data);
      applySettingsToForm(data);
      message.success('设置已保存');
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const testAiNode = async (node: AiNodeKey) => {
    try {
      await form.validateFields([['ai', node, 'model'], ['ai', node, 'url']]);
      const nodeValues = form.getFieldValue(['ai', node]) || {};
      setTestingNode(node);
      const result = await api<AiTestResult>(`/api/settings/ai/${node}/test`, {
        method: 'POST',
        body: JSON.stringify({ model: nodeValues.model || '', url: nodeValues.url || '', key: nodeValues.key || '' })
      });
      setTestResults((prev) => ({ ...prev, [node]: { status: 'passed', message: result.message, latency_ms: result.latency_ms } }));
      message.success(`${AI_SETTING_NODES.find((item) => item.key === node)?.label || node}测试通过`);
    } catch (err) {
      const error = err as Error & { errorFields?: unknown[] };
      if (error.errorFields) return;
      setTestResults((prev) => ({ ...prev, [node]: { status: 'failed', message: error.message } }));
      message.error(error.message);
    } finally {
      setTestingNode(null);
    }
  };

  const testStorage = async () => {
    try {
      const provider = form.getFieldValue(['storage', 'provider']) || 'local';
      if (provider !== 'local') {
        await form.validateFields([['storage', 'provider'], ['storage', 'bucket_name'], ['storage', 'endpoint']]);
      }
      const values = form.getFieldValue('storage') || {};
      setTestingStorage(true);
      const result = await api<StorageTestResult>('/api/settings/storage/test', {
        method: 'POST',
        body: JSON.stringify(values)
      });
      setStorageTestResult({ status: 'passed', message: result.message });
      message.success('存储测试通过');
    } catch (err) {
      const error = err as Error & { errorFields?: unknown[] };
      if (error.errorFields) return;
      setStorageTestResult({ status: 'failed', message: error.message });
      message.error(error.message);
    } finally {
      setTestingStorage(false);
    }
  };

  return <>
    <Header className="topbar"><Space><Button onClick={onBack}>返回</Button><Title level={3}>系统设置</Title></Space></Header>
    <Content className="home-content settings-content">
      <Form form={form} layout="vertical" onFinish={saveSettings} className="settings-form">
        <Card title="基础设置" extra={<Text type="secondary">控制上传前校验的默认限制</Text>}>
          <div className="basic-settings-grid">
            <Form.Item name={['basic', 'max_upload_size_mb']} label="单个文件大小上限" rules={[{ required: true, message: '请输入文件大小上限' }]}>
              <InputNumber min={1} max={5000} addonAfter="MB" style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item name={['basic', 'max_recording_duration_hours']} label="文件时长上限" rules={[{ required: true, message: '请输入文件时长上限' }]}>
              <InputNumber min={1} max={24} addonAfter="小时" style={{ width: '100%' }} />
            </Form.Item>
          </div>
        </Card>
        <Card title="AI 设置" extra={<Text type="secondary">每个节点可独立配置模型名称、URL 和 Key</Text>}>
          <Paragraph type="secondary">Key 保存后不会在页面回显；留空保存表示不修改该节点已有 Key。</Paragraph>
          <div className="ai-settings-grid">
            {AI_SETTING_NODES.map((node) => <Card key={node.key} size="small" title={node.label} extra={settings?.ai[node.key]?.key_configured ? <Tag color="green">Key 已配置</Tag> : <Tag>未配置 Key</Tag>}>
              <Paragraph type="secondary">{node.hint}</Paragraph>
              <Form.Item name={['ai', node.key, 'model']} label="模型名称" rules={[{ required: true, message: '请输入模型名称' }]}>
                <Input placeholder="例如 fun-asr / qwen3.5-flash" />
              </Form.Item>
              <Form.Item name={['ai', node.key, 'url']} label="API URL" rules={[{ required: true, message: '请输入 API URL' }]}>
                <Input placeholder="https://..." />
              </Form.Item>
              <Form.Item name={['ai', node.key, 'key']} label="API Key">
                <Input.Password autoComplete="new-password" placeholder={settings?.ai[node.key]?.key_configured ? '已配置，留空则不修改' : '请输入 API Key'} />
              </Form.Item>
              <Space className="ai-test-row" align="center">
                <Button htmlType="button" onClick={() => testAiNode(node.key)} loading={testingNode === node.key}>测试连接</Button>
                {testResults[node.key] && <Tag color={testResults[node.key]?.status === 'passed' ? 'green' : 'red'}>{testResults[node.key]?.status === 'passed' ? '测试通过' : '测试失败'}</Tag>}
                {testResults[node.key]?.latency_ms !== undefined && <Text type="secondary">{testResults[node.key]?.latency_ms}ms</Text>}
              </Space>
              {testResults[node.key]?.message && <Paragraph type="secondary" className="ai-test-message">{testResults[node.key]?.message}</Paragraph>}
            </Card>)}
          </div>
        </Card>
        <Card title="存储设置" extra={<Text type="secondary">修改后只影响新上传文件，历史录音保留上传时的存储位置</Text>}>
          <div className="storage-settings-grid">
            <Form.Item name={['storage', 'provider']} label="存储类型" rules={[{ required: true, message: '请选择存储类型' }]}>
              <Select options={[
                { value: 'local', label: 'Local 本地存储' },
                { value: 'railway_bucket', label: 'Railway Bucket' },
                { value: 's3_compatible', label: 'S3 Compatible' }
              ]} />
            </Form.Item>
            <Form.Item name={['storage', 'bucket_name']} label="Bucket 名称" rules={storageIsLocal ? [] : [{ required: true, message: '请输入 Bucket 名称' }]}>
              <Input disabled={storageIsLocal} placeholder="例如 railway bucket 名称" />
            </Form.Item>
            <Form.Item name={['storage', 'endpoint']} label="Endpoint" rules={storageIsLocal ? [] : [{ required: true, message: '请输入 Endpoint' }]}>
              <Input disabled={storageIsLocal} placeholder="https://..." />
            </Form.Item>
            <Form.Item name={['storage', 'region']} label="Region">
              <Input disabled={storageIsLocal} placeholder="auto" />
            </Form.Item>
            <Form.Item name={['storage', 'path_prefix']} label="Path Prefix">
              <Input placeholder="可选，例如 prod 或 ai-asr-file" />
            </Form.Item>
            <Form.Item name={['storage', 'access_key_id']} label="Access Key ID">
              <Input.Password disabled={storageIsLocal} autoComplete="new-password" placeholder={settings?.storage?.access_key_configured ? '已配置，留空则不修改' : '请输入 Access Key ID'} />
            </Form.Item>
            <Form.Item name={['storage', 'secret_access_key']} label="Secret Access Key">
              <Input.Password disabled={storageIsLocal} autoComplete="new-password" placeholder={settings?.storage?.secret_key_configured ? '已配置，留空则不修改' : '请输入 Secret Access Key'} />
            </Form.Item>
          </div>
          <Space className="storage-test-row" align="center">
            <Button htmlType="button" onClick={testStorage} loading={testingStorage}>测试存储连接</Button>
            {settings?.storage?.access_key_configured && !storageIsLocal && <Tag color="green">Access Key 已配置</Tag>}
            {settings?.storage?.secret_key_configured && !storageIsLocal && <Tag color="green">Secret Key 已配置</Tag>}
            {storageTestResult && <Tag color={storageTestResult.status === 'passed' ? 'green' : 'red'}>{storageTestResult.status === 'passed' ? '测试通过' : '测试失败'}</Tag>}
          </Space>
          {storageTestResult?.message && <Paragraph type="secondary" className="storage-test-message">{storageTestResult.message}</Paragraph>}
        </Card>
        <Space>
          <Button type="primary" htmlType="submit" loading={saving}>保存设置</Button>
          <Button onClick={loadSettings}>重新加载</Button>
        </Space>
      </Form>
    </Content>
  </>;
}

function formatDate(value: string) {
  if (!value) return '-';
  return value.slice(0, 10);
}

function formatMonthDayTime(value: string) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.slice(5, 16);
  return `${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')} ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
}

function downloadTextFile(content: string, filename: string) {
  const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

export default App;
