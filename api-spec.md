# 录音访谈分析工作台 API 规范

版本：v0.2  
日期：2026-05-07  
部署目标：Railway  
适用阶段：MVP

---

## 1. 文档范围

本文定义 MVP 阶段需要的 API 规范，包括：

1. 本产品后端对前端开放的业务 API。
2. 后端/Worker 对 Railway Storage Bucket、阿里云 Fun-ASR、阿里云 LLM 的调用规范。
3. 任务状态、错误码、日志、成本记录等通用约定。

MVP 不做正式用户管理、部门、权限、RAG 和向量数据库。

注意：Railway 部署后应用具备公网访问能力。MVP 先提供最简单登录，账号固定为 `admin`，密码固定为 `mp2026`，后续再升级为正式用户体系。

---

## 2. 参考官方文档

### 2.1 Railway

- Railway Storage Buckets：<https://docs.railway.com/guides/storage-buckets>
- Railway Storage Buckets 上传与访问：<https://docs.railway.com/storage-buckets/uploading-serving>
- Railway FastAPI 部署：<https://docs.railway.com/guides/fastapi>
- Railway PostgreSQL：<https://docs.railway.com/guides/postgresql>
- Railway Redis：<https://docs.railway.com/guides/redis>

### 2.2 阿里云模型服务

- 阿里云 Fun-ASR 录音文件识别 RESTful API：<https://help.aliyun.com/zh/model-studio/fun-asr-recorded-speech-recognition-restful-api>
- 阿里云百炼 OpenAI 兼容接口：<https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope>

---

## 3. 总体架构 API 边界

```text
Browser Frontend
  -> Product API on Railway
  -> Railway Storage Bucket by presigned URL
  -> Railway PostgreSQL
  -> Railway Redis Queue
  -> Railway Worker
      -> Alibaba Fun-ASR
      -> Alibaba LLM
      -> Railway Storage Bucket
```

API 分层：

| 分层 | 调用方 | 被调用方 | 说明 |
|---|---|---|---|
| 前端业务 API | Browser | Product API | 项目、录音、任务、纪要、问答、导出 |
| 对象存储 API | Browser/Worker | Railway Storage Bucket | 预签名上传、下载、删除对象 |
| ASR API | Worker | 阿里云 Fun-ASR | 创建识别任务、轮询结果 |
| LLM API | Worker/API | 阿里云 LLM | 清洁稿、纪要、问答 |
| 队列 API | Product API | Redis Queue | 创建异步任务 |
| 数据 API | Product API/Worker | PostgreSQL | 保存业务数据、日志、成本 |

---

## 4. 通用约定

### 4.1 Base URL

本产品后端：

```text
https://{railway-app-domain}/api
```

本地开发：

```text
http://localhost:8000/api
```

### 4.2 请求头

```http
Content-Type: application/json
X-Request-Id: <uuid，可选>
Authorization: Bearer <login_token，除健康检查和登录外必填>
```

### 4.3 通用响应格式

成功：

```json
{
  "success": true,
  "data": {},
  "request_id": "req_123",
  "server_time": "2026-05-07T10:00:00+08:00"
}
```

失败：

```json
{
  "success": false,
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "文件大小超过 500M",
    "details": {}
  },
  "request_id": "req_123",
  "server_time": "2026-05-07T10:00:00+08:00"
}
```

### 4.4 分页

列表接口统一支持：

```text
page: number，默认 1
page_size: number，默认 20，最大 100
```

响应：

```json
{
  "items": [],
  "page": 1,
  "page_size": 20,
  "total": 100
}
```

### 4.5 ID 约定

| 对象 | ID 前缀 |
|---|---|
| 项目 | `proj_` |
| 录音 | `rec_` |
| 任务 | `job_` |
| 转写段落 | `seg_` |
| 纪要 | `sum_` |
| 问答会话 | `qa_` |
| 导出任务 | `exp_` |
| 日志 | `log_` |

---

## 5. 状态枚举

### 5.1 Recording Status

```text
created
uploading
queued
asr_processing
asr_completed
cleaning
cleaning_completed
summary_generating
completed
failed
```

### 5.2 Job Type

```text
upload_finalize
asr_transcription
clean_transcript
summary_generation
qa_answer
context_compression
export
diagnostic_export
```

### 5.3 Job Status

```text
queued
running
succeeded
failed
retrying
canceled
```

### 5.4 Transcript Source

```text
raw_asr
clean_ai
clean_user_edited
```

### 5.5 Template Type

```text
customer_interview
expert_internal_interview
```

---

## 6. Health API

### 6.1 健康检查

```http
GET /api/health
```

响应：

```json
{
  "success": true,
  "data": {
    "status": "ok",
    "version": "0.1.0",
    "services": {
      "postgres": "ok",
      "redis": "ok",
      "storage": "ok"
    }
  }
}
```

---

## 6A. Auth API

### 6A.1 登录

```http
POST /api/auth/login
```

请求：

```json
{
  "username": "admin",
  "password": "mp2026"
}
```

响应：

```json
{
  "success": true,
  "data": {
    "token": "mvp-session-token",
    "username": "admin"
  }
}
```

MVP 规则：

- 用户名和密码固定写死。
- Token 可使用简单签名 token 或服务端 session token。
- 前端保存 token，并在后续请求中使用 `Authorization: Bearer <token>`。
- 后续版本再升级为正式用户体系。

### 6A.2 获取当前登录状态

```http
GET /api/auth/me
```

响应：

```json
{
  "success": true,
  "data": {
    "username": "admin"
  }
}
```

### 6A.3 退出登录

```http
POST /api/auth/logout
```

MVP 可由前端清除本地 token；服务端可直接返回成功。

---

## 7. Project API

### 7.1 创建项目

