# AI ASR File V1.0 PRD

版本：V1.0  
更新日期：2026-05-12  
适用范围：MVP 后的第一轮企业级能力升级  
状态：草案，供产品评审与开发拆分

---

## 1. 版本定位

V1.0 的目标是把当前 MVP 从“单用户录音识别工具”升级为“多用户项目协作型知识工作台”。

V1.0 聚焦：

1. 多用户与基础权限。
2. 用户用量限额与项目/用户用量报表。
3. 项目成员管理。
4. 跨项目共享文件引用。
5. 非录音文件内容提取：PDF、扫描 PDF、Excel、Word docx、TXT/MD。
6. 项目问答支持勾选录音与非录音文件，把对应文字稿/提取文本加载到上下文中。

### 1.1 V1.0 一句话

面向咨询团队的项目知识工作台：支持多人协作管理项目资料，统一处理录音、PDF、Excel、Word、TXT 等文件，并在用量可控的前提下基于用户勾选资料进行项目问答和报告引用。

### 1.2 V1.0 与 MVP 的差异

| 模块 | MVP | V1.0 |
|---|---|---|
| 用户 | 固定 admin 账号 | 管理员手动创建多用户 |
| 权限 | 单用户默认全权限 | 普通用户/管理员 + 项目成员 |
| 项目 | 个人项目 | 个人项目 + 成员共享 + 被共享项目可见 |
| 文件 | 音频为主 | 音频 + PDF + 扫描 PDF + Excel + docx + TXT/MD |
| 问答 | 勾选录音清洁稿，一次性上下文 | 勾选文件后加载对应文字稿/提取文本，一次性上下文 |
| 用量 | 任务层记录 | 用户/项目报表 + 用户每日/月度限额 |
| 管理 | 基础设置 | 用户管理、限额配置、用量报表、任务监控 |

---

## 2. V1.0 明确不做

V1.0 不做以下内容：

1. 不做复杂 RBAC。
2. 不做企业微信 SSO，用户先由管理员手动创建。
3. 不做部门组织架构同步。
4. 不做文件摘要。
5. 不做文档切片。
6. 不做 embedding。
7. 不做向量数据库。
8. 不做 RAG/向量检索/自动召回。
9. 不做 Word `.doc`，第一版只支持 `.docx`。
10. 不做跨企业级知识库门户。

说明：V1.0 的非录音文件处理只做“内容提取”和“LLM 易读化”。摘要、切片、向量化、RAG 全部放到 V1.5 专题项目。

---

## 3. 用户与权限

### 3.1 用户角色

V1.0 只保留两个系统角色：

| 角色 | 说明 | 核心能力 |
|---|---|---|
| 普通用户 | 咨询顾问、客户经理 | 创建项目、上传文件、处理录音/文档、项目问答、导出 |
| 管理员 | PMO、产品管理员、开发维护人员 | 用户管理、限额配置、用量报表、任务监控、系统设置 |

### 3.2 用户创建

用户先由管理员手动创建。

管理员能力：

- 创建用户。
- 修改用户姓名、角色、状态。
- 停用/启用用户。
- 重置密码。
- 查看用户用量。

用户字段：

- 用户 ID
- 登录账号
- 姓名/昵称
- 密码哈希
- 角色：普通用户/管理员
- 状态：启用/停用
- 创建时间
- 最近登录时间
- 后续预留：部门、岗位、邮箱、手机号、企业微信 user_id

### 3.3 项目内权限

项目内成员权限相同，可执行：

- 查看项目。
- 查看项目文件。
- 上传录音、PDF、Excel、docx、TXT/MD。
- 发起录音转写和文档内容提取。
- 查看和编辑清洁稿。
- 重新生成录音纪要。
- 发起项目问答。
- 导出内容。
- 添加共享文件引用。

例外规则：

- 删除项目：仅项目创建人或管理员可操作。
- 取消项目共享：仅项目创建人或管理员可操作。
- 删除被其他项目引用的文件：需要二次确认。

---

## 4. 项目成员与项目共享

### 4.1 项目成员管理

项目设置页增加“成员管理”。

功能：

- 查看项目成员列表。
- 搜索用户并添加为项目成员。
- 移除项目成员。
- 显示成员加入时间。
- 显示项目创建人。

成员列表字段：

- 用户姓名
- 账号
- 角色：创建人/成员
- 加入时间
- 操作：移除

业务规则：

