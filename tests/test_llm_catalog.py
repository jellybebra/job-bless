import pytest

from src.llm.catalog import is_text_model, text_model_ids
from src.llm.models import ModelInfo


@pytest.mark.parametrize("name", [
    "gemini-robotics-er-1.6-preview", "models/gemini-robotics-er-2-preview",
    "gemini-2.5-computer-use-preview-10-2025", "gemini-2.5-flash-image",
    "gemini-3.1-flash-tts-preview", "gemini-embedding-2", "imagen-4.0-generate-001",
    "veo-3.0-generate-preview", "gpt-4o-audio-preview", "gpt-realtime",
    "whisper-1", "text-embedding-3-small", "omni-moderation-latest", "rerank-v3.5",
    "gpt-4o-mini-transcribe", "lyria-002",
])
def test_specialized_models_are_not_offered_for_text_work(name):
    assert not is_text_model(ModelInfo(id=name))


def test_keep_multimodal_and_custom_chat_models_and_deduplicate():
    names = ['gemini-2.5-pro', 'gemma-4-31b-it', 'gpt-4-vision-preview',
             'claude-sonnet-4', 'my-local-model', 'gemini-2.5-pro', '', 'gemini-robotics-er-2-preview']
    assert text_model_ids(ModelInfo(id=name) for name in names) == sorted(set(names[:5]))


def test_explicit_capabilities_restrict_unknown_names():
    assert not is_text_model(ModelInfo(id='special-001', raw={'supportedGenerationMethods': ['embedContent']}))
    assert not is_text_model(ModelInfo(id='special-002', raw={'architecture': {'output_modalities': ['image']}}))
    assert is_text_model(ModelInfo(id='chat-001', raw={'supportedGenerationMethods': ['generateContent']}))
    assert is_text_model(ModelInfo(id='chat-002', raw={'output_modalities': ['text']}))


def test_health_catalog_and_count_exclude_specialized_models(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from src.config import LLMConfig
    from src.llm import health

    class Client:
        capabilities = SimpleNamespace(model_listing=True)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def list_models(self):
            return [ModelInfo(id=name) for name in ['gemini-2.5-pro', 'gemini-robotics-er-2-preview', 'gemini-2.5-flash-image']]

    monkeypatch.setattr(health, 'create_llm_client', lambda config: Client())
    result = asyncio.run(health.check_llm(LLMConfig(enabled=True)))
    assert result.ok
    assert result.models == ['gemini-2.5-pro']
    assert result.models_available == 1