```http
POST /api/projects
```

请求：

```json
{
  "title": "某制造业客户数字化规划访谈",
  "description": "可选备注"
}
```

响应：

```json
{
  "success": true,
  "data": {
    "project_id": "proj_001",
    "title": "某制造业客户数字化规划访谈",
    "description": "可选备注",
    "recording_count": 0,
    "total_duration_seconds": 0,
    "created_at": "2026-05-07T10:00:00+08:00",
    "updated_at": "2026-05-07T10:00:00+08:00"
  }
}
```

### 7.2 项目列表与标题检索

```http
GET /api/projects?keyword=制造业&page=1&page_size=20
```

响应：

```json
{
  "success": true,
  "data": {
    "items": [
      {
        "project_id": "proj_001",
        "title": "某制造业客户数字化规划访谈",
        "recording_count": 12,
        "total_duration_seconds": 42480,
        "latest_job_status": "summary_generating",
        "updated_at": "2026-05-07T10:00:00+08:00"
      }
    ],
    "page": 1,
    "page_size": 20,
    "total": 1
  }
}
```

### 7.3 项目详情

```http
GET /api/projects/{project_id}
```

响应字段：

```json
{
  "project_id": "proj_001",
  "title": "某制造业客户数字化规划访谈",
  "description": "",
  "recording_count": 12,
  "total_duration_seconds": 42480,
  "stats": {
    "completed": 9,
    "processing": 2,
    "failed": 1
  },
  "created_at": "2026-05-07T10:00:00+08:00",
  "updated_at": "2026-05-07T10:00:00+08:00"
}
```

### 7.4 更新项目

```http
PATCH /api/projects/{project_id}
```

请求：

```json
{
  "title": "更新后的项目标题",
  "description": "更新备注"
}
```

### 7.5 删除项目

```http
DELETE /api/projects/{project_id}
```

MVP 做硬删除。删除项目时同步删除：

- 项目记录。
- 项目下录音记录。
- 原始稿、清洁稿、纪要、问答历史。
- 任务和用量记录。
- Railway Bucket 中项目相关对象。

前端必须二次确认。

---

## 8. Recording Upload API

### 8.1 创建上传会话

```http
POST /api/projects/{project_id}/recordings/upload-session
```

用途：

- 校验文件格式和大小。
- 创建 `recordings` 记录，初始状态为 `uploading`，使文件能立即出现在项目列表。
- 返回对象 key；可返回 Railway Storage Bucket 预签名上传 URL，但当前 MVP 前端默认继续调用后端代理上传接口。

请求：

```json
{
  "file_name": "客户A-财务负责人.m4a",
  "file_size_bytes": 90177536,
  "mime_type": "audio/mp4",
  "extension": "m4a",
  "template_type": "customer_interview"
}
```

校验：

- `file_size_bytes <= 524288000`
- `extension in [mp3, wav, m4a, aac, flac, ogg, wma]`

响应：

```json
{
  "success": true,
  "data": {
    "recording_id": "rec_001",
    "object_key": "projects/proj_001/recordings/rec_001/original.m4a",
    "upload": {
      "method": "PUT",
      "url": "https://storage.railway.app/....presigned...",
      "headers": {
        "Content-Type": "audio/mp4"
      },
      "expires_in_seconds": 3600
    }
  }
}
```

### 8.2 上传文件内容并创建处理任务（当前 MVP 使用）

```http
POST /api/recordings/{recording_id}/upload-content
Content-Type: multipart/form-data
```

用途：

- 前端通过后端代理上传实际音频文件，避免浏览器直连 Bucket 的 CORS/区域配置问题。
- 后端将文件写入当前录音记录保存的存储快照。
- 写入成功后将录音状态改为 `queued`，创建 `asr_transcription` 任务并入队。
- 写入失败时录音状态改为 `failed`，前端列表显示“处理失败”。

请求字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| file | File | 是 | 实际音频文件 |

成功响应：

```json
{
  "success": true,
  "data": {
    "recording_id": "rec_001",
    "status": "queued",
    "job_id": "job_001"
  }
}
```

前端交互要求：浏览器上传进度达到 100% 后可关闭上传弹窗；文件列表依靠 `upload-session` 创建的记录展示“上传中/排队中/识别中”等状态，并通过项目页轮询刷新。


### 8.3 可选：完成直传并创建处理任务

```http
POST /api/recordings/{recording_id}/upload-complete
```

请求：

```json
{
  "object_key": "projects/proj_001/recordings/rec_001/original.m4a",
  "etag": "optional-etag",
  "file_size_bytes": 90177536
}
```

响应：

```json
{
  "success": true,
  "data": {
    "recording_id": "rec_001",
    "status": "queued",
    "job_id": "job_001"
  }
}
```

处理动作：

1. 标记录音为 `queued`。
2. 创建 `asr_transcription` 任务。
3. 写入任务队列。

### 8.4 获取录音列表

```http
GET /api/projects/{project_id}/recordings?keyword=财务&page=1&page_size=50
```

响应：

```json
{
  "items": [
    {
      "recording_id": "rec_001",
      "file_name": "客户A-财务负责人.m4a",
      "status": "completed",
      "duration_seconds": 4360,
      "file_size_bytes": 90177536,
      "template_type": "customer_interview",
      "created_at": "2026-05-07T10:00:00+08:00",
      "updated_at": "2026-05-07T10:10:00+08:00"
    }
  ],
  "page": 1,
  "page_size": 50,
  "total": 12
}
```

### 8.5 获取录音详情

```http
GET /api/recordings/{recording_id}
```

响应包含：