- 项目创建人默认是项目成员。
- 管理员可以查看和管理所有项目。
- 普通用户只能看到自己创建、加入、或有权查看的共享项目。
- 项目成员被移除后，不能继续查看该项目。

### 4.2 项目共享开关

项目设置页增加“项目共享”。

字段：

- 是否共享：开启/关闭
- 共享说明：开启后，项目成员可在其他项目中搜索并引用该项目文件

规则：

- 只有项目创建人或管理员可以开启/关闭共享。
- 共享项目不代表全企业公开，只对项目成员可见。
- 被共享人可以在自己的项目中搜索共享项目文件，并添加为引用。

### 4.3 取消共享二次确认

如果项目被其他项目引用，则取消共享前需要查询引用关系。

提示示例：

```text
当前项目有 3 个文件正在被 2 个其他项目引用。
取消共享后，这些引用文件将不能继续用于问答。
是否确认取消共享？

[取消] [确认取消共享]
```

确认后：

- 项目共享状态改为关闭。
- 已存在引用关系保留，但状态变为不可用。
- 引用方项目中显示“来源项目已取消共享，文件不可用”。

### 4.4 删除项目二次确认

项目删除权限：仅项目创建人或管理员。

如果待删除项目的文件被其他项目引用，则删除前二次确认。

提示示例：

```text
当前项目有文件被其他项目引用。
删除项目后，引用方将无法继续使用这些文件。
该操作不可恢复，是否确认删除？

[取消] [确认删除]
```

确认后：

- 原项目硬删除。
- 原项目文件、处理结果、问答历史按当前删除策略删除。
- 所有引用关系标记为 `source_deleted`。
- 引用方项目中显示“来源项目已删除，文件不可用”。

---

## 5. 跨项目文件引用

### 5.1 设计原则

跨项目共享采用“引用”，不复制文件。

好处：

- 节省存储。
- 避免重复 ASR/解析成本。
- 原项目处理结果可复用。
- 成本归属清晰。

### 5.2 添加共享文件流程

项目页左侧文件区增加按钮：

```text
[上传文件] [添加共享文件]
```

点击“添加共享文件”后打开弹窗：

```text
搜索共享项目或文件名...

文件名             类型      来源项目        状态        操作
CIO访谈.m4a        录音      客户A项目       可用        添加
调研结果.xlsx      Excel     客户A项目       可用        添加
规划材料.pdf       PDF       客户B项目       可用        添加
```

添加后，当前项目文件列表显示：

```text
CIO访谈.m4a    录音    引用自：客户A项目    可用
```

### 5.3 引用状态

| 状态 | 说明 | 是否可问答 |
|---|---|---|
| active | 来源项目仍共享且文件存在 | 是 |
| source_unshared | 来源项目取消共享 | 否 |
| source_deleted | 来源项目已删除 | 否 |
| file_deleted | 来源文件已删除 | 否 |

### 5.4 成本归属

| 场景 | 成本归属 |
|---|---|
| 原始文件上传、ASR、内容提取 | 来源项目 + 触发用户 |
| 引用项目内问答 | 当前项目 + 当前用户 |
| 引用文件重新处理 | 不允许在引用项目触发，需回来源项目处理 |

---

## 6. 文件管理与文件类型

### 6.1 支持文件类型

| 文件类型 | 格式 | V1.0 处理能力 |
|---|---|---|
| 音频 | mp3/wav/m4a/aac/flac/ogg/wma | ASR、清洁稿、纪要、问答上下文加载 |
| PDF | pdf | 文本型 PDF 直接提取；扫描 PDF OCR 提取 |
| Excel | xlsx/xls/csv | Sheet、表头、表格内容提取为 LLM 易读文本 |
| Word | docx | 标题、段落、列表、表格提取 |
| 文本 | txt/md | 编码识别、文本清洗、原文加载 |

Word 范围说明：

- V1.0 只支持 `.docx`。
- `.doc` 暂不支持，后续如需要可通过 LibreOffice 转换方案评估。

### 6.2 文件列表

项目页左侧从“录音列表”升级为“文件列表”。

字段：

- 文件名
- 文件类型
- 来源：本项目/引用
- 处理状态
- 时长/页数/Sheet 数/字符数
- 更新时间
- 操作

状态：

```text
上传中
待处理
ASR识别中
清洁稿生成中
纪要生成中
内容提取中
OCR识别中
处理完成
处理失败
不可用
```

### 6.3 文件详情页

