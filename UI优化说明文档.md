# UI 优化说明文档

> 记录本项目前端（`frontend/src/App.tsx` + `frontend/src/styles.css`）自初版以来所有 UI 改动，供前后端对齐参考。

---

## 一、首页（Home）重构

### 1.1 整体视觉重构

原版使用 Ant Design 默认 Table + 顶栏布局，新版重构为类 NotebookLM 风格：

| 区域 | 原版 | 新版 |
|------|------|------|
| 顶栏 | `<Header>` + 白底 | sticky 顶栏，品牌 Logo + 标题 + 设置/退出按钮 |
| 内容区 | 表格列表 | Hero 区（标题 + 搜索框 + 新建按钮）+ 卡片网格 |
| 项目展示 | 表格行 | `project-card` 卡片，悬停有上浮阴影动效 |
| 操作入口 | 表格内 inline 按钮 | 卡片右上角三点菜单（`...`），含"修改项目名称"和"删除项目" |

### 1.2 新建/修改项目弹窗优化

- 宽度从默认改为 `480px`
- 新增 `project-modal-label` 字段说明
- Input 改为 `size="large"`，添加 `showCount` 字符计数，`maxLength={50}`
- 确认按钮在名称为空时 `disabled`

### 1.3 空状态优化

项目列表为空时显示引导插画 + 文案（"还没有项目，点击「新建项目」开始"）。

---

## 二、项目内页（ProjectPage）优化

### 2.1 顶栏：删除项目移入下拉菜单

**改动前：** 顶栏右侧有红色"删除项目"按钮，醒目且容易误触。

**改动后：**
- 改为"更多操作"按钮（`<Dropdown menu={projectMoreMenu}>`）
- 删除项目移入下拉菜单 `{ key: 'delete', label: '删除项目', danger: true }`
- 返回首页按钮文案改为"← 返回首页"，层次更清晰

### 2.2 页面路由：刷新保持项目页

**改动前：** 使用 `useState` 管理视图，刷新后 state 重置，回到首页。

**改动后：** 使用 `window.location.hash` 同步路由状态：
- 进入项目页 → hash 写入 `#/project/{project_id}`
- 进入设置页 → hash 写入 `#/settings`
- 返回首页 → hash 清空
- 页面初始化和 `hashchange` 事件时解析 hash 恢复状态，刷新不丢失

### 2.3 左侧文件列表优化

**状态显示：**
- 改动前：`<Tag>{rec.status}</Tag>`（英文原始值）
- 改动后：彩色圆点 + 中文状态文字，通过 `STATUS_LABEL` 映射表转换：

```ts
const STATUS_LABEL = {
  uploaded: '已上传', pending: '排队中', queued: '排队中',
  transcribing: '转写中', cleaning: '整理中', summarizing: '生成纪要',
  processing: '处理中', completed: '已完成', failed: '失败'
};
```

- 圆点颜色语义：绿色（已完成）、黄色（处理中各阶段）、红色（失败）、灰色（排队/等待）

**其他保留逻辑：**
- `recordings.length >= 30` 时显示"已达到建议上限" Tag
- Checkbox 勾选上限 10 份用于问答
- 删除按钮 `e.stopPropagation()` 防止触发选中

### 2.4 中间转写稿工具栏：分两行

**改动前：** 文件名、状态、三个操作按钮全在一行，拥挤。

**改动后：**
- 第一行（`.middle-toolbar-info`）：文件名（加粗）+ 状态圆点
- 第二行（`.middle-toolbar-actions`）：三个 `size="small"` 按钮
  - "显示原始稿" / "隐藏原始稿"（切换 `showRaw`）
  - "重新生成纪要"（调用 `regenerateSummary`）
  - "导出清洁稿"（调用 `exportMd('transcript')`）

### 2.5 转写稿 SegmentEditor：发言人 badge + 独立编辑

**改动前：** 单一 `editing` 状态，进入编辑时展示发言人 Input + 正文 TextArea 合并在一起。

**改动后：** 拆分为两个独立编辑模式：

| 编辑目标 | 触发方式 | state | 保存操作 |
|---------|---------|-------|---------|
| 发言人名称 | 点击 speaker-badge | `editingSpeaker` | `saveSpeaker()` → `onSave(draft)` |
| 正文内容 | 点击"编辑"按钮 | `editingText` | `saveText()` → `onSave(draft)` |

- 发言人 badge 平时可见，hover 时背景变深提示可点击，点击后变为 inline Input
- 正文编辑框改为 `autoSize={{ minRows: 3 }}`，高度自适应内容，与显示态视觉一致
- 取消编辑时恢复 draft 为原始 segment（`setDraft(segment)`）
- 保存时两者都调用同一个 `onSave(draft)`，后端接口不变（`PATCH /api/transcript-segments/{id}` 传 `{ speaker, text }`）

### 2.6 纪要面板：stale 提示自动刷新

**问题根因：** `saveSegment` 原来只调用 `loadSelected()`，该方法只刷新 `segments` 和 `summary`，不刷新 `recordings` 数组。而 `summary_stale` 字段在 `Recording` 对象上，导致右侧纪要面板无法感知到状态变化。

**修复：** `saveSegment` 同时调用：
```ts
void loadProject();   // 刷新 recordings 列表，更新 summary_stale 字段
void loadSelected();  // 刷新转写稿和纪要内容
```

保存后，`selectedRecording.summary_stale` 变为 `true`，驱动 `SummaryView` 显示：
- 橙色提示 Tag："清洁稿已编辑，纪要可能过期"
- 中间工具栏的"重新生成纪要"按钮随时可用

---

## 三、与后端的接口约定

以下接口行为需要前后端对齐：

### 3.1 `PATCH /api/transcript-segments/{segment_id}`

**请求体：**
```json
{ "speaker": "发言人 1", "text": "正文内容..." }
```

**期望副作用（后端需确认）：**
1. 更新 `transcript_segments` 表对应记录的 `speaker` 和 `text` 字段
2. 将对应 `recording` 的 `summary_stale` 字段设为 `true`
3. 如有"替换全部同名发言人"逻辑，应在此接口中处理（前端目前传的是单条 segment 的 speaker，后端若有批量替换需明确行为）

**前端在保存后会调用：**
- `GET /api/projects/{project_id}`（刷新 recordings 列表，读取最新 `summary_stale`）
- `GET /api/recordings/{recording_id}/transcript?source=clean`（刷新转写稿）
- `GET /api/recordings/{recording_id}/summary`（刷新纪要内容）

### 3.2 `GET /api/projects/{project_id}/recordings`

返回的 `Recording` 对象中需包含 `summary_stale: boolean` 字段，这是驱动前端"纪要过期"提示的核心字段。

### 3.3 `POST /api/recordings/{recording_id}/summary/regenerate`

触发后端重新生成纪要任务，前端会在 1 秒后重新拉取项目和录音信息。

---

## 四、文件改动清单

| 文件 | 改动类型 |
|------|---------|
| `frontend/src/App.tsx` | 路由逻辑、SegmentEditor、saveSegment、状态映射、顶栏、工具栏、弹窗 |
| `frontend/src/styles.css` | 全量重构项目页样式、首页样式、新增 badge/dot/segment 等样式类 |
