import io
import os

import pytest
from PIL import Image

from app import create_app


def _png(width=320, height=240, color=(80, 140, 220)):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, "PNG")
    return buffer.getvalue()


def _client(tmp_path):
    app = create_app({
        "TESTING": True,
        "API_ONLY": True,
        "DATABASE": str(tmp_path / "media_tools.db"),
        "MEDIA_TOOLS_DIR": str(tmp_path / "media_tools"),
    })
    return app, app.test_client()


def _upload(client, name="a.png", payload=None):
    response = client.post(
        "/api/v1/media-tools/uploads",
        data={"file": (io.BytesIO(payload or _png()), name)},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["data"]["fileId"]


def _run_job(client, tool_type, **payload):
    body = {"toolType": tool_type}
    body.update(payload)
    response = client.post("/api/v1/media-tools/jobs", json=body)
    assert response.status_code == 200, response.get_data(as_text=True)
    job_id = response.get_json()["data"]["id"]
    status = client.get(f"/api/v1/media-tools/jobs/{job_id}")
    assert status.status_code == 200
    return job_id, status.get_json()["data"]


def test_compress_job_returns_result_file(tmp_path):
    _, client = _client(tmp_path)
    file_id = _upload(client)
    job_id, job = _run_job(
        client, "image_compress", sourceFiles=[file_id], options={"quality": "standard"},
    )
    assert job["status"] == "done"
    assert job["results"] and job["results"][0]["url"].endswith(".jpg")
    served = client.get(job["results"][0]["url"])
    assert served.status_code == 200
    assert len(served.data) > 0


def test_nine_grid_is_not_a_server_tool(tmp_path):
    _, client = _client(tmp_path)
    file_id = _upload(client)
    response = client.post(
        "/api/v1/media-tools/jobs",
        json={"toolType": "image_nine_grid", "sourceFiles": [file_id]},
    )
    assert response.status_code == 400
    assert response.get_json()["error_code"] == "UNSUPPORTED_TOOL"


def test_qr_job_needs_no_image(tmp_path):
    _, client = _client(tmp_path)
    _, job = _run_job(
        client, "image_qr_code",
        options={"text": "https://example.com", "size": 384},
    )
    assert job["status"] == "done"
    assert job["results"][0]["url"].endswith(".png")


def test_pdf_and_gif_jobs(tmp_path):
    _, client = _client(tmp_path)
    first = _upload(client, "a.png", _png(color=(220, 80, 80)))
    second = _upload(client, "b.png", _png(color=(80, 220, 80)))

    _, pdf_job = _run_job(client, "image_to_pdf", sourceFiles=[first, second])
    assert pdf_job["status"] == "done"
    assert pdf_job["results"][0]["url"].endswith(".pdf")
    assert pdf_job["results"][0]["pages"] == 2

    _, gif_job = _run_job(
        client, "image_to_gif", sourceFiles=[first, second], options={"duration": 120},
    )
    assert gif_job["status"] == "done"
    assert gif_job["results"][0]["url"].endswith(".gif")
    assert gif_job["results"][0]["frames"] == 2


def test_rejects_unsupported_upload(tmp_path):
    _, client = _client(tmp_path)
    response = client.post(
        "/api/v1/media-tools/uploads",
        data={"file": (io.BytesIO(b"not an image"), "x.txt")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert response.get_json()["error_code"] == "UNSUPPORTED_IMAGE"


@pytest.mark.parametrize("tool_type,options,count", [
    ("image_batch_ratio", {"ratio": "portrait_3_4", "fitMode": "crop"}, 1),
    ("image_long_stitch", {"direction": "vertical", "gap": 20}, 2),
    ("image_round_corners", {"radiusPercent": 20}, 1),
    ("image_convert", {"format": "png"}, 1),
    ("image_pad", {"ratio": "square_11", "background": "#f5f5f5"}, 1),
    ("image_resize", {"width": 200, "percent": 100}, 1),
])
def test_remaining_server_tools(tmp_path, tool_type, options, count):
    _, client = _client(tmp_path)
    files = [_upload(client, f"{index}.png", _png(color=(80 + index * 40, 120, 200)))
             for index in range(count)]
    _, job = _run_job(client, tool_type, sourceFiles=files, options=options)
    assert job["status"] == "done", job
    assert job["results"]
    served = client.get(job["results"][0]["url"])
    assert served.status_code == 200
    assert len(served.data) > 0
