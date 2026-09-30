import json
from unittest.mock import MagicMock, patch
import pytest
import requests
import etsy


@pytest.fixture
def mock_token_file(tmp_path, monkeypatch):
    token_data = {
        "access_token": "mock_user_123.sample_access_token_jwt",
        "refresh_token": "mock_refresh_token_xyz",
        "expires_in": 3600,
        "token_type": "Bearer",
    }
    shop_name = "testshop"
    path = tmp_path / f"tokens_{shop_name}.json"
    path.write_text(json.dumps(token_data), encoding="utf-8")

    monkeypatch.setattr(etsy, "KEY", "test_api_keystring")
    monkeypatch.setattr(etsy, "SECRET", "test_api_secret")
    monkeypatch.setattr(etsy, "API_KEY", "test_api_keystring:test_api_secret")

    # Patch Etsy init to use the temporary token path
    orig_init = etsy.Etsy.__init__

    def custom_init(self, shop):
        self.shop_name = shop
        self.path = str(path)
        with open(self.path, "r", encoding="utf-8") as f:
            self.tokens = json.load(f)
        self.user_id = self.tokens["access_token"].split(".")[0]
        self.shop_id = None

    monkeypatch.setattr(etsy.Etsy, "__init__", custom_init)
    return str(path)


def test_client_init_and_user_id(mock_token_file):
    client = etsy.Etsy("testshop")
    assert client.user_id == "mock_user_123"
    assert client.tokens["refresh_token"] == "mock_refresh_token_xyz"


@patch("requests.get")
def test_client_get_success(mock_get, mock_token_file):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"results": [{"receipt_id": 1}], "count": 1}
    mock_get.return_value = mock_resp

    client = etsy.Etsy("testshop")
    data = client.get("/shops/123/receipts")

    assert data["count"] == 1
    mock_get.assert_called_once()
    headers = mock_get.call_args[1]["headers"]
    assert headers["x-api-key"] == "test_api_keystring:test_api_secret"
    assert headers["Authorization"] == "Bearer mock_user_123.sample_access_token_jwt"


@patch("requests.post")
@patch("requests.get")
def test_client_401_auto_refresh(mock_get, mock_post, mock_token_file):
    # First GET returns 401, second GET returns 200
    resp_401 = MagicMock()
    resp_401.status_code = 401

    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.json.return_value = {"results": [{"receipt_id": 2}], "count": 1}

    mock_get.side_effect = [resp_401, resp_200]

    # Refresh POST returns new tokens
    refresh_resp = MagicMock()
    refresh_resp.status_code = 200
    refresh_resp.json.return_value = {
        "access_token": "mock_user_123.new_refreshed_access_token",
        "refresh_token": "mock_new_refresh_token",
        "expires_in": 3600,
    }
    mock_post.return_value = refresh_resp

    client = etsy.Etsy("testshop")
    data = client.get("/shops/123/receipts")

    assert data["results"][0]["receipt_id"] == 2
    assert mock_get.call_count == 2
    mock_post.assert_called_once()
    assert client.tokens["access_token"] == "mock_user_123.new_refreshed_access_token"

    # Verify updated token file was written
    with open(mock_token_file, "r", encoding="utf-8") as f:
        saved = json.load(f)
        assert saved["access_token"] == "mock_user_123.new_refreshed_access_token"


@patch("requests.get")
def test_client_receipts_pagination(mock_get, mock_token_file):
    client = etsy.Etsy("testshop")
    client.shop_id = 9999

    page1 = {"count": 150, "results": [{"receipt_id": i} for i in range(100)]}
    page2 = {"count": 150, "results": [{"receipt_id": i} for i in range(100, 150)]}

    mock_resp1 = MagicMock(status_code=200)
    mock_resp1.json.return_value = page1

    mock_resp2 = MagicMock(status_code=200)
    mock_resp2.json.return_value = page2

    mock_get.side_effect = [mock_resp1, mock_resp2]

    receipts = client.receipts()
    assert len(receipts) == 150
    assert mock_get.call_count == 2
