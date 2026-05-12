import json
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urlparse

import requests

from .config import get_settings
from .settings_service import get_ai_config


class ASRClient:
    def __init__(self):
        self.settings = get_settings()

    def transcribe(self, audio_url: str, file_name: str, speaker_count: int | None = None, on_task_id: Callable[[str], None] | None = None, on_event: Callable[[str, dict], None] | None = None) -> list[dict]:
        config = get_ai_config("asr")
        api_key = config.get("api_key", "")
        if self._use_local_mock(self.settings.asr_mock_enabled, api_key):
            self._emit_asr_event(on_event, "mock_used", {"file_name": file_name})
            return self._mock_segments(file_name)
        if not api_key:
            raise RuntimeError("ASR_API_KEY_MISSING: 请先在系统设置中配置 ASR API Key，或在 Railway 变量中配置 ASR_API_KEY。")

        submit_url = (config.get("url") or self.settings.asr_api_url).strip().rstrip("/")
        model = (config.get("model") or self.settings.asr_model).strip()
        self._emit_asr_event(on_event, "submit_start", {"model": model, "url": submit_url})
        task_id = self._submit_task(submit_url, api_key, model, audio_url, speaker_count=speaker_count, on_event=on_event)
        if on_task_id:
            on_task_id(task_id)
        self._emit_asr_event(on_event, "task_id_received", {"task_id": task_id})
        result_url = self._wait_for_result(submit_url, api_key, task_id, on_event=on_event)
        self._emit_asr_event(on_event, "result_download_start", {"task_id": task_id})
        result = self._download_transcription_result(result_url)
        self._emit_asr_event(on_event, "result_download_complete", {"task_id": task_id})
        segments = self._parse_transcription_result(result)
        self._emit_asr_event(on_event, "parse_complete", {"task_id": task_id, "segment_count": len(segments)})
        if not segments:
            raise RuntimeError("ASR_RESULT_EMPTY: 识别结果为空，请检查音频是否包含可识别语音。")
        return segments

    def _submit_task(self, submit_url: str, api_key: str, model: str, audio_url: str, speaker_count: int | None = None, on_event: Callable[[str, dict], None] | None = None) -> str:
        speaker_count_value = self.settings.asr_speaker_count if speaker_count is None else int(speaker_count or 0)
        payload = {
            "model": model,
            "input": {"file_urls": [audio_url]},
            "parameters": {
                "channel_id": [0],
                "disfluency_removal_enabled": False,
                "timestamp_alignment_enabled": True,
            },
        }
        if self.settings.asr_diarization_enabled:
            payload["parameters"]["diarization_enabled"] = True
            if speaker_count_value > 0:
                payload["parameters"]["speaker_count"] = speaker_count_value
            self._emit_asr_event(
                on_event,
                "diarization_config",
                {"speaker_count": speaker_count_value if speaker_count_value > 0 else None, "mode": "fixed" if speaker_count_value > 0 else "auto"},
            )
        if model == "paraformer-v2":
            payload["parameters"]["language_hints"] = ["zh", "en"]

        start = time.monotonic()
        response = requests.post(
            submit_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-DashScope-Async": "enable",
            },
            json=payload,
            timeout=60,
        )
        self._raise_for_api_error(response, "ASR_SUBMIT_FAILED")
        data = response.json()
        task_id = data.get("output", {}).get("task_id") or data.get("task_id")
        if not task_id:
            raise RuntimeError(f"ASR_SUBMIT_FAILED: 未返回 task_id，响应={self._compact_json(data)}")
        self._emit_asr_event(on_event, "submit_complete", {"task_id": str(task_id), "elapsed_ms": int((time.monotonic() - start) * 1000)})
        return str(task_id)

    def _wait_for_result(self, submit_url: str, api_key: str, task_id: str, on_event: Callable[[str, dict], None] | None = None) -> str:
        task_url = self._task_url(submit_url, task_id)
        deadline = time.monotonic() + self.settings.asr_poll_timeout_seconds
        last_payload: dict[str, Any] | None = None
        poll_count = 0
        self._emit_asr_event(on_event, "poll_start", {"task_id": task_id, "interval_seconds": self.settings.asr_poll_interval_seconds})
        while time.monotonic() < deadline:
            poll_count += 1
            poll_started = time.monotonic()
            response = requests.post(
                task_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "X-DashScope-Async": "enable",
                },
                timeout=60,
            )
            self._raise_for_api_error(response, "ASR_POLL_FAILED")
            payload = response.json()
            last_payload = payload
            output = payload.get("output") if isinstance(payload.get("output"), dict) else payload
            status = str(output.get("task_status") or output.get("status") or "").upper()
            self._emit_asr_event(
                on_event,
                "poll_status",
                {"task_id": task_id, "poll_count": poll_count, "status": status or "UNKNOWN", "elapsed_ms": int((time.monotonic() - poll_started) * 1000)},
            )

            if status == "SUCCEEDED":
                result_url = self._extract_transcription_url(output)
                self._emit_asr_event(on_event, "result_url_received", {"task_id": task_id, "poll_count": poll_count})
                return result_url
            if status in {"FAILED", "CANCELED", "CANCELLED"}:
                message = output.get("message") or payload.get("message") or self._compact_json(output)
                raise RuntimeError(f"ASR_TASK_FAILED: task_id={task_id}, status={status}, message={message}")
            if status not in {"PENDING", "RUNNING", ""}:
                raise RuntimeError(f"ASR_TASK_UNKNOWN_STATUS: task_id={task_id}, status={status}, response={self._compact_json(output)}")

            time.sleep(self.settings.asr_poll_interval_seconds)

        raise RuntimeError(f"ASR_TASK_TIMEOUT: task_id={task_id}, last_response={self._compact_json(last_payload or {})}")

    def _emit_asr_event(self, callback: Callable[[str, dict], None] | None, event: str, payload: dict | None = None) -> None:
        if not callback:
            return
        try:
            callback(event, payload or {})
        except Exception:
            pass

    def _extract_transcription_url(self, output: dict) -> str:
        results = output.get("results") or []
        if not isinstance(results, list) or not results:
            raise RuntimeError(f"ASR_RESULT_URL_MISSING: 查询成功但未返回 results，响应={self._compact_json(output)}")

        failed_items = []
        for item in results:
            if not isinstance(item, dict):
                continue
            sub_status = str(item.get("subtask_status") or item.get("status") or "SUCCEEDED").upper()
            if sub_status == "SUCCEEDED" and item.get("transcription_url"):
                return str(item["transcription_url"])
            failed_items.append({"status": sub_status, "code": item.get("code"), "message": item.get("message")})
        raise RuntimeError(f"ASR_SUBTASK_FAILED: 未找到成功的转写子任务，details={self._compact_json(failed_items)}")

    def _download_transcription_result(self, transcription_url: str) -> Any:
        response = requests.get(transcription_url, timeout=120)
        response.raise_for_status()
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError("ASR_RESULT_INVALID_JSON: 识别结果不是有效 JSON。") from exc

    def _parse_transcription_result(self, data: Any) -> list[dict]:
        if isinstance(data, list):
            data = {"segments": data}
        if not isinstance(data, dict):
            return []

        segments: list[dict] = []
        transcripts = data.get("transcripts")
        if isinstance(transcripts, list):
            for transcript in transcripts:
                if not isinstance(transcript, dict):
                    continue
                sentences = transcript.get("sentences")
                channel_id = transcript.get("channel_id")
                if isinstance(sentences, list) and sentences:
                    for sentence in sentences:
                        segment = self._segment_from_item(sentence, channel_id=channel_id)
                        if segment:
                            segments.append(segment)
                else:
                    text = str(transcript.get("text") or transcript.get("transcript") or "").strip()
                    if text:
                        duration = self._to_ms(
                            transcript.get("content_duration_in_milliseconds")
                            or transcript.get("content_duration")
                            or data.get("properties", {}).get("original_duration_in_milliseconds")
                            or 0
                        )
                        segments.append(
                            {
                                "speaker": self._speaker_label(transcript, channel_id=channel_id),
                                "start_time_ms": 0,
                                "end_time_ms": duration,
                                "text": text,
                                "confidence": self._confidence(transcript),
                            }
                        )

        for key in ("sentences", "segments", "paragraphs"):
            items = data.get(key)
            if isinstance(items, list):
                for item in items:
                    segment = self._segment_from_item(item)
                    if segment:
                        segments.append(segment)

        if not segments:
            text = str(data.get("text") or data.get("transcript") or data.get("content") or "").strip()
            if text:
                duration = self._to_ms(data.get("duration") or data.get("duration_ms") or data.get("original_duration_in_milliseconds") or 0)
                segments.append({"speaker": "说话人", "start_time_ms": 0, "end_time_ms": duration, "text": text, "confidence": None})

        segments = [item for item in segments if item["text"]]
        segments.sort(key=lambda item: (item["start_time_ms"], item["end_time_ms"]))
        return segments

    def _segment_from_item(self, item: Any, channel_id: Any = None) -> dict | None:
        if not isinstance(item, dict):
            return None
        text = str(item.get("text") or item.get("sentence") or item.get("transcript") or item.get("content") or "").strip()
        if not text:
            return None
        start = self._to_ms(item.get("begin_time", item.get("start_time", item.get("start", item.get("offset", 0)))))
        end = self._to_ms(item.get("end_time", item.get("end", item.get("stop", start))))
        if end < start:
            end = start
        return {
            "speaker": self._speaker_label(item, channel_id=channel_id),
            "start_time_ms": start,
            "end_time_ms": end,
            "text": text,
            "confidence": self._confidence(item),
        }

    def _speaker_label(self, item: dict, channel_id: Any = None) -> str:
        for key in ("speaker", "speaker_name"):
            if item.get(key):
                return str(item[key])
        if item.get("speaker_id") is not None:
            return f"发言人{int(item['speaker_id']) + 1}" if str(item["speaker_id"]).isdigit() else f"发言人{item['speaker_id']}"
        if channel_id is not None:
            return "发言人1"
        if item.get("channel_id") is not None:
            return "发言人1"
        return "发言人"

    def _confidence(self, item: dict) -> float | None:
        value = item.get("confidence", item.get("score"))
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _to_ms(self, value: Any) -> int:
        if value is None:
            return 0
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0
        if 0 < number < 1000 and not float(number).is_integer():
            return int(number * 1000)
        return int(number)

    def _task_url(self, submit_url: str, task_id: str) -> str:
        parsed = urlparse(submit_url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}/api/v1/tasks/{task_id}"
        return f"https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"

    def _raise_for_api_error(self, response: requests.Response, prefix: str) -> None:
        if response.status_code < 400:
            return
        message = self._safe_response_text(response)
        raise RuntimeError(f"{prefix}: HTTP {response.status_code}, {message}")

    def _safe_response_text(self, response: requests.Response) -> str:
        try:
            data = response.json()
            message = data.get("message") or data.get("code") or data.get("error", {}).get("message") or data
        except ValueError:
            message = response.text
        return str(message).replace("\n", " ")[:800]

    def _compact_json(self, payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:1200]

    def _use_local_mock(self, mock_enabled: bool, api_key: str) -> bool:
        return self.settings.app_env == "local" and mock_enabled and not api_key

    def _mock_segments(self, file_name: str) -> list[dict]:
        topic = file_name.rsplit(".", 1)[0]
        return [
            {
                "speaker": "顾问",
                "start_time_ms": 0,
                "end_time_ms": 12000,
                "text": f"我们今天主要围绕{topic}做一次访谈，重点了解当前业务痛点和后续需求。",
                "confidence": 0.98,
            },
            {
                "speaker": "客户",
                "start_time_ms": 13000,
                "end_time_ms": 42000,
                "text": "我们现在最大的问题不是系统数量，而是各部门对同一个指标的理解不一致，开会经常先争数据口径。",
                "confidence": 0.96,
            },
            {
                "speaker": "顾问",
                "start_time_ms": 43000,
                "end_time_ms": 58000,
                "text": "如果后续建设统一的数据平台，您最担心的风险是什么？",
                "confidence": 0.97,
            },
            {
                "speaker": "客户",
                "start_time_ms": 59000,
                "end_time_ms": 90000,
                "text": "我担心业务部门不愿意改流程，最后系统上线了，但是大家还是按原来的方式各看各的。",
                "confidence": 0.95,
            },
            {
                "speaker": "客户",
                "start_time_ms": 91000,
                "end_time_ms": 120000,
                "text": "如果能先把关键指标和责任机制统一起来，再逐步推进系统建设，我们会更有信心。",
                "confidence": 0.95,
            },
        ]


