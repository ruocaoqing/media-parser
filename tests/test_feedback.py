import os
from unittest.mock import patch

from app import create_app


def _client(tmp_path):
    app = create_app({
        "TESTING": True,
        "API_ONLY": True,
        "DATABASE": str(tmp_path / "feedback.db"),
    })
    return app.test_client()


def test_feedback_is_forwarded_by_the_server(tmp_path):
    client = _client(tmp_path)

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"errcode": 0}

    with patch.dict(os.environ, {"FEEDBACK_WEBHOOK_URL": "https://example.test/hook"}), \
         patch("src.api.feedback.requests.post", return_value=FakeResponse()) as post:
        response = client.post("/api/v1/feedback", json={
            "type": "advice",
            "description": "希望增加新布局",
            "clientId": "anonymous-client",
            "version": "0.4.0",
        })

    assert response.status_code == 200
    assert response.get_json()["data"]["sent"] is True
    payload = post.call_args.kwargs["json"]
    assert payload["msgtype"] == "text"
    assert "希望增加新布局" in payload["text"]["content"]
    assert "anonymous-client" in payload["text"]["content"]


def test_feedback_requires_server_configuration_and_valid_input(tmp_path):
    client = _client(tmp_path)
    with patch.dict(os.environ, {"FEEDBACK_WEBHOOK_URL": ""}):
        response = client.post("/api/v1/feedback", json={"type": "other", "description": "内容"})
    assert response.status_code == 503

    with patch.dict(os.environ, {"FEEDBACK_WEBHOOK_URL": "https://example.test/hook"}):
        response = client.post("/api/v1/feedback", json={"type": "other", "description": " "})
    assert response.status_code == 400
    assert response.get_json()["error_code"] == "INVALID_FEEDBACK"
