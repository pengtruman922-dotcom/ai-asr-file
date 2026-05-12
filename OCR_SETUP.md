# OCR 服务部署指南

下面是你在 Railway 上新建 OCR 服务的步骤。

## 1. 新建服务

1. 打开 Railway 项目。
2. 点击 `+ New`，选择 `GitHub Repo`。
3. 选择当前仓库 `pengtruman922-dotcom/ai-asr-file`。
4. 创建后，把这个服务命名为 `OCR`。
5. 在服务设置里配置：
   - `Root Directory`：`ocr`
   - `Build`：使用仓库里的 `Dockerfile`
   - `Start Command`：留空

## 2. 配置环境变量

在 OCR 服务里添加下面变量：

```bash
PORT=8000
OCR_SERVICE_TOKEN=你自己生成的一串长随机字符串
OCR_LANG=ch
OCR_MAX_PAGES=80
OCR_RENDER_SCALE=2.0
```

## 3. 让 Web / Worker 调用 OCR

在 Web 和 Worker 两个服务里都增加：

```bash
OCR_SERVICE_URL=http://${{OCR.RAILWAY_PRIVATE_DOMAIN}}:${{OCR.PORT}}
OCR_SERVICE_TOKEN=${{OCR.OCR_SERVICE_TOKEN}}
OCR_SERVICE_TIMEOUT_SECONDS=900
```

说明：

- `OCR` 必须和你在 Railway 里给 OCR 服务起的名字一致。
- OCR 服务只走 Railway 私有网络，不需要公开域名。
- 如果你想先快速验证，也可以给 OCR 服务开公网域名，把 `OCR_SERVICE_URL` 改成公网地址。

## 4. 这个服务做什么

OCR 服务只负责一件事：把扫描版 PDF 渲染成图片，再交给 PaddleOCR 识别，最后把识别文本返回给 Worker。

流程是：

```text
Worker 检测到 PDF 文本层太少
  -> 调用 OCR 服务 /api/ocr/pdf
  -> OCR 服务用 PyMuPDF 渲染页面
  -> PaddleOCR 识别
  -> 返回 OCR 文本
  -> Worker 写入 extracted_text
```

## 5. 你需要知道的点

- OCR 服务比较重，所以我把它拆成独立服务，避免把 Web / Worker 镜像搞得太大。
- 如果以后你愿意，我还可以继续把 OCR 再拆成更细的图片处理服务。
