# UI 集成契约

本文档用于首页、项目页 UI 重构时对齐前后端行为，避免只改视觉时误伤业务逻辑。

## P0：必须保持

### 录音状态枚举

后端 `Recording.status` 的正式枚举为：

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

前端可以兼容 `uploaded`、`pending`、`transcribing`、`summarizing`、`processing` 等 UI 别名，但业务判断必须先归一化到上面的后端枚举。

### 清洁稿段落编辑

接口：

```http
PATCH /api/transcript-segments/{segment_id}
```

请求体：

```json
{
  "speaker": "发言人A",
  "text": "段落正文",
  "replace_same_speaker": false
}
```

规则：

- 保存任意段落后，后端会把录音 `summary_stale` 和纪要 `stale` 置为 `true`。
- 只修改当前段落时，传 `replace_same_speaker: false` 或不传。
- 替换同一录音内所有同名发言人时，必须传 `replace_same_speaker: true`。
- 前端保存成功后应立即本地标记当前录音和纪要过期，并重新拉取项目录音列表与当前录音详情。

### 问答接口

项目问答必须优先使用流式接口：

```http
POST /api/qa-threads/{thread_id}/messages/stream
```

前端需处理 SSE 事件：

```text
created
reasoning
content
done
error
```

不要回退到旧的非流式提交逻辑，否则会丢失流式回答和思考过程展示。

## P1：强烈建议保持

- 失败录音展示 `latest_failed_job_*`，并提供内联重试按钮。
- 处理中录音展示 `current_job_progress`。
- AI 问答等待时只禁用发送按钮，输入框保持可编辑。
- 默认勾选当前项目内前 10 个已处理完成录音。
- 三栏布局保持可拖拽调整宽度。
- 音频上传完成后即可播放，不等待清洁稿完成。

## P2：兼容和防回退

- 视觉层可以自由重构，但不要改动接口字段名。
- 新增状态文案时只改显示层，不改后端状态值。
- 如果需要新增字段，先补充 `src/types.ts`，再更新此文档。
- 如果后端返回未知状态，前端可以显示原始值，但不要影响已知状态的处理逻辑。
