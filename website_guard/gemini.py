"""Compatibility imports for the modular AI package."""

from src.ai.gemini_service import GeminiService
from src.ai.intent_parser import GeminiIntentParser
from src.ai.knowledge import AIKnowledgeResponder, GeminiKnowledgeDecision

GeminiEngine = GeminiService

__all__ = [
    "AIKnowledgeResponder",
    "GeminiEngine",
    "GeminiIntentParser",
    "GeminiKnowledgeDecision",
    "GeminiService",
]