文件详情页必须展示“提取文字稿”。

不同文件展示：

| 类型 | 中间区展示 |
|---|---|
| 音频 | 清洁稿、原始稿、音频播放器 |
| PDF | 提取文字稿，按页展示；扫描件展示 OCR 结果 |
| Excel | 提取文字稿，按 Workbook/Sheet 展示 |
| docx | 提取文字稿，保留标题、段落、表格 |
| TXT/MD | 提取文字稿，即清洗后的原文 |
| 引用文件 | 同源文件内容，只读展示 |

文件详情页还需要展示：

- 提取引擎。
- 提取时间。
- 提取字符数。
- 处理警告，例如“疑似扫描件”“部分表格识别可能不完整”。
- 重新处理按钮。

---

## 7. 非录音文件内容提取方案

### 7.1 总体原则

V1.0 不做摘要、不做切片、不做向量化。

目标是把文件转换为 LLM 易读的“提取文字稿”：

```text
原始文件
  ↓
内容提取
  ↓
结构保留和文本清洗
  ↓
生成 extracted_text
  ↓
文件详情页展示
  ↓
问答时按用户勾选文件加载全文
```

### 7.2 PDF 内容提取

PDF 分两类：

1. 文本型 PDF。
2. 扫描型 PDF。

#### 7.2.1 PDF 类型检测

检测逻辑：

```text
抽取前 3 页文本
  ↓
如果平均每页可提取字符数 >= 50：文本型 PDF
否则：疑似扫描 PDF
```

增强判断：

- 页面可提取字符数少。
- 页面中图片面积占比高。
- 文字对象数量少。

#### 7.2.2 文本型 PDF 流程

```text
上传 PDF
  ↓
检测为文本型 PDF
  ↓
按页提取正文
  ↓
提取表格
  ↓
清理页眉页脚、页码、重复空白
  ↓
按页组装为 extracted_text
  ↓
处理完成
```

推荐工具：

- PyMuPDF：页面级文本、元数据、基础版面信息。
- pdfplumber：文本型 PDF 表格提取补充。

输出格式：

```markdown
# 文件：客户调研报告.pdf
类型：PDF
页数：18
提取方式：文本提取

## 第 1 页

这里是第 1 页正文内容……

### 表格 1：第 1 页

| 指标 | 2024年 | 2025年 |
|---|---:|---:|
| 收入 | 1200 | 1500 |
| 成本 | 800 | 900 |

## 第 2 页

这里是第 2 页正文内容……
```

说明：这里按页展示不是“切片”，只是保留 PDF 原始页码结构，便于用户查看和后续引用。

#### 7.2.3 扫描 PDF 流程

扫描 PDF OCR 是 V1.0 必做。

推荐方案：PaddleOCR。

流程：

```text
上传 PDF
  ↓
检测为疑似扫描 PDF
  ↓
逐页渲染为图片
  ↓
PaddleOCR 识别文字
  ↓
如页面疑似表格，尝试表格结构识别
  ↓
按页组装为 extracted_text
  ↓
处理完成
```

推荐实现：

- 使用 PyMuPDF 将 PDF 页面渲染为图片。
- 使用 PaddleOCR 进行中文/英文 OCR。
- 表格场景优先评估 PaddleOCR PP-Structure。

扫描 PDF 输出格式：

```markdown
# 文件：扫描合同.pdf
类型：PDF
页数：12
提取方式：OCR识别
识别提示：该文件为扫描件，OCR 结果可能存在错别字或漏识别，请以原件为准。

## 第 1 页 OCR文本

这里是 OCR 识别后的文本……

## 第 2 页 OCR文本

这里是 OCR 识别后的文本……
```

#### 7.2.4 PDF 限制

建议 V1.0 设置限制：

- 单个 PDF 最大页数：默认 100 页，可配置。
- 扫描 PDF OCR 最大页数：默认 50 页，可配置。
- 超出限制时提示用户拆分文件或联系管理员调整上限。

---

## 8. Excel 内容提取方案

### 8.1 目标

Excel 的目标不是简单抽文字，而是把 Workbook/Sheet/表格转换成 LLM 易读的结构化文本。

需要保留：

- Workbook 概览。
- Sheet 名称。
- 有效区域。
- 表头。
- 行号。
- 单元格范围。
- 表格数据。
- 合并单元格处理说明。
- 公式值处理说明。

### 8.2 推荐工具

