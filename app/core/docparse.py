"""知识库文件导入：从 PDF / Word / TXT / Markdown 中抽取纯文本。

PDF 依赖 ``pypdf``，Word（.docx）依赖 ``python-docx``；两者都按需导入，
未安装时返回 422 提示而不是 500。旧版 .doc 不支持，请另存为 .docx。
"""
from __future__ import annotations

import codecs
import io
import logging
import re
from pathlib import Path

from app.core.errors import Unprocessable

logger = logging.getLogger("app.docparse")

MAX_FILE_BYTES = 10 * 1024 * 1024
SUPPORTED_EXTS = (".pdf", ".docx", ".txt", ".md", ".markdown")

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE_RE = re.compile(r"[ \t\u00a0\u3000]+")
_BLANK_RE = re.compile(r"\n{3,}")


def ext_of(filename: str) -> str:
    return Path(filename or "").suffix.lower()


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CTRL_RE.sub(" ", text)
    lines = [_SPACE_RE.sub(" ", line).strip() for line in text.split("\n")]
    return _BLANK_RE.sub("\n\n", "\n".join(lines)).strip()


def _decode_plain(data: bytes) -> str:
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="replace")
    if data.startswith(codecs.BOM_UTF8):
        data = data[len(codecs.BOM_UTF8) :]
    for encoding in ("utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的兜底
        raise Unprocessable("服务端缺少 PDF 解析依赖，请安装 pypdf 后重试") from exc

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:  # noqa: BLE001
                raise Unprocessable("PDF 已加密，无法解析") from exc
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Unprocessable:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("pdf parse failed", extra={"error": str(exc)})
        raise Unprocessable("PDF 解析失败，请确认文件未损坏") from exc

    return "\n\n".join(pages)


def _docx_text(data: bytes) -> str:
    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的兜底
        raise Unprocessable("服务端缺少 Word 解析依赖，请安装 python-docx 后重试") from exc

    try:
        document = Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        logger.warning("docx parse failed", extra={"error": str(exc)})
        raise Unprocessable("Word 解析失败，请确认文件为 .docx 格式且未损坏") from exc

    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def extract_text(filename: str, data: bytes) -> str:
    """按扩展名抽取文本，失败抛 422。"""
    name = (filename or "").strip()
    if not name:
        raise Unprocessable("文件名不能为空", field="file")
    if not data:
        raise Unprocessable("文件内容为空", field="file")
    if len(data) > MAX_FILE_BYTES:
        limit_mb = MAX_FILE_BYTES // (1024 * 1024)
        raise Unprocessable(f"文件不能超过 {limit_mb}MB", field="file")

    ext = ext_of(name)
    if ext == ".doc":
        raise Unprocessable("暂不支持旧版 .doc，请另存为 .docx 后上传", field="file")
    if ext not in SUPPORTED_EXTS:
        raise Unprocessable(
            "仅支持 PDF / Word(.docx) / TXT / Markdown 文件", field="file"
        )

    if ext == ".pdf":
        raw = _pdf_text(data)
    elif ext == ".docx":
        raw = _docx_text(data)
    else:
        raw = _decode_plain(data)

    text = _normalize(raw)
    if not text:
        hint = "（可能是扫描件，请先转成可复制文本的 PDF）" if ext == ".pdf" else ""
        raise Unprocessable(f"未从文件中解析出文本{hint}", field="file")
    return text


def source_label(filename: str) -> str:
    """列表「来源」列展示用，例如 file:pdf。"""
    return f"file:{ext_of(filename).lstrip('.') or 'unknown'}"