```json
{
  "recording_id": "rec_001",
  "project_id": "proj_001",
  "file_name": "客户A-财务负责人.m4a",
  "object_key": "projects/proj_001/recordings/rec_001/original.m4a",
  "status": "completed",
  "duration_seconds": 4360,
  "template_type": "customer_interview",
  "summary_status": "ready",
  "transcript_status": "ready",
  "latest_job_id": "job_003"
}
```

### 8.6 获取音频播放 URL

```http
POST /api/recordings/{recording_id}/play-url
```

响应：

```json
{
  "success": true,
  "data": {
    "url": "https://storage.railway.app/...presigned...",
    "expires_in_seconds": 3600
  }
}
```


### 8.7 修改录音展示名称

```http
PATCH /api/recordings/{recording_id}
Content-Type: application/json
```

用途：只修改前端展示的 `file_name`，不修改对象存储 `object_key`。

请求：

```json
{
  "file_name": "客户A-财务负责人-补充访谈.m4a"
}
```

响应：返回更新后的录音详情。

### 8.8 删除录音

```http
DELETE /api/recordings/{recording_id}
```

MVP 做硬删除。删除录音时同步删除：

- 录音记录。
- 原始稿、清洁稿、纪要。
- 录音关联任务和用量记录。
- Railway Bucket 中该录音原始文件和相关导出文件。

前端必须二次确认。

---

## 9. Transcript API

### 9.1 获取清洁稿段落

```http
GET /api/recordings/{recording_id}/transcript?source=clean
```

响应：

```json
{
  "success": true,
  "data": {
    "recording_id": "rec_001",
    "source": "clean_ai",
    "segments": [
      {
        "segment_id": "seg_001",
        "speaker": "客户",
        "start_time_ms": 192000,
        "end_time_ms": 220000,
        "text": "我们现在最大的问题不是系统数量，而是各部门对同一个指标的理解不一样。",
        "raw_segment_id": "seg_raw_001",
        "edited": false
      }
    ],
    "updated_at": "2026-05-07T10:10:00+08:00"
  }
}
```

### 9.2 获取原始稿段落

```http
GET /api/recordings/{recording_id}/transcript?source=raw
```

响应结构与清洁稿一致，但 `source = raw_asr`，文本只读。

### 9.3 更新清洁稿段落

```http
PATCH /api/transcript-segments/{segment_id}
```

请求：

```json
{
  "text": "我们现在最大的问题不是系统数量，而是各部门对同一个指标的理解不一致。",
  "speaker": "客户"
}
```

响应：

```json
{
  "success": true,
  "data": {
    "segment_id": "seg_001",
    "source": "clean_user_edited",
    "summary_stale": true
  }
}
```

处理动作：

- 保存用户编辑文本。
- 标记相关纪要 `stale = true`。

---

## 10. Summary API

### 10.1 获取纪要

```http
GET /api/recordings/{recording_id}/summary
```

响应：

```json
{
  "success": true,
  "data": {
    "summary_id": "sum_001",
    "recording_id": "rec_001",
    "template_type": "customer_interview",
    "status": "ready",
    "stale": false,
    "content": {
      "background": "本次访谈围绕客户数据治理和系统建设展开。",
      "key_findings": [
        {
          "title": "数据口径不一致",
          "detail": "客户认为多个部门对同一指标理解不一致，影响管理决策。",
          "source_refs": [
            {
              "segment_id": "seg_001",
              "start_time_ms": 192000,
              "end_time_ms": 220000
            }
          ]
        }
      ],
      "pain_points": [],
      "requirements": [],
      "risks": [],
      "todos": [],
      "quotable_quotes": []
    },
    "created_at": "2026-05-07T10:10:00+08:00"
  }
}
```

### 10.2 重新生成纪要

```http
POST /api/recordings/{recording_id}/summary/regenerate
```

请求：

```json
{
  "template_type": "customer_interview"
}
```

响应：

```json
{
  "success": true,
  "data": {
    "job_id": "job_010",
    "status": "queued"
  }
}
```

---

## 11. Project QA API

### 11.1 创建项目问答

```http
POST /api/projects/{project_id}/qa
```

请求：