- `openpyxl`：读取 xlsx，保留 Sheet、单元格、合并单元格、公式等结构。
- `pandas`：读取 csv/xlsx，适合表格化处理。
- `.xls`：可先用 `xlrd` 或后续转换方案；如兼容成本高，可在开发时降级处理。

### 8.3 处理流程

```text
上传 Excel
  ↓
读取 Workbook
  ↓
枚举 Sheet
  ↓
识别每个 Sheet 的有效区域
  ↓
识别表头
  ↓
处理合并单元格
  ↓
提取显示值，必要时保留公式
  ↓
转换为 LLM 易读文本
  ↓
处理完成
```

### 8.4 表格输出规则

#### 小表：Markdown 表格

适合行列不多的表格。

```markdown
# 文件：调研结果.xlsx
类型：Excel
Sheet 数：2

## Workbook 概览

- 用户调研：20 行，6 列
- 访谈对象：8 行，5 列

## Sheet：用户调研
有效区域：A1:F20
行数：20
列数：6

| 行号 | 客户名称 | 行业 | 规模 | 主要痛点 | 预算 |
|---:|---|---|---|---|---:|
| 2 | A公司 | 制造 | 500人 | 系统割裂 | 200万 |
| 3 | B公司 | 零售 | 1200人 | 数据口径不统一 | 300万 |
```

#### 大表：TSV 文本

大表不建议 Markdown 表格，因为会浪费 token。

建议超过 50 行或 12 列时，转为 TSV 风格文本。

```text
# 文件：调研结果.xlsx
类型：Excel
Sheet 数：1

## Sheet：用户调研
有效区域：A1:F500
行数：500
列数：6
字段：客户名称, 行业, 规模, 主要痛点, 预算, 备注

表格数据 TSV：
row_id	客户名称	行业	规模	主要痛点	预算	备注
2	A公司	制造	500人	系统割裂	200万	-
3	B公司	零售	1200人	数据口径不统一	300万	-
4	C公司	金融	800人	流程自动化不足	500万	-
```

### 8.5 合并单元格处理

规则：

- 合并单元格的值向下/向右填充到被合并区域。
- 在 extracted_text 中保留说明：

```text
说明：原表存在合并单元格，系统已将合并单元格的值填充到对应行列，便于模型理解。
```

### 8.6 公式处理

默认只提取显示值。

如果可获得公式，可在文件详情中保留公式信息，但问答上下文默认只加载显示值。

示例：

```text
销售额：1500
公式：=SUM(B2:B10)
```

### 8.7 Excel 大文件限制

V1.0 接受“超上下文则提示减少文件”的策略。

规则：

- 单个 Excel 提取文本最大字符数：可配置，默认 100,000 字。
- 单次问答加载总字符数：可配置，默认按模型上下文上限估算。
- 超出时不做摘要、不做切片、不做压缩，直接提示用户减少勾选文件。

提示示例：

```text
已选文件提取文本过长，超过当前模型上下文上限。
V1.0 暂不支持大表智能检索，请减少勾选文件，或拆分 Excel 后重新上传。
```

---

## 9. Word / TXT 内容提取

### 9.1 Word docx

V1.0 只支持 `.docx`。

提取内容：

- 标题。
- 段落。
- 列表。
- 表格。
- 页眉页脚可选。

不处理：

- `.doc`。
- 批注。
- 修订历史。
- 复杂文本框和浮动对象。

推荐工具：

- `python-docx`：基础稳定，适合标题、段落、表格提取。
- `mammoth`：适合 docx 转 HTML/Markdown 风格文本，可作为增强方案评估。

输出示例：

```markdown
# 文件：项目访谈提纲.docx
类型：Word
提取方式：docx文本提取

## 访谈背景

正文内容……

## 表格：需求清单

| 编号 | 需求 | 优先级 |
|---|---|---|
| 1 | 数据统一管理 | 高 |
| 2 | 自动生成报表 | 中 |
```

### 9.2 TXT / Markdown

支持：

- `.txt`
- `.md`

处理流程：

```text
读取文件
  ↓
检测编码：UTF-8 / UTF-8 BOM / GBK
  ↓
统一转 UTF-8
  ↓
清理异常空白和控制字符
  ↓
保留原始段落
```

推荐工具：

- `charset-normalizer` 或等价编码探测方案。

---

## 10. V1.0 问答机制

### 10.1 问答原则

V1.0 不做 RAG，也不自动检索相关段落。

