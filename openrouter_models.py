from dataclasses import dataclass
from typing import Optional


class OpenRouterModelError(ValueError):
    """Raised when an OpenRouter model is invalid or unrecognized."""
    pass


@dataclass(frozen=True)
class OpenRouterModelSpec:
    model_id: str
    friendly_name: str
    text: bool
    vision: bool
    audio_input: bool
    context_limit: int
    output_limit: int
    status: str = "active"


OPENROUTER_DEFAULT_MODEL = "openai/gpt-5.6-terra"

OPENROUTER_MODEL_SPECS: dict[str, OpenRouterModelSpec] = {
    "openai/gpt-5.6-terra": OpenRouterModelSpec("openai/gpt-5.6-terra", "OpenAI GPT-5.6 Terra", True, True, False, 1050000, 128000),
    "openai/gpt-5.6-sol": OpenRouterModelSpec("openai/gpt-5.6-sol", "OpenAI GPT-5.6 Sol", True, True, False, 1050000, 128000),
    "openai/gpt-5.6-luna-pro": OpenRouterModelSpec("openai/gpt-5.6-luna-pro", "OpenAI GPT-5.6 Luna Pro", True, True, False, 1050000, 128000),
    "google/gemini-3.7-flash": OpenRouterModelSpec("google/gemini-3.7-flash", "Google Gemini 3.7 Flash", True, True, True, 1048576, 65536),
    "google/gemini-3.8-flash": OpenRouterModelSpec("google/gemini-3.8-flash", "Google Gemini 3.8 Flash", True, True, True, 1048576, 65536),
    "google/gemini-3.1-pro-preview": OpenRouterModelSpec("google/gemini-3.1-pro-preview", "Google Gemini 3.1 Pro Preview", True, True, True, 1048576, 65536),
    "anthropic/claude-sonnet-4.6": OpenRouterModelSpec("anthropic/claude-sonnet-4.6", "Anthropic Claude Sonnet 4.6", True, True, False, 1000000, 128000),
    "anthropic/claude-opus-4.6": OpenRouterModelSpec("anthropic/claude-opus-4.6", "Anthropic Claude Opus 4.6", True, True, False, 1000000, 128000),
    "anthropic/claude-haiku-4.5": OpenRouterModelSpec("anthropic/claude-haiku-4.5", "Anthropic Claude Haiku 4.5", True, True, False, 200000, 64000),
    "x-ai/grok-4.7": OpenRouterModelSpec("x-ai/grok-4.7", "xAI Grok 4.7", True, True, False, 500000, 450000),
    "x-ai/grok-4.6": OpenRouterModelSpec("x-ai/grok-4.6", "xAI Grok 4.6", True, True, False, 500000, 450000),
    "deepseek/deepseek-v4.1-flash": OpenRouterModelSpec("deepseek/deepseek-v4.1-flash", "DeepSeek V4.1 Flash", True, True, False, 1048576, 393216),
    "qwen/qwen3-vl-235b-a22b-instruct": OpenRouterModelSpec("qwen/qwen3-vl-235b-a22b-instruct", "Qwen3 VL 235B Instruct", True, True, False, 262144, 32768),
    "moonshotai/kimi-k2.6": OpenRouterModelSpec("moonshotai/kimi-k2.6", "Kimi K2.6", True, True, False, 262144, 235929),
    "mistralai/mistral-medium-3-5": OpenRouterModelSpec("mistralai/mistral-medium-3-5", "Mistral Medium 3.5", True, True, False, 262144, 209715),
}

OPENROUTER_MODELS = tuple(OPENROUTER_MODEL_SPECS.keys())


def is_valid_openrouter_model(model: Optional[str]) -> bool:
    """Return True if model is non-empty and present in the curated catalog."""
    if not model:
        return False
    return str(model).strip() in OPENROUTER_MODEL_SPECS


def resolve_openrouter_model(model: Optional[str]) -> str:
    """
    Resolve model according to strict rules:
    - None / empty / whitespace -> OPENROUTER_DEFAULT_MODEL
    - non-empty and in curated catalog -> exact model
    - non-empty but not in curated catalog -> raises OpenRouterModelError
    """
    if model is None or not str(model).strip():
        return OPENROUTER_DEFAULT_MODEL
    cleaned = str(model).strip()
    if cleaned in OPENROUTER_MODEL_SPECS:
        return cleaned
    raise OpenRouterModelError(f"Недопустимая модель OpenRouter: '{cleaned}'. Модель отсутствует в каталоге.")


def resolve_openrouter_fallback_model(
    configured_fallback_model: Optional[str],
    configured_primary_model: Optional[str],
) -> str:
    """
    Strict OpenRouter fallback resolution:
    A. fallback_model is non-empty:
       - must be valid curated model, otherwise raise OpenRouterModelError.
    B. fallback_model is empty:
       - use configured_primary_model if non-empty (must be valid, otherwise raise OpenRouterModelError).
    C. both empty:
       - return OPENROUTER_DEFAULT_MODEL.
    """
    fb_clean = (configured_fallback_model or "").strip()
    if fb_clean:
        if fb_clean in OPENROUTER_MODEL_SPECS:
            return fb_clean
        raise OpenRouterModelError(f"Недопустимая резервная модель OpenRouter: '{fb_clean}'.")

    primary_clean = (configured_primary_model or "").strip()
    if primary_clean:
        if primary_clean in OPENROUTER_MODEL_SPECS:
            return primary_clean
        raise OpenRouterModelError(f"Недопустимая модель OpenRouter в конфигурации: '{primary_clean}'.")

    return OPENROUTER_DEFAULT_MODEL


def get_openrouter_model_by_index(index: int) -> Optional[str]:
    """Retrieve model ID by 0-based catalog index or None if index out of range."""
    if 0 <= index < len(OPENROUTER_MODELS):
        return OPENROUTER_MODELS[index]
    return None


def get_openrouter_model_index(model: Optional[str]) -> Optional[int]:
    """Retrieve 0-based index for a model ID or None if not found."""
    if not model:
        return None
    cleaned = str(model).strip()
    try:
        return OPENROUTER_MODELS.index(cleaned)
    except ValueError:
        return None