```json
{
  "recording_ids": ["rec_001", "rec_002", "rec_003"],
  "question": "这几份访谈中客户最核心的需求是什么？",
  "overflow_strategy": "reject"
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| recording_ids | 最多 10 个，必须属于同一项目 |
| question | 用户问题 |
| overflow_strategy | `reject`，MVP 不做自动摘要 |

响应：

```json
{
  "success": true,
  "data": {
    "qa_session_id": "qa_001",
    "job_id": "job_020",
    "status": "queued"
  }
}
```

### 11.2 获取问答结果

```http
GET /api/qa/{qa_session_id}
```

响应：

```json
{
  "success": true,
  "data": {
    "qa_session_id": "qa_001",
    "status": "ready",
    "question": "这几份访谈中客户最核心的需求是什么？",
    "answer": "客户最核心的需求包括统一数据口径、建立数据治理机制、提升跨部门协同效率。",
    "sources": [
      {
        "recording_id": "rec_001",
        "file_name": "客户A-财务负责人.m4a",
        "segment_id": "seg_001",
        "start_time_ms": 192000,
        "end_time_ms": 220000,
        "quote": "我们现在最大的问题不是系统数量，而是各部门对同一个指标的理解不一样。"
      }
    ],
    "usage": {
      "input_tokens": 120000,
      "output_tokens": 2000,
      "compressed": false
    }
  }
}
```

### 11.3 上下文超限响应

如果 `overflow_strategy = reject` 且材料超限：

```json
{
  "success": false,
  "error": {
    "code": "LLM_CONTEXT_TOO_LONG",
    "message": "已选录音清洁稿超过模型上下文上限，请减少文件数量。",
    "details": {
      "selected_recording_count": 10,
      "estimated_tokens": 350000,
      "model_context_limit": 128000
    }
  }
}
```

---

## 12. Job API

### 12.1 最近任务

```http
GET /api/jobs/recent?page=1&page_size=20
```

### 12.2 项目任务列表

```http
GET /api/projects/{project_id}/jobs?status=failed&page=1&page_size=20
```

响应：

```json
{
  "items": [
    {
      "job_id": "job_001",
      "project_id": "proj_001",
      "recording_id": "rec_001",
      "job_type": "asr_transcription",
      "status": "failed",
      "progress": 60,
      "error_code": "ASR_POLLING_TIMEOUT",
      "error_message": "ASR 结果轮询超时",
      "started_at": "2026-05-07T10:00:00+08:00",
      "finished_at": null
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```

### 12.3 获取任务详情

```http
GET /api/jobs/{job_id}
```

### 12.4 重试任务

```http
POST /api/jobs/{job_id}/retry
```

响应：

```json
{
  "success": true,
  "data": {
    "new_job_id": "job_002",
    "status": "queued"
  }
}
```

---

## 13. Export API

### 13.1 创建导出任务

```http
POST /api/recordings/{recording_id}/exports
```

请求：

```json
{
  "export_type": "summary",
  "format": "markdown"
}
```

可选值：

```text
export_type: transcript / summary / qa_result
format: markdown
```

响应：

```json
{
  "success": true,
  "data": {
    "export_id": "exp_001",
    "job_id": "job_030",
    "status": "queued"
  }
}
```

### 13.2 获取导出下载 URL

```http
GET /api/exports/{export_id}
```

响应：

```json
{
  "success": true,
  "data": {
    "export_id": "exp_001",
    "status": "ready",
    "download_url": "https://storage.railway.app/...presigned...",
    "expires_in_seconds": 3600
  }
}
```

---

## 14. Settings API

### 14.1 获取系统设置

```http
GET /api/settings
```

响应：

```json
{
  "success": true,
  "data": {
    "file": {
      "max_size_mb": 500,
      "max_duration_hours": 3,
      "allowed_extensions": ["mp3", "wav", "m4a", "aac", "flac", "ogg", "wma"]
    },
    "qa": {
      "max_recordings": 10,
      "overflow_strategy": "reject"
    },
    "templates": ["customer_interview", "expert_internal_interview"],
    "storage": {
      "provider": "railway_bucket",
      "bucket_configured": true
    },
    "models": {
      "asr_provider": "aliyun_fun_asr",
      "llm_provider": "aliyun_qwen"
    }
  }
}
```

### 14.2 更新系统设置

```http
PATCH /api/settings
```

MVP 可只支持更新非密钥配置。密钥通过 Railway 环境变量配置，不建议通过前端页面写入数据库。

---

## 15. Usage API

### 15.1 成本用量总览

```http
GET /api/usage/overview
```

响应：

```json
{
  "success": true,
  "data": {
    "total_audio_duration_seconds": 3600000,
    "total_asr_duration_seconds": 3600000,
    "total_input_tokens": 12000000,
    "total_output_tokens": 1000000,
    "estimated_cost": 1234.56
  }
}
```

### 15.2 按项目统计

```http
GET /api/usage/projects?page=1&page_size=20
```

### 15.3 按任务明细

```http
GET /api/usage/jobs?project_id=proj_001&page=1&page_size=50
```

---

## 16. Diagnostic API

### 16.1 最近错误

```http
GET /api/diagnostics/errors?project_id=proj_001&page=1&page_size=20
```

### 16.2 创建诊断包

```http
POST /api/diagnostics/export
```

请求：

```json
{
  "time_range": "24h",
  "project_id": "proj_001",
  "recording_id": null,
  "include_clean_text": false,
  "include_qa_content": false
}
```

响应：

```json
{
  "success": true,
  "data": {
    "export_id": "exp_diag_001",
    "job_id": "job_040",
    "status": "queued"
  }
}
```

诊断包默认包含：

- 应用版本和运行环境。
- 最近操作路径。
- 接口请求记录，不包含密钥。
- 任务状态流转。
- ASR 调用状态、错误码、耗时。
- LLM 调用状态、错误码、token、耗时。
- 文件元信息。
- 最近错误。

诊断包默认不包含：

- 清洁稿正文。
- 用户问题和 AI 回答。
- API key。
- 对象存储 secret。

---

## 17. Railway Storage Bucket 调用规范

Railway Storage Buckets 提供 S3 兼容接口，后端建议通过 S3 SDK 管理对象，并只把预签名 URL 暴露给前端。

### 17.1 环境变量

```env
RAILWAY_BUCKET_ENDPOINT=
RAILWAY_BUCKET_NAME=
RAILWAY_BUCKET_ACCESS_KEY_ID=
RAILWAY_BUCKET_SECRET_ACCESS_KEY=
RAILWAY_BUCKET_REGION=auto
```

### 17.2 Object Key 规范

```text
projects/{project_id}/recordings/{recording_id}/original.{ext}
projects/{project_id}/exports/{export_id}/result.{ext}
diagnostics/{export_id}/diagnostic.zip
```

### 17.3 预签名上传

后端生成：

```text
PUT presigned URL
expires_in_seconds = 3600
Content-Type = 文件 MIME type
```

前端直接调用：

```http
PUT {presigned_upload_url}
Content-Type: audio/mp4

<binary file>
```

### 17.4 预签名下载

用途：

- 音频播放。
- 导出文件下载。
- Worker 给阿里 ASR 提供可访问的音频 URL。

有效期建议：

| 用途 | 有效期 |
|---|---|
| 前端播放 | 1 小时 |
| 导出下载 | 1 小时 |
| ASR 拉取 | 6-24 小时，视 ASR 任务排队时间而定 |

---

## 18. 阿里云 Fun-ASR 调用规范

官方文档：<https://help.aliyun.com/zh/model-studio/fun-asr-recorded-speech-recognition-restful-api>

### 18.1 环境变量

```env
ASR_API_KEY=
ASR_MODEL=fun-asr
ASR_API_URL=https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription
```

具体模型名称以阿里云文档和账号可用模型为准。

### 18.2 创建识别任务

```http
POST ${ASR_API_URL}
Authorization: Bearer ${ASR_API_KEY}
Content-Type: application/json
X-DashScope-Async: enable
```

请求示例：

```json
{
  "model": "fun-asr",
  "input": {
    "file_urls": [
      "https://storage.railway.app/...presigned-download-url..."
    ]
  },
  "parameters": {
    "channel_id": [0],
    "language_hints": ["zh", "en"],
    "enable_words": false
  }
}
```

响应示例：

```json
{
  "output": {
    "task_id": "task_xxx"
  },
  "request_id": "request_xxx"
}
```

落库：

- `processing_jobs.external_task_id = task_id`
- `model_call_logs.request_id = request_id`
- `usage_records.call_type = asr`

### 18.3 轮询任务结果

```http
GET https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}
Authorization: Bearer ${ASR_API_KEY}
```

常见任务状态：

```text
PENDING
RUNNING
SUCCEEDED
FAILED
```

轮询策略建议：

```text
前 3 分钟：每 10 秒轮询一次
3-30 分钟：每 30 秒轮询一次
30 分钟后：每 60 秒轮询一次
超过 6 小时：标记超时失败
```

成功后：

1. 获取转写结果 URL 或结果内容。
2. 解析段落、说话人、开始时间、结束时间、文本。
3. 写入 `raw_transcript_segments`。
4. 进入 `clean_transcript` 任务。

失败后：

- 记录 `ASR_TASK_FAILED`。
- 保存阿里云错误码和 request_id。
- 前端任务队列显示可重试。

---

## 19. 阿里云 LLM 调用规范

官方文档：<https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope>

MVP 建议使用 OpenAI 兼容模式，降低模型切换成本。

### 19.1 环境变量

```env
LLM_CLEAN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_CLEAN_API_KEY=
LLM_CLEAN_MODEL=qwen3.5-flash

LLM_SUMMARY_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_SUMMARY_API_KEY=
LLM_SUMMARY_MODEL=qwen3.5-flash

LLM_QA_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_QA_API_KEY=
LLM_QA_MODEL=qwen3.6-plus
```

每个节点的模型名称、URL 和 Key 独立配置。具体模型名称以阿里云账号可用模型为准。

### 19.2 Chat Completions

```http
POST https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions
Authorization: Bearer ${LLM_NODE_API_KEY}
Content-Type: application/json
```

请求示例：

```json
{
  "model": "qwen3.5-flash",
  "messages": [
    {
      "role": "system",
      "content": "你是咨询公司的访谈纪要助手。请严格基于给定转写稿输出结构化 JSON。"
    },
    {
      "role": "user",
      "content": "转写稿内容..."
    }
  ],
  "temperature": 0.2,
  "response_format": {
    "type": "json_object"
  }
}
```

响应示例：

```json
{
  "id": "chatcmpl-xxx",
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": "{\"summary\":\"...\"}"
      }
    }
  ],
  "usage": {
    "prompt_tokens": 1000,
    "completion_tokens": 200,
    "total_tokens": 1200
  }
}
```

### 19.3 清洁稿输出 JSON

```json
{
  "segments": [
    {
      "raw_segment_id": "seg_raw_001",
      "speaker": "客户",
      "start_time_ms": 192000,
      "end_time_ms": 220000,
      "clean_text": "我们现在最大的问题不是系统数量，而是各部门对同一个指标的理解不一样。"
    }
  ]
}
```

### 19.4 纪要输出 JSON

客户访谈纪要：

```json
{
  "background": "",
  "interviewee_role_and_focus": "",
  "key_findings": [
    {
      "title": "",
      "detail": "",
      "source_refs": [
        {
          "segment_id": "seg_001",
          "start_time_ms": 192000,
          "end_time_ms": 220000
        }
      ]
    }
  ],
  "pain_points": [],
  "requirements": [],
  "risks": [],
  "todos": [],
  "quotable_quotes": [
    {
      "quote": "",
      "speaker": "",
      "segment_id": "",
      "start_time_ms": 0
    }
  ]
}
```

### 19.5 项目问答输出 JSON

```json
{
  "answer": "",
  "key_points": [
    {
      "title": "",
      "detail": "",
      "sources": [
        {
          "recording_id": "rec_001",
          "file_name": "客户A-财务负责人.m4a",
          "segment_id": "seg_001",
          "start_time_ms": 192000,
          "end_time_ms": 220000,
          "quote": ""
        }
      ]
    }
  ],
  "uncertainties": []
}
```

### 19.6 模型调用落库

每次 LLM 调用写入：

- `model_call_logs`
- `usage_records`
- 相关 job 状态

必须记录：

```text
provider
model_name
call_type
input_tokens
output_tokens
duration_ms
status
error_code
request_id
context_length
is_context_overflow
```

---

## 20. 错误码规范

| 错误码 | 说明 |
|---|---|
| VALIDATION_ERROR | 参数校验失败 |
| UNAUTHORIZED | 未登录或登录 token 无效 |
| PROJECT_NOT_FOUND | 项目不存在 |
| RECORDING_NOT_FOUND | 录音不存在 |
| FILE_TOO_LARGE | 文件超过 500M |
| UNSUPPORTED_FILE_TYPE | 文件格式不支持 |
| STORAGE_PRESIGN_FAILED | 生成预签名 URL 失败 |
| STORAGE_UPLOAD_NOT_FOUND | 上传完成后对象不存在 |
| ASR_CREATE_TASK_FAILED | 创建 ASR 任务失败 |
| ASR_POLLING_TIMEOUT | ASR 轮询超时 |
| ASR_TASK_FAILED | ASR 任务失败 |
| LLM_CALL_FAILED | LLM 调用失败 |
| LLM_CONTEXT_TOO_LONG | 上下文超过模型上限 |
| SUMMARY_STALE | 清洁稿更新后纪要可能过期 |
| JOB_NOT_RETRYABLE | 任务不可重试 |
| EXPORT_FAILED | 导出失败 |
| INTERNAL_ERROR | 未分类服务端错误 |

---

## 21. Railway 环境变量清单

```env
# App
APP_ENV=production
APP_BASE_URL=https://{railway-app-domain}
MVP_SHARED_TOKEN=