用户勾选哪些文件，系统就加载哪些文件的文字内容：

- 音频：加载清洁稿。
- PDF：加载 extracted_text。
- 扫描 PDF：加载 OCR extracted_text。
- Excel：加载 extracted_text。
- docx：加载 extracted_text。
- TXT/MD：加载 extracted_text。

### 10.2 上下文构造

Prompt 结构建议：

```text
你是咨询顾问的项目资料分析助手。
请优先基于用户勾选的文件内容回答问题。
如果文件内容不足以回答，可以说明“不足以判断”。
如果使用一般分析补充，请明确区分“文件依据”和“一般分析”。

<file name="客户访谈.m4a" type="audio">
...
</file>

<file name="规划材料.pdf" type="pdf">
...
</file>

<file name="调研结果.xlsx" type="excel">
...
</file>
```

### 10.3 上下文超限处理

如果所选文件提取文本超过上下文上限：

- 不自动摘要。
- 不自动切片。
- 不自动压缩。
- 不做向量召回。
- 直接提示用户减少勾选文件。

提示示例：

```text
已选文件内容超过当前模型上下文上限。
请减少勾选文件后重试。
```

### 10.4 文件来源引用

虽然 V1.0 不做 RAG，但仍可要求 LLM 在回答中引用文件名。

引用格式建议：

```text
来源：客户访谈.m4a 12:35
来源：规划材料.pdf 第 8 页
来源：调研结果.xlsx / Sheet：用户调研
来源：访谈提纲.docx
```

PDF/Excel/docx 的来源引用依赖 extracted_text 中保留的页码、Sheet、标题等结构。

---

## 11. 用量控制与报表

### 11.1 用量记录

所有模型和文件处理任务统一写入 `usage_records`。

字段：

- usage_id
- user_id
- project_id
- file_id / recording_id
- job_id
- usage_type：asr / clean / summary / qa / pdf_extract / pdf_ocr / excel_extract / docx_extract
- model_name
- input_tokens
- output_tokens
- audio_duration_seconds
- file_size_bytes
- status
- created_at

### 11.2 项目维度报表

字段：

- 项目名称
- 项目创建人
- 项目成员数
- 文件数量
- 录音文件数量
- 录音文件总时长
- 项目内问答次数
- QA 输入 token
- QA 输出 token
- 总 token
- 最近使用时间

筛选：

- 时间范围
- 项目名称
- 项目创建人
- 是否共享

展示：

- 指标卡
- 趋势图
- 项目用量表
- 导出 CSV

### 11.3 用户维度报表

字段：

- 用户姓名
- 录音文件处理数量
- 录音识别总时长
- 对话问答次数
- QA 输入 token
- QA 输出 token
- 总 token
- 今日用量
- 本月用量
- 最近使用时间

筛选：

- 时间范围
- 用户
- 角色
- 状态

展示：

- 用户排行
- 用户明细表
- 单用户用量趋势

### 11.4 用户限额

V1.0 只针对用户限额，不做项目/部门限额。

限额项：

| 限额 | 粒度 |
|---|---|
| 每日最大 ASR 时长 | 用户/天 |
| 每月最大 ASR 时长 | 用户/月 |
| 每日 QA token 上限 | 用户/天 |
| 每月 QA token 上限 | 用户/月 |

限制逻辑：

- 上传录音前检查 ASR 时长额度。
- 提交问答前检查 QA token 额度。
- 超限时阻止提交。
- 接近上限时提示。

### 11.5 右上角用户用量入口

右上角由“退出”改为用户名称。

```text
录音分析工作台                         张三 ▾
```

点击或 hover 后展示：

```text
张三
普通用户

今日用量
ASR：1.2h / 3h        [======----]
QA Token：18k / 50k   [====------]

本月用量
ASR：12.6h / 30h      [========--]
QA Token：210k / 500k [====------]

退出登录
```

---

## 12. 管理后台

V1.0 管理后台包含：

1. 用户管理。
2. 用户限额配置。
3. 项目用量报表。
4. 用户用量报表。
5. 任务监控。
6. 系统设置。

### 12.1 用户限额配置

管理员可配置：

- 默认用户限额。
- 单个用户限额。
- 是否启用限额。

### 12.2 任务监控

任务列表字段：

- 任务 ID
- 用户
- 项目
- 文件
- 任务类型
- 状态
- 开始时间
- 结束时间
- 错误信息
- 重试操作

任务类型：

