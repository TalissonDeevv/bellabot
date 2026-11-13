from __future__ import annotations

import asyncio

from website_guard.english_learning import (
    EnglishLearningDatabase,
    EnglishLearningManager,
    EnglishQuizCategoryView,
    EnglishStudyView,
    OBJECTIVES,
    XP_PER_LEVEL,
    english_quiz_picker_embed,
    random_quiz,
)
from website_guard.state import StateStore


class FakeQuizService:
    available = True

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def generate_json(self, prompt, schema, **kwargs):
        self.prompts.append(str(prompt))
        return {
            "question": "Qual frase soa mais natural para dizer que você procura uma vaga dev?",
            "options": [
                "I search developer work.",
                "I am looking for a developer job.",
                "I make job developer.",
                "I want one work of code.",
            ],
            "answer_index": 1,
            "explanation": "`I am looking for a developer job` é a forma mais natural nesse contexto.",
            "word": "looking for",
            "difficulty": "A2",
        }


def test_english_profile_activity_and_ranking(tmp_path) -> None:
    db = EnglishLearningDatabase(tmp_path / "english_learning.db")
    profile = db.ensure_profile(10, 100)

    assert profile.cefr_level == "A1"
    assert profile.objective in OBJECTIVES
    assert profile.xp == 0

    updated = db.record_activity(
        guild_id=10,
        user_id=100,
        kind="quiz",
        xp=120,
        duration_seconds=300,
        correct=1,
        total=1,
        words=["deploy", "issue"],
    )

    assert updated.xp == 120
    assert updated.study_seconds == 300
    assert updated.words_learned == 2
    assert updated.exercises_done == 1
    assert updated.accuracy == 100
    assert "first_quiz" in updated.achievements
    assert "no_mistakes" in updated.achievements

    ranking = db.ranking(10, "xp")
    assert ranking[0]["user_id"] == 100
    assert ranking[0]["score"] == 120


def test_english_objective_placement_and_friendship(tmp_path) -> None:
    db = EnglishLearningDatabase(tmp_path / "english_learning.db")

    objective = db.set_objective(10, 100, "vaga-internacional")
    assert objective.objective == "vaga-internacional"
    assert "entrevista" in objective.current_lesson.lower()

    placed = db.record_placement(10, 100, [2, 2, 2, 2, 0])
    assert placed.cefr_level == "B2"
    assert placed.xp >= 120

    db.add_friend(10, 100, 200)
    assert db.friends(10, 100) == [200]
    assert db.friends(10, 200) == [100]


def test_english_level_progress(tmp_path) -> None:
    db = EnglishLearningDatabase(tmp_path / "english_learning.db")
    profile = db.record_activity(
        guild_id=10,
        user_id=100,
        kind="study:tech",
        xp=XP_PER_LEVEL + 150,
        duration_seconds=600,
    )

    assert profile.game_level == 2
    assert profile.level_progress == 150
    assert "tech_master" in profile.achievements


def test_english_learning_thread_storage(tmp_path) -> None:
    db = EnglishLearningDatabase(tmp_path / "english_learning.db")
    assert db.active_thread(10, 100) is None

    saved = db.save_thread(
        guild_id=10,
        user_id=100,
        thread_id=300,
        channel_id=400,
    )

    assert saved["thread_id"] == 300
    assert saved["channel_id"] == 400
    assert saved["status"] == "active"
    assert db.active_thread(10, 100)["thread_id"] == 300
    assert db.thread_by_id(10, 300)["user_id"] == 100


def test_english_learning_onboarding_profile(tmp_path) -> None:
    db = EnglishLearningDatabase(tmp_path / "english_learning.db")

    profile = db.update_learning_profile(
        guild_id=10,
        user_id=100,
        objective="vaga-internacional",
        goal_reason="Quero aprender inglês para entrevistas no exterior.",
        learning_focus="escrita,pronuncia,conversacao",
        onboarding_completed=True,
        onboarding_step="done",
    )

    assert profile.objective == "vaga-internacional"
    assert profile.goal_reason.startswith("Quero aprender inglês")
    assert profile.learning_focus == "escrita,pronuncia,conversacao"
    assert profile.onboarding_completed is True
    assert profile.onboarding_step == "done"


def test_english_dynamic_quiz_generation(tmp_path) -> None:
    service = FakeQuizService()
    manager = EnglishLearningManager(StateStore(tmp_path / "state.json"), service)
    manager.db.update_learning_profile(
        guild_id=10,
        user_id=100,
        objective="vaga-internacional",
        goal_reason="Quero entrevistas no exterior.",
        learning_focus="escrita,conversacao",
        onboarding_completed=True,
    )

    quiz = asyncio.run(manager.generate_quiz(10, 100, "escrita"))

    assert quiz["source"] == "groq"
    assert quiz["category"] == "escrita"
    assert len(quiz["options"]) == 4
    assert 0 <= quiz["answer"] <= 3
    assert quiz["options"][quiz["answer"]] == "I am looking for a developer job."
    assert "entrevistas no exterior" in service.prompts[0]


def test_english_random_quiz_supports_dynamic_categories() -> None:
    quiz = random_quiz("pronuncia")

    assert quiz["category"] == "pronuncia"
    assert quiz["source"] == "local"
    assert len(quiz["options"]) == 4


def test_english_study_view_has_learning_shortcuts(tmp_path) -> None:
    manager = EnglishLearningManager(StateStore(tmp_path / "state.json"), FakeQuizService())
    view = EnglishStudyView(manager, 10, 100)
    labels = {
        getattr(item, "label", None)
        for item in view.children
        if getattr(item, "label", None)
    }

    assert {"Perfil", "Dashboard", "Plano", "Ranking", "Quiz", "Teste de nível"} <= labels


def test_english_quiz_category_view_offers_learning_focuses(tmp_path) -> None:
    manager = EnglishLearningManager(StateStore(tmp_path / "state.json"), FakeQuizService())
    view = EnglishQuizCategoryView(manager, 10, 100)
    select = next(item for item in view.children if hasattr(item, "options"))
    values = {option.value for option in select.options}
    embed = english_quiz_picker_embed()

    assert {
        "vocabulario",
        "gramatica",
        "conversacao",
        "escrita",
        "pronuncia",
        "tech",
    } <= values
    assert "Escolha o foco" in str(embed.title)
