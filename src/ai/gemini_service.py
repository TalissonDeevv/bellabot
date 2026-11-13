"""Compatibilidade temporária para extensões que ainda usam o nome antigo."""

from .groq_service import GroqCooldownError, GroqService, MediaInput

GeminiCooldownError = GroqCooldownError
GeminiService = GroqService

__all__ = [
    "GeminiCooldownError",
    "GeminiService",
    "MediaInput",
]