- ASR
- 清洁稿生成
- 纪要生成
- PDF文本提取
- PDF OCR
- Excel内容提取
- docx内容提取
- TXT内容提取
- 问答

---

## 13. 数据模型草案

### 13.1 users

- id
- username
- display_name
- password_hash
- role
- status
- created_at
- last_login_at

### 13.2 user_quotas

- id
- user_id
- daily_asr_seconds_limit
- monthly_asr_seconds_limit
- daily_qa_token_limit
- monthly_qa_token_limit
- enabled
- updated_at

### 13.3 project_members

- id
- project_id
- user_id
- member_type：owner/member
- created_at

### 13.4 projects 新增字段

- owner_id
- shared_enabled
- shared_updated_at

### 13.5 project_files

- id
- project_id
- owner_user_id
- file_name
- file_type：audio/pdf/excel/docx/txt/md
- object_key
- mime_type
- file_size_bytes
- duration_seconds
- page_count
- sheet_count
- extracted_text
- extracted_char_count
- extraction_engine
- extraction_warnings
- status
- source：uploaded/referenced
- source_project_id
- source_file_id
- created_at
- updated_at

说明：当前 `recordings` 可以短期保留，同时新增 `project_files` 做统一文件层。音频文件可与 `recordings` 一对一关联。

### 13.6 project_file_references

- id
- source_project_id
- source_file_id
- target_project_id
- target_file_id
- added_by_user_id
- status：active/source_unshared/source_deleted/file_deleted
- created_at
- updated_at

### 13.7 file_extraction_artifacts

该表不是切片，不参与 RAG/召回，仅用于文件详情展示与排查解析质量。

- id
- file_id
- artifact_type：page_text / ocr_page_text / table / sheet_text / docx_text
- page_number
- sheet_name
- cell_range
- content_text
- content_json
- created_at

如果开发排期紧张，V1.0 可以先只存 `project_files.extracted_text`，`file_extraction_artifacts` 作为增强项。

---

## 14. API 草案

### 14.1 用户

```http
POST /api/admin/users
GET /api/admin/users
PATCH /api/admin/users/{user_id}
POST /api/admin/users/{user_id}/reset-password
```

### 14.2 限额与用量

```http
GET /api/me/usage
GET /api/admin/usage/projects
GET /api/admin/usage/users
GET /api/admin/users/{user_id}/quota
PATCH /api/admin/users/{user_id}/quota
```

### 14.3 项目成员

```http
GET /api/projects/{project_id}/members
POST /api/projects/{project_id}/members
DELETE /api/projects/{project_id}/members/{user_id}
```

### 14.4 项目共享

```http
PATCH /api/projects/{project_id}/sharing
GET /api/projects/{project_id}/references/check
DELETE /api/projects/{project_id}
```

### 14.5 文件

```http
POST /api/projects/{project_id}/files/upload-session
POST /api/files/{file_id}/upload-content
GET /api/projects/{project_id}/files
GET /api/files/{file_id}
DELETE /api/files/{file_id}
POST /api/files/{file_id}/reprocess
GET /api/files/{file_id}/extracted-text
GET /api/files/{file_id}/extraction-artifacts
```

### 14.6 共享文件引用

```http
GET /api/shared-files/search
POST /api/projects/{project_id}/file-references
DELETE /api/projects/{project_id}/file-references/{reference_id}
```

---

## 15. 页面原型草案

### 15.1 首页

```text
录音分析工作台                                      张三 ▾
----------------------------------------------------------------
[搜索项目......] [+ 新建项目]

Tab: 我的项目 | 共享给我的项目 | 最近使用

项目名称        文件数  录音时长(h)  成员数  共享  最近更新     ...
客户A项目       18      12.5         4      是    2026-05-11   ...
客户B项目       6       3.2          2      否    2026-05-09   ...
```

### 15.2 项目页

```text
客户A项目                                      项目设置  ...
----------------------------------------------------------------
左侧文件区              中间文件内容区                 右侧功能区
[上传文件] [添加共享文件]   提取文字稿/清洁稿/表格文本       [纪要/提取文本] [问答]

CIO访谈.m4a      处理完成
调研结果.xlsx    处理完成
规划材料.pdf     OCR识别中
访谈提纲.docx    处理完成
共享/访谈B.m4a   可用
```

### 15.3 文件详情页