# Database
DATABASE_URL=${{Postgres.DATABASE_URL}}

# Redis
REDIS_URL=${{Redis.REDIS_URL}}

# Railway Storage Bucket
RAILWAY_BUCKET_ENDPOINT=
RAILWAY_BUCKET_NAME=
RAILWAY_BUCKET_ACCESS_KEY_ID=
RAILWAY_BUCKET_SECRET_ACCESS_KEY=
RAILWAY_BUCKET_REGION=auto

# Auth
ADMIN_USERNAME=admin
ADMIN_PASSWORD=mp2026
SESSION_SECRET=

# ASR
ASR_API_URL=https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription
ASR_API_KEY=
ASR_MODEL=fun-asr

# LLM: clean transcript
LLM_CLEAN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_CLEAN_API_KEY=
LLM_CLEAN_MODEL=qwen3.5-flash

# LLM: summary
LLM_SUMMARY_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_SUMMARY_API_KEY=
LLM_SUMMARY_MODEL=qwen3.5-flash

# LLM: project QA
LLM_QA_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_QA_API_KEY=
LLM_QA_MODEL=qwen3.6-plus

# Mock mode
ASR_MOCK_ENABLED=true
LLM_MOCK_ENABLED=true
STORAGE_MOCK_ENABLED=true

