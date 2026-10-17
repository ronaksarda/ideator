import os
import pytest
from ideator.backend.config import Settings


def test_settings_defaults():
    s = Settings(_env_file=None)
    assert s.falkordb_host == "localhost"
    assert s.falkordb_port == 6379
    assert s.mock_serpapi is True
    assert s.mock_tabpfn is True
    assert s.mock_gemma is True
    assert s.mock_elevenlabs is True


def test_settings_with_keys(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "serp_secret")
    monkeypatch.setenv("TABPFN_API_KEY", "tab_secret")
    monkeypatch.setenv("GEMMA_API_KEY", "gemma_secret")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven_secret")
    monkeypatch.setenv("FALKORDB_PORT", "6380")

    s = Settings()
    assert s.falkordb_port == 6380
    assert s.serpapi_api_key == "serp_secret"
    assert s.mock_serpapi is False
    assert s.tabpfn_api_key == "tab_secret"
    assert s.mock_tabpfn is False
    assert s.gemma_api_key == "gemma_secret"
    assert s.mock_gemma is False
    assert s.elevenlabs_api_key == "eleven_secret"
    assert s.mock_elevenlabs is False


def test_settings_with_gemini_api_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gemini_secret")
    s = Settings()
    assert s.gemini_api_key == "gemini_secret"
    assert s.mock_gemma is False