```text
文件：调研结果.xlsx
类型：Excel
状态：处理完成
提取字符数：86,240
提取引擎：openpyxl + pandas

[提取文字稿]
# 文件：调研结果.xlsx
类型：Excel
Sheet 数：3
...
```

### 15.4 用户用量浮层

```text
张三
普通用户

今日用量
ASR：1.2h / 3h        [======----]
QA Token：18k / 50k   [====------]

本月用量
ASR：12.6h / 30h      [========--]
QA Token：210k / 500k [====------]

退出登录
```

---

## 16. 关键业务流程

### 16.1 管理员创建用户

```text
管理员进入用户管理
  ↓
点击创建用户
  ↓
填写账号、姓名、初始密码、角色
  ↓
保存
  ↓
用户可登录系统
```

### 16.2 上传文本型 PDF

```text
用户进入项目
  ↓
上传 PDF
  ↓
检测为文本型 PDF
  ↓
按页提取文本与表格
  ↓
生成 extracted_text
  ↓
文件详情页可查看提取文字稿
  ↓
可参与问答
```

### 16.3 上传扫描 PDF

```text
用户进入项目
  ↓
上传 PDF
  ↓
检测为扫描 PDF
  ↓
逐页渲染图片
  ↓
PaddleOCR 识别
  ↓
生成 OCR extracted_text
  ↓
文件详情页可查看 OCR 文字稿
  ↓
可参与问答
```

### 16.4 上传 Excel

```text
用户进入项目
  ↓
上传 Excel
  ↓
解析 Workbook/Sheet/表头/有效区域
  ↓
转换为 Markdown 或 TSV 文本
  ↓
生成 extracted_text
  ↓
文件详情页可查看提取文字稿
  ↓
可参与问答
```

### 16.5 项目问答

```text
用户勾选文件
  ↓
系统加载音频清洁稿和非录音文件 extracted_text
  ↓
检查上下文长度
  ↓
未超限：提交 LLM
  ↓
超限：提示减少勾选文件
```

---

## 17. 验收标准

### 17.1 用户与权限

- 管理员可以创建、停用、重置用户密码。
- 普通用户只能查看自己创建、加入、或有权限访问的共享项目。
- 项目删除仅项目创建人或管理员可操作。

### 17.2 用量控制

- 系统可记录用户和项目维度的 ASR 时长、QA 次数、输入/输出 token。
- 管理员可查看项目维度和用户维度报表。
- 管理员可配置用户每日/月度 ASR 和 QA token 上限。
- 用户右上角可查看今日和本月用量进度条。
- 超限时系统阻止上传录音或发起问答。

### 17.3 项目共享

- 项目可以开启/关闭共享。
- 项目成员可在其他项目中搜索可用共享文件。
- 添加共享文件后，当前项目可使用该文件问答。
- 来源项目取消共享或删除后，引用文件不可继续使用。
- 来源项目取消共享或删除前，如存在引用，必须二次确认。

### 17.4 非录音文件

- 支持上传文本型 PDF 并提取文本。
- 支持上传扫描 PDF 并通过 PaddleOCR 提取文字。
- 支持上传 xlsx/xls/csv 并提取为 LLM 易读文本。
- 支持上传 docx 并提取标题、段落、表格。
- 支持上传 txt/md 并提取清洗后的原文。
- 文件详情页展示提取文字稿。
- V1.0 不生成文件摘要。
- V1.0 不生成文档切片。

### 17.5 问答

- 用户可勾选录音、PDF、Excel、docx、TXT/MD 参与问答。
- 系统按用户勾选加载对应清洁稿或 extracted_text。
- 超上下文时提示减少勾选文件。
- 不做向量检索、不做自动召回、不做自动摘要压缩。

---

## 18. 开发拆分建议

### Sprint 1：多用户与项目成员

- users 表与登录改造。
- 用户管理后台。
- project_members。
- 项目列表权限过滤。
- 项目删除权限限制。

### Sprint 2：用量限额和报表

- usage_records 补齐 user_id。
- 用户限额配置。
- 上传/问答前限额校验。
- 用户右上角用量卡片。
- 项目/用户用量报表。

### Sprint 3：项目共享和文件引用

- 项目共享开关。
- 共享文件搜索。
- 文件引用关系。
- 取消共享/删除项目引用检查。
- 引用文件不可用状态。

### Sprint 4：文件模型升级与内容提取

- project_files 表。
- 统一文件列表。
- PDF 文本提取。
- 扫描 PDF OCR。
- Excel 内容提取。
- docx 内容提取。
- TXT/MD 内容提取。
- 文件详情页展示 extracted_text。

