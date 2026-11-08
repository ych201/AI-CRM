"""知识库文件上传：格式/大小校验、文本抽取、入库与幂等。"""
from __future__ import annotations

import io

import pytest

from app.db import kb as kb_db

TXT = "课程资料：AI 产品经理实战班本学期 9800 元，含 12 周直播与 2 次模拟面试。" * 5


def _upload(client, headers, filename, data, collection="course", **form):
    payload = {"file": (io.BytesIO(data), filename), "collection": collection}
    payload.update(form)
    return client.post(
        "/api/kb/upload", data=payload, headers=headers, content_type="multipart/form-data"
    )


def _docx_bytes(paragraphs: list[str]) -> bytes:
    document = pytest.importorskip("docx").Document()
    for line in paragraphs:
        document.add_paragraph(line)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _pdf_bytes(text: str) -> bytes:
    """手工拼一个只含一行文本的最小 PDF。"""
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] "
        "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
    ]
    stream = f"BT /F1 12 Tf 20 150 Td ({text}) Tj ET"
    objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = "%PDF-1.4\n"
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n{body}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode("latin-1")


def test_upload_requires_api_key(client):
    resp = client.post(
        "/api/kb/upload",
        data={"file": (io.BytesIO(TXT.encode()), "a.txt"), "collection": "course"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 401
    assert resp.get_json()["error"]["code"] == "UNAUTHORIZED"


def test_upload_markdown(client, headers):
    resp = _upload(client, headers, "课程大纲.md", f"# 大纲\n\n{TXT}".encode())
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["action"] == "created"
    assert body["chunks"] >= 1
    assert body["filename"] == "课程大纲.md"
    assert body["source"] == "file:md"
    assert body["doc_id"].startswith("course_")
    assert body["title"] == "课程大纲"

    detail = client.get(f"/api/kb/documents/{body['doc_id']}", headers=headers).get_json()
    assert "9800" in detail["content"]
    assert "# 大纲" in detail["content"]


def test_upload_txt_gbk(client, headers):
    resp = _upload(client, headers, "notes.txt", TXT.encode("gb18030"))
    assert resp.status_code == 200, resp.get_json()
    detail = client.get(f"/api/kb/documents/{resp.get_json()['doc_id']}", headers=headers)
    assert "9800" in detail.get_json()["content"]


def test_upload_docx(client, headers):
    data = _docx_bytes(["课程价格", TXT])
    resp = _upload(client, headers, "课程价格.docx", data)
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["source"] == "file:docx"
    detail = client.get(f"/api/kb/documents/{resp.get_json()['doc_id']}", headers=headers)
    assert "课程价格" in detail.get_json()["content"]


def test_upload_pdf(client, headers):
    resp = _upload(client, headers, "handbook.pdf", _pdf_bytes("Hello PDF Knowledge 9800"))
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["source"] == "file:pdf"
    detail = client.get(f"/api/kb/documents/{resp.get_json()['doc_id']}", headers=headers)
    assert "Hello PDF Knowledge 9800" in detail.get_json()["content"]


def test_upload_long_file_splits_into_documents(client, headers):
    paragraph = "这一段用于验证长文件自动拆分逻辑。" * 60
    content = "\n\n".join([paragraph] * 30)
    assert len(content) > 20000

    resp = _upload(client, headers, "long.md", content.encode())
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["documents"] > 1
    assert len(body["doc_ids"]) == body["documents"]
    assert kb_db.count() == body["documents"]
    for doc_id in body["doc_ids"]:
        assert len(kb_db.get(doc_id)["content"]) <= 20000


def test_upload_same_file_is_idempotent(client, headers):
    first = _upload(client, headers, "pricing.md", TXT.encode())
    second = _upload(client, headers, "pricing.md", TXT.encode())
    assert first.status_code == 200
    assert second.get_json()["action"] == "updated"
    assert second.get_json()["doc_id"] == first.get_json()["doc_id"]
    assert kb_db.count() == 1


def test_upload_rejects_bad_input(client, headers):
    empty = _upload(client, headers, "empty.txt", b"")
    assert empty.status_code == 422
    assert empty.get_json()["error"]["message"] == "文件内容为空"

    legacy = _upload(client, headers, "old.doc", TXT.encode())
    assert legacy.status_code == 422
    assert ".docx" in legacy.get_json()["error"]["message"]

    exe = _upload(client, headers, "virus.exe", b"MZ...")
    assert exe.status_code == 422

    broken_pdf = _upload(client, headers, "broken.pdf", b"%PDF-1.4 not a real pdf")
    assert broken_pdf.status_code == 422

    bad_collection = _upload(client, headers, "a.md", TXT.encode(), collection="nope")
    assert bad_collection.status_code == 422

    missing = client.post(
        "/api/kb/upload",
        data={"collection": "course"},
        headers=headers,
        content_type="multipart/form-data",
    )
    assert missing.status_code == 400