class LLMClient:
    def __init__(self):
        self.settings = get_settings()

    def clean_segments(self, raw_segments: list[dict], on_event: Callable[[str, dict], None] | None = None) -> list[dict]:
        config = get_ai_config("clean")
        api_key = config.get("api_key", "")
        if self._use_local_mock(self.settings.llm_mock_enabled, api_key):
            self._emit_clean_event(on_event, "mock_used", {"segment_count": len(raw_segments)})
            return [
                {
                    "raw_segment_id": item["id"],
                    "speaker": item["speaker"],
                    "start_time_ms": item["start_time_ms"],
                    "end_time_ms": item["end_time_ms"],
                    "clean_text": item["text"].strip(),
                }
                for item in raw_segments
            ]
        if not api_key:
            raise RuntimeError("LLM_API_KEY_MISSING: 请先在系统设置中配置清洁稿模型 API Key。")

        batches = self._split_clean_batches(raw_segments)
        batch_count = len(batches)
        if batch_count == 0:
            return []
        max_workers = max(1, min(self.settings.llm_clean_batch_concurrency, batch_count))
        self._emit_clean_event(
            on_event,
            "batch_plan",
            {
                "segment_count": len(raw_segments),
                "batch_count": batch_count,
                "max_workers": max_workers,
                "max_segments": self.settings.llm_clean_batch_max_segments,
                "max_chars": self.settings.llm_clean_batch_max_chars,
            },
        )

        results: list[list[dict] | None] = [None] * batch_count
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {}
            for index, batch in enumerate(batches, start=1):
                self._emit_clean_event(on_event, "batch_submitted", self._clean_batch_payload(index, batch_count, batch))
                future = executor.submit(self._clean_segment_batch, config, api_key, batch, index, batch_count)
                future_map[future] = (index, batch)
            for future in as_completed(future_map):
                index, batch = future_map[future]
                try:
                    results[index - 1] = future.result()
                except Exception as exc:
                    self._emit_clean_event(on_event, "batch_failed", {**self._clean_batch_payload(index, batch_count, batch), "error": str(exc)})
                    raise RuntimeError(f"LLM_CLEAN_BATCH_FAILED: 第 {index}/{batch_count} 批清洁稿生成失败：{exc}") from exc
                self._emit_clean_event(on_event, "batch_completed", self._clean_batch_payload(index, batch_count, batch))

        cleaned = [item for batch_result in results for item in (batch_result or [])]
        self._emit_clean_event(on_event, "all_batches_completed", {"batch_count": batch_count, "segment_count": len(cleaned)})
        return cleaned

    def _split_clean_batches(self, raw_segments: list[dict]) -> list[list[dict]]:
        batches: list[list[dict]] = []
        current: list[dict] = []
        current_chars = 0
        max_segments = max(1, self.settings.llm_clean_batch_max_segments)
        max_chars = max(1000, self.settings.llm_clean_batch_max_chars)
        for segment in raw_segments:
            segment_chars = len(str(segment.get("text") or ""))
            should_flush = current and (len(current) >= max_segments or current_chars + segment_chars > max_chars)
            if should_flush:
                batches.append(current)
                current = []
                current_chars = 0
            current.append(segment)
            current_chars += segment_chars
        if current:
            batches.append(current)
        return batches

    def _clean_segment_batch(self, config: dict, api_key: str, raw_segments: list[dict], batch_index: int, batch_count: int) -> list[dict]:
        content = self._chat_json(
            config.get("url", self.settings.llm_clean_base_url),
            api_key,
            config.get("model", self.settings.llm_clean_model),
            "你是咨询访谈转写清洁助手。请修正明显错别字、标点和断句，保留原意、说话人、时间戳，不要扩写、合并或删除段落。只返回 JSON。",
            json.dumps(
                {
                    "task": "clean_transcript_segments",
                    "batch": {"index": batch_index, "total": batch_count},
                    "rules": [
                        "返回 segments 数组，数量和输入 segments 保持一致",
                        "每个输出段落必须保留对应 raw_segment_id、speaker、start_time_ms、end_time_ms",
                        "clean_text 只做可读性清洁，不要总结、扩写或改变原意",
                    ],
                    "output_schema": {
                        "segments": [
                            {
                                "raw_segment_id": "原始段落 id",
                                "speaker": "说话人",
                                "start_time_ms": 0,
                                "end_time_ms": 0,
                                "clean_text": "清洁后的文本",
                            }
                        ]
                    },
                    "segments": raw_segments,
                },
                ensure_ascii=False,
            ),
        )
        data = self._loads_json(content)
        segments = data.get("segments") if isinstance(data, dict) else data
        if not isinstance(segments, list):
            raise RuntimeError("LLM_CLEAN_INVALID_JSON: 清洁稿模型未返回 segments 数组。")
        return self._normalize_clean_segments(raw_segments, segments)

    def _normalize_clean_segments(self, raw_segments: list[dict], segments: list) -> list[dict]:
        cleaned = []
        raw_by_id = {item["id"]: item for item in raw_segments}
        parsed_by_id: dict[str, dict] = {}
        parsed_by_index: dict[int, dict] = {}
        for index, item in enumerate(segments):
            if not isinstance(item, dict):
                continue
            raw_id = item.get("raw_segment_id") or item.get("id") or (raw_segments[index]["id"] if index < len(raw_segments) else None)
            if raw_id:
                parsed_by_id[str(raw_id)] = item
            parsed_by_index[index] = item

        for index, raw in enumerate(raw_segments):
            item = parsed_by_id.get(str(raw["id"])) or parsed_by_index.get(index) or {}
            raw_id = item.get("raw_segment_id") or item.get("id") or raw["id"]
            if raw_id not in raw_by_id:
                raw_id = raw["id"]
            cleaned.append(
                {
                    "raw_segment_id": raw_id,
                    "speaker": item.get("speaker") or raw.get("speaker") or "说话人",
                    "start_time_ms": int(item.get("start_time_ms") if item.get("start_time_ms") is not None else raw.get("start_time_ms", 0)),
                    "end_time_ms": int(item.get("end_time_ms") if item.get("end_time_ms") is not None else raw.get("end_time_ms", 0)),
                    "clean_text": str(item.get("clean_text") or item.get("text") or raw.get("text") or "").strip(),
                }
            )
        return cleaned

    def _clean_batch_payload(self, batch_index: int, batch_count: int, batch: list[dict]) -> dict:
        return {
            "batch_index": batch_index,
            "batch_count": batch_count,
            "segment_count": len(batch),
            "char_count": sum(len(str(item.get("text") or "")) for item in batch),
        }

    def _emit_clean_event(self, on_event: Callable[[str, dict], None] | None, event: str, payload: dict | None = None) -> None:
        if on_event:
            on_event(event, payload or {})

    def summarize(self, clean_segments: list[dict], template_type: str) -> dict:
        config = get_ai_config("summary")
        api_key = config.get("api_key", "")
        if self._use_local_mock(self.settings.llm_mock_enabled, api_key):
            quotes = [
                {
                    "quote": item["text"],
                    "speaker": item["speaker"],
                    "segment_id": item["id"],
                    "start_time_ms": item["start_time_ms"],
                }
                for item in clean_segments
                if item["speaker"] in {"客户", "专家", "内部受访人", "说话人", "说话人1", "说话人2"}
            ][:3]
            quote_lines = "\n".join(f"- [{self._fmt_time(q['start_time_ms'])}] {q['speaker']}：{q['quote']}" for q in quotes)
            markdown = f"""## 访谈摘要
本次访谈围绕客户当前业务痛点、数据治理诉求和系统建设风险展开。受访者重点提到跨部门数据口径不一致、流程变革阻力，以及系统建设前需要先明确管理责任。

## 关键结论
1. **数据口径不一致**：不同部门对关键指标理解不一致，影响沟通和决策。
2. **流程和组织配合是主要风险**：受访者担心系统上线后业务部门仍沿用原流程。
3. **先治理、后系统**：受访者倾向于先统一关键指标和责任机制，再逐步推进系统建设。

## 客户痛点
- 部门间数据口径不一致。
- 业务流程变革阻力较大。
- 系统建设可能与管理机制脱节。

## 需求分析
- 统一关键指标口径。
- 明确数据治理责任机制。
- 分阶段推进系统建设。

## 风险与顾虑
- 业务部门不愿意改变现有工作方式。
- 系统上线后仍然各看各的数据。

## 后续建议
- 下一轮访谈补充确认核心指标范围。
- 进一步了解业务部门流程调整意愿。

## 报告引用建议
{quote_lines}
"""
            return {"format": "markdown", "markdown": markdown.strip(), "report_quotes": quotes}
        if not api_key:
            raise RuntimeError("LLM_API_KEY_MISSING: 请先在系统设置中配置纪要模型 API Key。")

        prompt = json.dumps({"template_type": template_type, "segments": clean_segments}, ensure_ascii=False)
        content = self._chat_plain(
            config.get("url", self.settings.llm_summary_base_url),
            api_key,
            config.get("model", self.settings.llm_summary_model),
            "你是咨询公司访谈纪要助手。请基于清洁稿生成可直接阅读和引用的 Markdown 纪要，包含访谈摘要、关键结论、客户痛点、需求分析、风险与顾虑、后续建议、报告引用建议。报告引用建议必须带时间戳。不要输出 JSON。",
            prompt,
        )
        return {"format": "markdown", "markdown": content.strip()}

    def answer(self, question: str, materials: list[dict], history: list[dict] | None = None) -> dict:
        config = get_ai_config("qa")
        api_key = config.get("api_key", "")
        if self._use_local_mock(self.settings.llm_mock_enabled, api_key):
            return {
                "answer": "这是本地模拟回答：请优先参考已选文件内容回答用户问题，并在引用材料时注明文件名和时间点。",
                "answer_markdown": "这是本地模拟回答：请优先参考已选文件内容回答用户问题，并在引用材料时注明文件名和时间点。",
                "sources": [],
            }
        if not api_key:
            raise RuntimeError("LLM_API_KEY_MISSING: 请先在系统设置中配置问答模型 API Key。")
        system, prompt = self._qa_prompt(question, materials, history)
        content = self._chat_plain(
            config.get("url", self.settings.llm_qa_base_url),
            api_key,
            config.get("model", self.settings.llm_qa_model),
            system,
            prompt,
        )
        return self._sanitize_qa_output({"answer": content.strip(), "answer_markdown": content.strip(), "sources": []})

    def answer_stream(self, question: str, materials: list[dict], history: list[dict] | None = None) -> Any:
        config = get_ai_config("qa")
        api_key = config.get("api_key", "")
        if self._use_local_mock(self.settings.llm_mock_enabled, api_key):
            mock_text = "这是本地模拟回答：我会优先参考已选文件内容和最近对话上下文；引用材料时会注明文件名和时间点。"
            for piece in self._chunk_text(mock_text, 12):
                yield {"type": "content", "delta": piece}
            return
        if not api_key:
            raise RuntimeError("LLM_API_KEY_MISSING: 请先在系统设置中配置问答模型 API Key。")
        system, prompt = self._qa_prompt(question, materials, history)
        yield from self._chat_stream(
            config.get("url", self.settings.llm_qa_base_url),
            api_key,
            config.get("model", self.settings.llm_qa_model),
            system,
            prompt,
        )

    def _qa_prompt(self, question: str, materials: list[dict], history: list[dict] | None = None) -> tuple[str, str]:
        prompt_materials = []
        for item in materials:
            if item.get("segments"):
                prompt_materials.append(
                    {
                        "file_name": item.get("file_name", ""),
                        "file_type": item.get("file_type", "audio"),
                        "segments": item.get("segments", []),
                    }
                )
            else:
                prompt_materials.append(
                    {
                        "file_name": item.get("file_name", ""),
                        "file_type": item.get("file_type", "document"),
                        "extracted_text": item.get("text", ""),
                    }
                )
        system = "请回答用户问题。优先参考给定文件内容和最近对话上下文；没有相关材料时也可以基于通用知识回答，但要说明材料中未找到依据；不要编造文件中没有的事实；引用文件内容时尽量注明来源文件名、页码/Sheet/时间点；不要输出内部ID。"
        prompt = json.dumps(
            {
                "question": question,
                "materials": prompt_materials,
                "recent_history": history or [],
            },
            ensure_ascii=False,
        )
        return system, prompt

    def _chunk_text(self, text: str, size: int) -> list[str]:
        return [text[index : index + size] for index in range(0, len(text), size)]

    def _sanitize_qa_output(self, data: dict) -> dict:
        for key in ("answer", "answer_markdown"):
            if isinstance(data.get(key), str):
                data[key] = self._strip_internal_ids(data[key])
        data["sources"] = self._sanitize_sources(data.get("sources") or [])
        for point in data.get("key_points") or []:
            if isinstance(point, dict):
                if isinstance(point.get("detail"), str):
                    point["detail"] = self._strip_internal_ids(point["detail"])
                point["sources"] = self._sanitize_sources(point.get("sources") or [])
        return data

    def _sanitize_sources(self, sources: list) -> list:
        cleaned = []
        for source in sources:
            if not isinstance(source, dict):
                continue
            cleaned.append(
                {
                    "recording_id": source.get("recording_id", ""),
                    "file_name": source.get("file_name", ""),
                    "start_time_ms": source.get("start_time_ms", 0),
                    "end_time_ms": source.get("end_time_ms", 0),
                    "quote": self._strip_internal_ids(str(source.get("quote", ""))),
                }
            )
        return cleaned

    def _strip_internal_ids(self, text: str) -> str:
        return re.sub(r"[\s\(（\[]*seg_[0-9A-Za-z_-]+[\)）\]]*", "", text)

    def _chat_json(self, base_url: str, api_key: str, model: str, system: str, user: str) -> str:
        try:
            return self._chat(base_url, api_key, model, system, user, json_mode=True)
        except requests.HTTPError as exc:
            response = exc.response
            body = response.text.lower() if response is not None else ""
            if response is not None and response.status_code in {400, 422} and "response_format" in body:
                return self._chat(base_url, api_key, model, system + "\n请仅输出合法 JSON，不要包含 Markdown 代码块。", user, json_mode=False)
            raise

    def _chat_plain(self, base_url: str, api_key: str, model: str, system: str, user: str) -> str:
        return self._chat(base_url, api_key, model, system, user, json_mode=False)

    def _chat(self, base_url: str, api_key: str, model: str, system: str, user: str, json_mode: bool) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.2,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        response = requests.post(
            self._chat_endpoint(base_url),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=self.settings.llm_timeout_seconds,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            message = self._safe_response_text(response)
            raise requests.HTTPError(f"LLM_CALL_FAILED: HTTP {response.status_code}, {message}", response=response) from exc
        data = response.json()
        return data["choices"][0]["message"]["content"]

    def _chat_stream(self, base_url: str, api_key: str, model: str, system: str, user: str) -> Any:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.2,
            "stream": True,
        }
        with requests.post(
            self._chat_endpoint(base_url),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=self.settings.llm_timeout_seconds,
            stream=True,
        ) as response:
            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                message = self._safe_response_text(response)
                raise requests.HTTPError(f"LLM_CALL_FAILED: HTTP {response.status_code}, {message}", response=response) from exc
            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="ignore")
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                raw = line.removeprefix("data:").strip()
                if raw == "[DONE]":
                    break
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                choices = data.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or choices[0].get("message") or {}
                reasoning = self._delta_to_text(delta.get("reasoning_content") or delta.get("reasoning"))
                content = self._delta_to_text(delta.get("content"))
                if reasoning:
                    yield {"type": "reasoning", "delta": reasoning}
                if content:
                    yield {"type": "content", "delta": content}

    def _delta_to_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or ""))
            return "".join(parts)
        return str(value)

    def _chat_endpoint(self, base_url: str) -> str:
        endpoint = (base_url or self.settings.llm_clean_base_url).strip().rstrip("/")
        if endpoint.endswith("/chat/completions"):
            return endpoint
        return endpoint + "/chat/completions"

    def _use_local_mock(self, mock_enabled: bool, api_key: str) -> bool:
        return self.settings.app_env == "local" and mock_enabled and not api_key

    def _loads_json(self, content: str) -> Any:
        content = content.strip()
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", content, flags=re.S)
            if match:
                return json.loads(match.group(1))
            match = re.search(r"(\{.*\}|\[.*\])", content, flags=re.S)
            if match:
                return json.loads(match.group(1))
            raise

    def _safe_response_text(self, response: requests.Response) -> str:
        try:
            data = response.json()
            message = data.get("message") or data.get("error", {}).get("message") or data.get("code") or str(data)
        except ValueError:
            message = response.text
        return str(message).replace("\n", " ")[:800]

    def _fmt_time(self, ms: int) -> str:
        seconds = ms // 1000
        return f"{seconds // 60:02d}:{seconds % 60:02d}"


asr_client = ASRClient()
llm_client = LLMClient()