### Sprint 5：项目文件问答升级

- 问答支持勾选多类型文件。
- 上下文构造支持 audio/pdf/excel/docx/txt。
- 上下文超限校验。
- 来源文件名引用提示。

---

## 19. V1.5 专题：摘要、切片、向量检索

V1.5 单独立项，建议目标：

- 文件级摘要。
- 文档切片。
- embedding 模型。
- 向量数据库或 PostgreSQL pgvector。
- 音频/PDF/Excel/docx/TXT 统一向量化。
- 项目内 RAG。
- 引用文件参与 RAG。
- 来源片段召回和重排序。

V1.0 只做内容提取，不做以上能力。

---

## 20. 开源解析方案来源

- PyMuPDF 文档：https://pymupdf.readthedocs.io/en/latest/the-basics.html
- pdfplumber GitHub：https://github.com/jsvine/pdfplumber
- PaddleOCR / PP-Structure：https://github.com/PaddlePaddle/PaddleOCR
- Docling GitHub：https://github.com/docling-project/docling
- Marker GitHub：https://github.com/datalab-to/marker
- MinerU GitHub：https://github.com/opendatalab/MinerU

V1.0 推荐：

- 文本型 PDF：PyMuPDF + pdfplumber。
- 扫描 PDF：PaddleOCR，必要时评估 PP-Structure。
- Excel：openpyxl + pandas。
- Word docx：python-docx，必要时评估 mammoth。
- TXT/MD：charset-normalizer + 文本清洗。

---

## 15. V1.0 开发落地补充（2026-05-12）

本轮实现已按 V1.0 范围落地以下工程决策：

1. 登录从固定账号升级为数据库用户，启动时自动创建默认管理员 `admin / mp2026`，后续用户由管理员在管理后台手动创建。
2. 项目新增 `owner_id`、`is_shared` 和 `project_members`；普通用户可访问自己创建、被加入、或已共享可见的项目，项目删除仅创建人/管理员可执行。
3. 新增统一文件层 `project_files`，音频继续兼容原 `recordings`，非录音文件支持 PDF、Excel、docx、TXT/MD 的内容提取。
4. 新增共享文件引用 `project_file_references`；引用不复制源文件，源项目取消共享/删除或源文件删除后，引用状态变为不可用，不能参与问答。
5. 项目问答优先使用 `file_ids`，音频加载清洁稿，非音频加载 `extracted_text`；上下文超过 10 万字直接提示减少文件，不做摘要、切片、RAG。
6. 新增用户限额和报表：`user_quotas`、`usage_records.user_id/file_id`，管理后台提供用户管理、限额配置、项目用量报表、用户用量报表、任务监控。
7. 首页右上角升级为用户用量下拉卡片，展示今日/月度 ASR 与问答 Token 使用进度，并保留退出入口。
8. UI 延续当前蓝灰白底、紧凑表格、圆角卡片、状态圆点、三列项目页风格；项目页左侧升级为文件列表，中间按文件类型展示清洁稿或提取文字稿。

### 15.1 OCR 说明

代码层已为扫描 PDF 预留 PaddleOCR 调用路径：当 PDF 文本提取结果过少时，会尝试导入 `paddleocr` 并逐页 OCR。当前部署依赖默认不强制安装 PaddleOCR，以避免 Railway 构建体积和系统依赖风险；如需在 V1.0 正式环境启用扫描 PDF OCR，需要单独为 Worker 镜像安装 PaddleOCR/PaddlePaddle，或拆分 OCR Worker 服务。

---

### 15.2 OCR 独立服务落地

为降低 Web/Worker 镜像体积和 Railway 构建失败风险，扫描 PDF OCR 不直接安装在主应用镜像中，而是拆为独立 Railway 服务：

- 目录：`ocr/`
- 技术：FastAPI + PyMuPDF + PaddleOCR
- 触发：PDF 文本层提取结果过少时，Worker 调用 OCR 服务。
- 配置：Worker 使用 `OCR_SERVICE_URL` 和 `OCR_SERVICE_TOKEN` 调用。
- 网络：推荐使用 Railway Private Networking，不暴露 OCR 公网域名。

这仍符合 V1.0“扫描 PDF 必做 OCR”的产品要求，同时将 PaddleOCR 的重依赖风险隔离在 OCR Worker 服务中。
