"""Models suitable for the application's ordinary text-generation tasks.

Provider catalogs can mix chat models with models for media generation,
robotics, embeddings and other APIs. Missing capability metadata is common,
so keep unfamiliar/custom model IDs unless they explicitly indicate a
specialized task.
"""

import re
from collections.abc import Iterable

from src.llm.models import ModelInfo


_SPECIALIZED = re.compile(
    r"(?:^|[/_.:\-])(?:robotics|computer[-_]use|embeddings?|rerank(?:er)?|"
    r"moderation|images?|imagen|veo|video|audio|tts|whisper|transcri(?:be|ption)|"
    r"realtime|music|lyria)(?=$|[/_.:\-]|\d)",
    re.IGNORECASE,
)


def is_text_model(model: ModelInfo) -> bool:
    if not model.id or _SPECIALIZED.search(model.id):
        return False
    methods = model.raw.get("supportedGenerationMethods")
    if isinstance(methods, list) and "generateContent" not in methods:
        return False
    architecture = model.raw.get("architecture")
    if not isinstance(architecture, dict):
        architecture = {}
    outputs = model.raw.get("output_modalities", architecture.get("output_modalities"))
    if isinstance(outputs, list) and "text" not in outputs:
        return False
    return True


def text_model_ids(models: Iterable[ModelInfo]) -> list[str]:
    return sorted({model.id for model in models if is_text_model(model)})