# Limits
MAX_UPLOAD_SIZE_MB=500
MAX_RECORDING_DURATION_HOURS=3
MAX_QA_RECORDINGS=10
QA_OVERFLOW_STRATEGY=reject
```

---

## 22. MVP 最小 API 清单

第一批开发建议至少实现：

```text
GET    /api/health
POST   /api/auth/login
GET    /api/auth/me
POST   /api/auth/logout
POST   /api/projects
GET    /api/projects
GET    /api/projects/{project_id}
PATCH  /api/projects/{project_id}
DELETE /api/projects/{project_id}

POST   /api/projects/{project_id}/recordings/upload-session
POST   /api/recordings/{recording_id}/upload-content
POST   /api/recordings/{recording_id}/upload-complete
GET    /api/projects/{project_id}/recordings
GET    /api/recordings/{recording_id}
PATCH  /api/recordings/{recording_id}
POST   /api/recordings/{recording_id}/play-url
DELETE /api/recordings/{recording_id}

GET    /api/recordings/{recording_id}/transcript
PATCH  /api/transcript-segments/{segment_id}

GET    /api/recordings/{recording_id}/summary
POST   /api/recordings/{recording_id}/summary/regenerate

POST   /api/projects/{project_id}/qa-threads
GET    /api/projects/{project_id}/qa-threads
GET    /api/qa-threads/{thread_id}
POST   /api/qa-threads/{thread_id}/messages

GET    /api/jobs/recent
GET    /api/projects/{project_id}/jobs
GET    /api/jobs/{job_id}
POST   /api/jobs/{job_id}/retry

POST   /api/recordings/{recording_id}/exports
GET    /api/exports/{export_id}

GET    /api/settings
PATCH  /api/settings

GET    /api/usage/overview
GET    /api/usage/projects
GET    /api/usage/jobs

GET    /api/diagnostics/errors
POST   /api/diagnostics/export
```

---

## 23. 2026-05-07 API 调整：Markdown 纪要与多轮问答

### 23.1 纪要接口调整

`GET /api/recordings/{recording_id}/summary` 返回 Markdown 内容：

```json
{
  "summary_id": "sum_xxx",
  "recording_id": "rec_xxx",
  "template_type": "customer_interview",
  "status": "ready",
  "stale": false,
  "content": {
    "format": "markdown",
    "markdown": "## 访谈摘要\n...\n\n## 关键结论\n..."
  }
}
```

前端不再按固定字段渲染纪要，只渲染 `content.markdown`。

### 23.2 多轮问答数据模型

新增：

```text
qa_threads
- id
- project_id
- title
- created_at
- updated_at

qa_messages
- id
- thread_id
- project_id
- role: user / assistant
- content
- selected_recording_ids
- sources
- status
- usage
- error_code
- created_at
```

说明：

- `qa_threads.title` 默认取第一条用户问题前 10 个字。
- 每轮消息保存发送当时用户勾选的 `selected_recording_ids`。
- 对话历史按 `qa_threads.updated_at desc` 排序。

### 23.3 多轮问答 API

创建对话：

```http
POST /api/projects/{project_id}/qa-threads
```

获取项目对话列表：

```http
GET /api/projects/{project_id}/qa-threads
```

响应项：

```json
{
  "thread_id": "qath_xxx",
  "project_id": "proj_xxx",
  "title": "客户最核心需",
  "created_at": "2026-05-07T12:00:00+08:00",
  "updated_at": "2026-05-07T12:03:00+08:00",
  "last_message_at": "2026-05-07T12:03:00+08:00"
}
```

获取单个对话及消息：

```http
GET /api/qa-threads/{thread_id}
```

发送消息：

```http
POST /api/qa-threads/{thread_id}/messages
```

请求：

```json
{
  "recording_ids": ["rec_1", "rec_2"],
  "question": "这几份访谈中客户最核心的需求是什么？"
}
```

响应：

```json
{
  "thread_id": "qath_xxx",
  "user_message_id": "qamsg_user",
  "assistant_message_id": "qamsg_assistant",
  "job_id": "job_xxx",
  "status": "queued"
}
```

### 23.4 Prompt 组装规则

每次发送消息时：

```text
Prompt = 当前勾选文件清洁稿 + 最近 4 轮用户/AI 问答 + 当前问题
```

限制：

- 最多勾选 10 份文件。
- 勾选文件清洁稿累计超过 10 万字时返回 `LLM_CONTEXT_TOO_LONG`。
- MVP 不做 token 估算和摘要压缩。

### 23.5 废弃接口

以下 MVP 初版接口废弃，不再作为前端主路径：

```text
POST /api/projects/{project_id}/qa
GET  /api/projects/{project_id}/qa
GET  /api/qa/{qa_session_id}
```



## 设置接口修订（2026-05-07）

### GET /api/settings

返回系统设置，不返回 API Key 明文。

```json
{
  "basic": {
    "max_upload_size_mb": 500,
    "max_recording_duration_hours": 3
  },
  "ai": {
    "asr": {"model": "fun-asr", "url": "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription", "key": "", "key_configured": false},
    "clean": {"model": "qwen3.5-flash", "url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "key": "", "key_configured": false},
    "summary": {"model": "qwen3.5-flash", "url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "key": "", "key_configured": false},
    "qa": {"model": "qwen3.6-plus", "url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "key": "", "key_configured": false}
  }
}
```

### PATCH /api/settings

保存基础限制和各 AI 节点配置。`key` 为空字符串时表示保留已有 Key；如需清空可传 `clear_key: true`。

```json
{
  "basic": {"max_upload_size_mb": 500, "max_recording_duration_hours": 3},
  "ai": {
    "asr": {"model": "fun-asr", "url": "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription", "key": ""},
    "clean": {"model": "qwen3.5-flash", "url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "key": ""},
    "summary": {"model": "qwen3.5-flash", "url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "key": ""},
    "qa": {"model": "qwen3.6-plus", "url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "key": ""}
  }
}
```

### POST /api/projects/{project_id}/recordings/upload-session 补充

请求体可传 `duration_seconds`，后端会按当前 `max_recording_duration_hours` 做时长校验；文件大小按当前 `max_upload_size_mb` 校验。


### POST /api/settings/ai/{node}/test

测试单个 AI 节点配置是否可用。`node` 取值：`asr`、`clean`、`summary`、`qa`。请求可以传当前表单中的未保存配置；`key` 为空时后端使用已保存 Key。

```json
{
  "model": "qwen3.5-flash",
  "url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
  "key": "sk-xxx"
}
```

成功响应：

```json
{
  "node": "summary",
  "status": "passed",
  "message": "模型连接成功，已收到测试响应",
  "latency_ms": 820,
  "model": "qwen3.5-flash",
  "url": "https://dashscope.aliyuncs.com/compatible-mode/v1"
}
```

失败时返回标准错误结构，例如 `AI_KEY_REQUIRED`、`AI_URL_INVALID`、`AI_TEST_TIMEOUT`、`AI_TEST_FAILED`。


## 存储设置接口（2026-05-08）

系统设置新增 `storage` 配置，不归入 AI 设置。修改存储配置只影响新上传文件；录音记录会保存上传时的存储快照，用于历史文件播放和删除。

### GET /api/settings 补充字段

```json
{
  "storage": {
    "provider": "local",
    "bucket_name": "",
    "endpoint": "",
    "region": "auto",
    "path_prefix": "",
    "access_key_id": "",
    "secret_access_key": "",
    "access_key_configured": false,
    "secret_key_configured": false
  }
}
```

`provider` 可选：`local`、`railway_bucket`、`s3_compatible`。Key 字段不返回明文。

### PATCH /api/settings 补充字段

```json
{
  "storage": {
    "provider": "railway_bucket",
    "bucket_name": "bucket-name",
    "endpoint": "https://xxx",
    "region": "auto",
    "path_prefix": "prod",
    "access_key_id": "xxx",
    "secret_access_key": "xxx"
  }
}
```

`access_key_id` 或 `secret_access_key` 留空表示保留已有值。

### POST /api/settings/storage/test

测试当前表单中的存储配置。`local` 会测试本地目录可用性；`railway_bucket` 和 `s3_compatible` 会向 Bucket 写入、读取并删除一个 `_healthchecks` 临时对象。

```json
{
  "provider": "railway_bucket",
  "bucket_name": "bucket-name",
  "endpoint": "https://xxx",
  "region": "auto",
  "path_prefix": "prod",
  "access_key_id": "xxx",
  "secret_access_key": "xxx"
}
```

成功响应：

```json
{
  "status": "passed",
  "message": "Bucket 连接成功，已完成写入/读取/删除测试",
  "provider": "railway_bucket",
  "bucket_name": "bucket-name",
  "endpoint": "https://xxx"
}
```


---

## 12. 2026-05-08 Railway 可运行版本补充

### 12.1 ASR 外部接口编排

提交任务：

```http
POST https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription
Authorization: Bearer <ASR_API_KEY>
Content-Type: application/json
X-DashScope-Async: enable
```

请求体：

```json
{
  "model": "<ASR_MODEL>",
  "input": {"file_urls": ["<recording-presigned-download-url>"]},
  "parameters": {
    "channel_id": [0],
    "disfluency_removal_enabled": false,
    "timestamp_alignment_enabled": true
  }
}
```

说明：当模型名为 `paraformer-v2` 时，系统额外传入 `language_hints: ["zh", "en"]`；其他模型不传该字段。

查询任务：

```http
POST https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}
Authorization: Bearer <ASR_API_KEY>
Content-Type: application/json
X-DashScope-Async: enable
```

处理规则：

1. `PENDING` / `RUNNING`：按 `ASR_POLL_INTERVAL_SECONDS` 继续轮询。
2. `SUCCEEDED`：从 `output.results[]` 中选择 `subtask_status=SUCCEEDED` 且存在 `transcription_url` 的结果。
3. `FAILED` / `CANCELED`：当前 ASR Job 失败，写入错误码 `ASR_TASK_FAILED`。
4. 超过 `ASR_POLL_TIMEOUT_SECONDS`：当前 ASR Job 失败，错误信息包含外部 `task_id`。

识别结果解析：

```text
transcription_url JSON
  -> transcripts[]
  -> sentences[]
  -> RawTranscriptSegment(speaker, start_time_ms, end_time_ms, text, confidence)
```

### 12.2 LLM 外部接口编排

清洁稿、纪要、项目问答均使用阿里云百炼 OpenAI 兼容接口：

```http
POST https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions
Authorization: Bearer <LLM_API_KEY>
Content-Type: application/json
```

系统允许每个节点独立配置：

| 节点 | 默认模型 | 结果要求 |
|---|---|---|
| 清洁稿 | qwen3.5-flash | JSON，`segments[]` |
| 纪要 | qwen3.5-flash | Markdown |
| 项目问答 | qwen3.6-plus | JSON，`answer_markdown` + `sources[]` |

当模型不支持 `response_format={"type":"json_object"}` 时，后端会自动重试一次普通 Chat Completion，并通过提示词要求仅输出 JSON。

### 12.3 Railway 服务约定

Web 服务启动命令：

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Worker 服务启动命令：

```bash
python -m app.worker
```

生产环境必须设置：

```text
QUEUE_SYNC=false
STORAGE_MOCK_ENABLED=false
ASR_MOCK_ENABLED=false
LLM_MOCK_ENABLED=false
```

Web 与 Worker 必须连接同一个 PostgreSQL、Redis 和 Bucket 配置，否则会出现任务入队成功但 Worker 无法读取文件或配置的问题。


## 2026-05-09 项目页交互相关接口补充

- `PATCH /api/transcript-segments/{segment_id}` 支持请求字段 `replace_same_speaker: true`，用于用户修改发言人后批量替换当前录音中相同的旧发言人名称。
- `GET /api/projects/{project_id}/jobs` 返回的 `job_type`、`status` 在前端统一映射为中文；队列弹窗每 3 秒刷新一次，并展示 `progress` 与基于 `created_at/started_at` 的已耗时。
- `POST /api/qa-threads/{thread_id}/messages` 仍然只接收本轮勾选的 `recording_ids`；连续对话的“沿用上轮勾选”和“默认前 10 份”由前端状态管理实现。
- 录音状态在前端映射为：`created=草稿`、`uploading=上传中`、`queued=排队中`、`asr_processing=识别中`、`cleaning=清洁稿生成中`、`summary_generating=纪要生成中`、`completed=处理完成`、`failed=处理失败`。


## 2026-05-09 问答与播放接口补充

- `POST /api/qa-threads/{thread_id}/messages` 增加并发保护：同一对话存在 `queued` 或 `running` 的 assistant 消息时，返回 `QA_IN_PROGRESS`。
- `POST /api/qa-threads/{thread_id}/messages` 只允许选择 `status=completed` 的录音；未完成录音返回 `QA_RECORDING_NOT_READY`。
- `qa_answer` 任务开始运行时，后端会将 assistant 消息状态从 `queued` 更新为 `running`，前端据此持续禁用当前对话发送按钮。
- QA LLM 输入材料不再包含 `segment_id` 等内部段落 ID；返回结果会清理 `seg_xxx` 形态的内部标签，来源展示仅使用文件名、时间戳和原文摘录。
- `POST /api/recordings/{recording_id}/play-url` 在录音上传完成后即可由前端调用，用于播放原始音频；前端不再等待清洁稿完成。


## 2026-05-09 任务耗时与 ASR 诊断字段补充

### Recording 当前任务快照字段

`GET /api/projects/{project_id}/recordings` 与 `GET /api/recordings/{recording_id}` 在录音处理中会返回：

```json
{
  "current_job_type": "asr_transcription",
  "current_job_status": "running",
  "current_job_created_at": "2026-05-09T10:00:00Z",
  "current_job_started_at": "2026-05-09T10:00:03Z"
}
```

前端以 `current_job_started_at || current_job_created_at` 作为“已处理”计时起点，每秒本地刷新；后端状态仍按轮询节奏刷新。

### ProcessingJob ASR 诊断字段

`GET /api/projects/{project_id}/jobs` 与 `GET /api/jobs/{job_id}` 返回：

```json
{
  "external_task_id": "dashscope-task-id",
  "metadata": {
    "asr_task_id": "dashscope-task-id",
    "asr_file_name": "客户访谈.m4a",
    "asr_file_size_bytes": 13900000,
    "asr_diagnostics": {
      "last_event": "poll_status",
      "last_status": "RUNNING",
      "poll_count": 12,
      "events": []
    }
  }
}
```

ASR 诊断事件包括：`download_url_created`、`submit_start`、`submit_complete`、`task_id_received`、`poll_start`、`poll_status`、`result_url_received`、`result_download_start`、`result_download_complete`、`parse_complete`。MVP 保留当前默认说话人分离配置不变。
