from __future__ import annotations

import asyncio
import io
import json
import math
import random
import re
import sqlite3
import threading
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import discord
from PIL import Image, ImageDraw, ImageFont

from src.ai.groq_service import GroqService
from src.ai.personality import BELLA_PERSONA_RUNTIME
from .embed_builder import EmbedDraft
from .state import StateStore

ENGLISH_COLOR = 0x5B7CFA
XP_PER_LEVEL = 1000
CEFR_LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")
ENGLISH_PANEL_START_CUSTOM_ID = "jarvis:english:start"
ENGLISH_PANEL_PROFILE_CUSTOM_ID = "jarvis:english:profile"
ENGLISH_PANEL_PLACEMENT_CUSTOM_ID = "jarvis:english:placement"
ENGLISH_PANEL_QUIZ_CUSTOM_ID = "jarvis:english:quiz"
ENGLISH_PANEL_RANKING_CUSTOM_ID = "jarvis:english:ranking"
ENGLISH_SETTINGS_SECTION = "english_learning_settings"
ENGLISH_PANEL_DEFAULT_DESCRIPTION = (
    "Uma central de estudos de inglês focada em tecnologia, carreira e "
    "conversas reais.\n\n"
    "Você pode começar do zero, fazer teste de nível, praticar Tech "
    "English, ganhar XP, manter sequência, desbloquear conquistas e "
    "subir no ranking do servidor."
)
ENGLISH_PANEL_DEFAULT_FIELDS = [
    {
        "name": "Como começar",
        "value": (
            "Clique em **Começar agora** para iniciar seus estudos ou em "
            "**Teste de nível** para a Bella estimar seu nível A1 até C2."
        ),
        "inline": False,
    },
    {
        "name": "O que tem aqui",
        "value": (
            "Tech English, vocabulário, gramática, conversação, entrevistas, "
            "quiz, missões, ranking, conquistas e perfil visual."
        ),
        "inline": False,
    },
    {
        "name": "Foco",
        "value": "Inglês para programação, trabalho remoto, documentação, entrevistas e clientes.",
        "inline": False,
    },
]
OBJECTIVES = {
    "vaga-internacional": "Conseguir vaga internacional",
    "trabalho-remoto": "Trabalhar remoto",
    "documentacao": "Ler documentação",
    "clientes": "Conversar com clientes",
    "escrita": "Melhorar escrita",
    "zero": "Aprender do zero",
}

QUIZ_BANK: dict[str, list[dict[str, Any]]] = {
    "vocabulario": [
        {
            "question": "O que significa `meeting` em inglês?",
            "options": [
                "Reunião",
                "Mensagem",
                "Viagem",
                "Contrato",
            ],
            "answer": 0,
            "explanation": "`Meeting` significa reunião. Exemplo: `I have a meeting today.`",
            "word": "meeting",
        },
        {
            "question": "`I need help` significa:",
            "options": [
                "Eu preciso de ajuda",
                "Eu quero dormir",
                "Eu estou atrasado",
                "Eu já terminei",
            ],
            "answer": 0,
            "explanation": "`Need` expressa necessidade. A frase quer dizer que a pessoa precisa de ajuda.",
            "word": "need",
        },
    ],
    "gramatica": [
        {
            "question": "Complete: `She ____ English every day.`",
            "options": ["write", "writes", "writing", "wrote"],
            "answer": 1,
            "explanation": "Com `she/he/it` no presente simples, usamos `writes`.",
            "word": "writes",
        },
        {
            "question": "Qual frase está correta?",
            "options": [
                "I am work now.",
                "I working now.",
                "I am working now.",
                "I works now.",
            ],
            "answer": 2,
            "explanation": "Presente contínuo: sujeito + am/is/are + verbo com -ing.",
            "word": "working",
        },
    ],
    "conversacao": [
        {
            "question": "Qual resposta soa mais natural para `How are you?`",
            "options": [
                "I'm good, thanks.",
                "I am a good.",
                "I have good.",
                "Good I am.",
            ],
            "answer": 0,
            "explanation": "`I'm good, thanks` é uma resposta natural e simples para cumprimentar alguém.",
            "word": "I'm good",
        },
        {
            "question": "Como perguntar naturalmente `Você pode repetir?` em inglês?",
            "options": [
                "Can you repeat that?",
                "Can repeat you?",
                "You repeat can?",
                "Repeat this you?",
            ],
            "answer": 0,
            "explanation": "`Can you repeat that?` é direto e muito usado em conversa.",
            "word": "repeat",
        },
    ],
    "escrita": [
        {
            "question": "Qual frase está mais natural para uma mensagem curta?",
            "options": [
                "I will check it and get back to you.",
                "I check and back you.",
                "I will see this and return you.",
                "I go verify and speak.",
            ],
            "answer": 0,
            "explanation": "`I will check it and get back to you` é uma forma natural de dizer que vai verificar e retornar.",
            "word": "get back to you",
        },
        {
            "question": "Qual frase é melhor para agradecer uma resposta?",
            "options": [
                "Thanks for your reply.",
                "Thanks by your answer.",
                "Thanks to answer me.",
                "Thank you for answer.",
            ],
            "answer": 0,
            "explanation": "`Thanks for your reply` usa `for` para indicar o motivo do agradecimento.",
            "word": "reply",
        },
    ],
    "pronuncia": [
        {
            "question": "Qual palavra tem som inicial parecido com `tch` em portuguêsó",
            "options": ["chair", "hour", "user", "one"],
            "answer": 0,
            "explanation": "`Chair` começa com som parecido com `tchér`. Isso ajuda na pronúncia inicial.",
            "word": "chair",
        },
        {
            "question": "Em `thought`, o `th` tem um som que não existe igual em português. Qual dica ajuda maisó",
            "options": [
                "Colocar a língua levemente entre os dentes e soltar o ar",
                "Falar como se fosse `t` forte",
                "Trocar sempre por som de `f`",
                "Ignorar o som e falar só `ought`",
            ],
            "answer": 0,
            "explanation": "O `th` exige ar passando com a língua levemente entre os dentes. Não é `t` nem `f`.",
            "word": "th",
        },
    ],
    "tech": [
        {
            "question": "Em Git, `pull request` é:",
            "options": [
                "Uma solicitação para revisar e integrar alterações",
                "Um comando para deletar branch",
                "Um tipo de servidor",
                "Uma senha temporária",
            ],
            "answer": 0,
            "explanation": "Um PR propõe mudanças para revisão antes do merge.",
            "word": "pull request",
        },
        {
            "question": "`Environment variables` são:",
            "options": [
                "Variáveis de ambiente usadas para configurar aplicações",
                "Cores do terminal",
                "Arquivos de imagem",
                "Mensagens de commit",
            ],
            "answer": 0,
            "explanation": "Elas guardam configurações como tokens, URLs e portas.",
            "word": "environment variables",
        },
    ],
}

ACHIEVEMENTS = {
    "first_exercise": ("Primeiro exercício", "Concluiu o primeiro exercício."),
    "first_conversation": ("Primeira conversa", "Praticou conversação em inglês."),
    "first_quiz": ("Primeiro Quiz", "Completou o primeiro quiz."),
    "first_week": ("Primeira semana", "Manteve atividade por 7 dias."),
    "streak_30": ("30 dias", "Alcançou 30 dias de sequência."),
    "streak_100": ("100 dias", "Alcançou 100 dias de sequência."),
    "words_100": ("100 palavras", "Aprendeu 100 palavras."),
    "words_500": ("500 palavras", "Aprendeu 500 palavras."),
    "grammar_master": ("Mestre da Gramática", "Acumulou alto desempenho em gramática."),
    "vocab_king": ("Rei do Vocabulário", "Dominou muitos termos de vocabulário."),
    "no_mistakes": ("Sem erros", "Fez uma atividade sem errar."),
    "marathon": ("Maratonista", "Estudou por bastante tempo acumulado."),
    "tech_master": ("Tech English Master", "Avançou no inglês para tecnologia."),
}


@dataclass(frozen=True)
class EnglishProfile:
    guild_id: int
    user_id: int
    xp: int
    cefr_level: str
    objective: str
    streak: int
    best_streak: int
    study_seconds: int
    words_learned: int
    exercises_done: int
    correct_answers: int
    wrong_answers: int
    conversations: int
    started_at: str
    last_activity: str | None
    current_lesson: str
    title: str
    theme: str
    goal_reason: str
    learning_focus: str
    onboarding_completed: bool
    onboarding_step: str
    achievements: list[str]

    @property
    def game_level(self) -> int:
        return max(1, self.xp // XP_PER_LEVEL + 1)

    @property
    def level_progress(self) -> int:
        return self.xp % XP_PER_LEVEL

    @property
    def accuracy(self) -> float:
        total = self.correct_answers + self.wrong_answers
        return (self.correct_answers / total * 100) if total else 0.0


class EnglishLearningDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS english_profiles (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    xp INTEGER NOT NULL DEFAULT 0,
                    cefr_level TEXT NOT NULL DEFAULT 'A1',
                    objective TEXT NOT NULL DEFAULT 'documentacao',
                    streak INTEGER NOT NULL DEFAULT 0,
                    best_streak INTEGER NOT NULL DEFAULT 0,
                    study_seconds INTEGER NOT NULL DEFAULT 0,
                    words_learned INTEGER NOT NULL DEFAULT 0,
                    exercises_done INTEGER NOT NULL DEFAULT 0,
                    correct_answers INTEGER NOT NULL DEFAULT 0,
                    wrong_answers INTEGER NOT NULL DEFAULT 0,
                    conversations INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    last_activity TEXT,
                    current_lesson TEXT NOT NULL DEFAULT 'Primeiros passos',
                    title TEXT NOT NULL DEFAULT 'Dev em evolução',
                    theme TEXT NOT NULL DEFAULT 'midnight',
                    goal_reason TEXT NOT NULL DEFAULT '',
                    learning_focus TEXT NOT NULL DEFAULT 'escrita,pronuncia,conversacao',
                    onboarding_completed INTEGER NOT NULL DEFAULT 0,
                    onboarding_step TEXT NOT NULL DEFAULT 'goal',
                    achievements TEXT NOT NULL DEFAULT '[]',
                    favorites TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY (guild_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS english_activity (
                    activity_id TEXT PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    xp INTEGER NOT NULL DEFAULT 0,
                    duration_seconds INTEGER NOT NULL DEFAULT 0,
                    correct INTEGER NOT NULL DEFAULT 0,
                    total INTEGER NOT NULL DEFAULT 0,
                    words TEXT NOT NULL DEFAULT '[]',
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_english_activity_user
                    ON english_activity(guild_id, user_id, created_at);

                CREATE TABLE IF NOT EXISTS english_placement (
                    attempt_id TEXT PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    score INTEGER NOT NULL,
                    level TEXT NOT NULL,
                    answers TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS english_friends (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    friend_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, user_id, friend_id)
                );

                CREATE TABLE IF NOT EXISTS english_threads (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    last_activity TEXT NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                );
                """
            )
            self._ensure_profile_columns(connection)

    def _ensure_profile_columns(self, connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(english_profiles)").fetchall()
        }
        migrations = {
            "goal_reason": "ALTER TABLE english_profiles ADD COLUMN goal_reason TEXT NOT NULL DEFAULT ''",
            "learning_focus": (
                "ALTER TABLE english_profiles ADD COLUMN learning_focus TEXT NOT NULL "
                "DEFAULT 'escrita,pronuncia,conversacao'"
            ),
            "onboarding_completed": (
                "ALTER TABLE english_profiles ADD COLUMN onboarding_completed INTEGER NOT NULL DEFAULT 0"
            ),
            "onboarding_step": (
                "ALTER TABLE english_profiles ADD COLUMN onboarding_step TEXT NOT NULL DEFAULT 'goal'"
            ),
        }
        for column, statement in migrations.items():
            if column not in columns:
                connection.execute(statement)

    def ensure_profile(self, guild_id: int, user_id: int) -> EnglishProfile:
        now = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO english_profiles (
                    guild_id, user_id, started_at, last_activity
                ) VALUES (?, ?, ?, NULL)
                """,
                (guild_id, user_id, now),
            )
            row = connection.execute(
                "SELECT * FROM english_profiles WHERE guild_id=? AND user_id=?",
                (guild_id, user_id),
            ).fetchone()
        return _profile_from_row(row)

    def profile(self, guild_id: int, user_id: int) -> EnglishProfile:
        return self.ensure_profile(guild_id, user_id)

    def set_objective(self, guild_id: int, user_id: int, objective: str) -> EnglishProfile:
        self.ensure_profile(guild_id, user_id)
        objective = objective if objective in OBJECTIVES else "documentacao"
        lesson = _lesson_for_objective(objective)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE english_profiles
                   SET objective=?, current_lesson=?
                 WHERE guild_id=? AND user_id=?
                """,
                (objective, lesson, guild_id, user_id),
            )
        return self.profile(guild_id, user_id)

    def update_learning_profile(
        self,
        *,
        guild_id: int,
        user_id: int,
        objective: str | None = None,
        goal_reason: str | None = None,
        learning_focus: str | None = None,
        onboarding_completed: bool | None = None,
        onboarding_step: str | None = None,
    ) -> EnglishProfile:
        self.ensure_profile(guild_id, user_id)
        updates: list[str] = []
        values: list[Any] = []
        if objective:
            objective = objective if objective in OBJECTIVES else "documentacao"
            updates.extend(["objective=?", "current_lesson=?"])
            values.extend([objective, _lesson_for_objective(objective)])
        if goal_reason is not None:
            updates.append("goal_reason=?")
            values.append(goal_reason.strip()[:1200])
        if learning_focus is not None:
            updates.append("learning_focus=?")
            values.append(learning_focus.strip()[:300] or "escrita,pronuncia,conversacao")
        if onboarding_completed is not None:
            updates.append("onboarding_completed=?")
            values.append(1 if onboarding_completed else 0)
        if onboarding_step is not None:
            updates.append("onboarding_step=?")
            values.append(onboarding_step.strip()[:40] or "goal")
        if updates:
            values.extend([guild_id, user_id])
            with self._lock, self._connect() as connection:
                connection.execute(
                    f"""
                    UPDATE english_profiles
                       SET {', '.join(updates)}
                     WHERE guild_id=? AND user_id=?
                    """,
                    values,
                )
        return self.profile(guild_id, user_id)

    def set_level(self, guild_id: int, user_id: int, level: str) -> EnglishProfile:
        self.ensure_profile(guild_id, user_id)
        level = level if level in CEFR_LEVELS else "A1"
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE english_profiles
                   SET cefr_level=?
                 WHERE guild_id=? AND user_id=?
                """,
                (level, guild_id, user_id),
            )
        return self.profile(guild_id, user_id)

    def record_activity(
        self,
        *,
        guild_id: int,
        user_id: int,
        kind: str,
        xp: int,
        duration_seconds: int = 0,
        correct: int = 0,
        total: int = 0,
        words: Iterable[str] = (),
        payload: dict[str, Any] | None = None,
    ) -> EnglishProfile:
        profile = self.ensure_profile(guild_id, user_id)
        today = date.today().isoformat()
        previous_day = _date_only(profile.last_activity)
        streak = profile.streak
        if previous_day != today:
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            streak = profile.streak + 1 if previous_day == yesterday else 1
        word_list = sorted({word.strip().casefold() for word in words if word.strip()})
        achievements = set(profile.achievements)
        achievements.update(
            _earned_achievements(
                kind=kind,
                streak=streak,
                study_seconds=profile.study_seconds + duration_seconds,
                words_learned=profile.words_learned + len(word_list),
                exercises_done=profile.exercises_done + 1,
                correct=correct,
                total=total,
            )
        )
        now = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO english_activity (
                    activity_id, guild_id, user_id, kind, xp, duration_seconds,
                    correct, total, words, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex[:12],
                    guild_id,
                    user_id,
                    kind,
                    max(0, xp),
                    max(0, duration_seconds),
                    max(0, correct),
                    max(0, total),
                    json.dumps(word_list, ensure_ascii=False),
                    json.dumps(payload or {}, ensure_ascii=False),
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE english_profiles
                   SET xp=xp+?,
                       streak=?,
                       best_streak=MAX(best_streak, ?),
                       study_seconds=study_seconds+?,
                       words_learned=words_learned+?,
                       exercises_done=exercises_done+1,
                       correct_answers=correct_answers+?,
                       wrong_answers=wrong_answers+?,
                       conversations=conversations+?,
                       last_activity=?,
                       achievements=?
                 WHERE guild_id=? AND user_id=?
                """,
                (
                    max(0, xp),
                    streak,
                    streak,
                    max(0, duration_seconds),
                    len(word_list),
                    max(0, correct),
                    max(0, total - correct),
                    1 if kind == "conversation" else 0,
                    now,
                    json.dumps(sorted(achievements), ensure_ascii=False),
                    guild_id,
                    user_id,
                ),
            )
        return self.profile(guild_id, user_id)

    def record_placement(
        self,
        guild_id: int,
        user_id: int,
        answers: list[int],
    ) -> EnglishProfile:
        score = sum(answers)
        level = _placement_level(score)
        self.set_level(guild_id, user_id, level)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO english_placement (
                    attempt_id, guild_id, user_id, score, level, answers, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex[:12],
                    guild_id,
                    user_id,
                    score,
                    level,
                    json.dumps(answers),
                    _now(),
                ),
            )
        return self.record_activity(
            guild_id=guild_id,
            user_id=user_id,
            kind="placement",
            xp=120,
            duration_seconds=420,
            correct=score,
            total=max(1, len(answers) * 2),
            words=("placement", level),
        )

    def history(
        self,
        guild_id: int,
        user_id: int,
        *,
        limit: int = 10,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM english_activity
                 WHERE guild_id=? AND user_id=?
                 ORDER BY created_at DESC
                 LIMIT ? OFFSET ?
                """,
                (guild_id, user_id, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def ranking(self, guild_id: int, metric: str, *, limit: int = 10) -> list[dict[str, Any]]:
        metric = metric if metric in {"xp", "sequencia", "exercicios", "palavras", "tempo"} else "xp"
        column = {
            "xp": "xp",
            "sequencia": "streak",
            "exercicios": "exercises_done",
            "palavras": "words_learned",
            "tempo": "study_seconds",
        }[metric]
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT user_id, xp, streak, exercises_done, words_learned,
                       study_seconds, correct_answers, wrong_answers,
                       {column} AS score
                  FROM english_profiles
                 WHERE guild_id=?
                 ORDER BY score DESC, xp DESC
                 LIMIT ?
                """,
                (guild_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def rank_position(self, guild_id: int, user_id: int, metric: str = "xp") -> int | None:
        rows = self.ranking(guild_id, metric, limit=1000)
        for index, row in enumerate(rows, start=1):
            if int(row["user_id"]) == user_id:
                return index
        return None

    def dashboard(self, guild_id: int, user_id: int) -> dict[str, Any]:
        profile = self.profile(guild_id, user_id)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT created_at, xp, duration_seconds, correct, total
                  FROM english_activity
                 WHERE guild_id=? AND user_id=?
                 ORDER BY created_at ASC
                """,
                (guild_id, user_id),
            ).fetchall()
        daily: dict[str, dict[str, int]] = {}
        for row in rows:
            day = str(row["created_at"])[:10]
            bucket = daily.setdefault(
                day,
                {"xp": 0, "duration_seconds": 0, "correct": 0, "total": 0},
            )
            bucket["xp"] += int(row["xp"] or 0)
            bucket["duration_seconds"] += int(row["duration_seconds"] or 0)
            bucket["correct"] += int(row["correct"] or 0)
            bucket["total"] += int(row["total"] or 0)
        return {"profile": profile, "daily": daily}

    def add_friend(self, guild_id: int, user_id: int, friend_id: int) -> None:
        now = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO english_friends (
                    guild_id, user_id, friend_id, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (guild_id, user_id, friend_id, now),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO english_friends (
                    guild_id, user_id, friend_id, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (guild_id, friend_id, user_id, now),
            )

    def friends(self, guild_id: int, user_id: int) -> list[int]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT friend_id FROM english_friends
                 WHERE guild_id=? AND user_id=?
                 ORDER BY created_at DESC
                """,
                (guild_id, user_id),
            ).fetchall()
        return [int(row["friend_id"]) for row in rows]

    def active_thread(self, guild_id: int, user_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM english_threads
                 WHERE guild_id=? AND user_id=? AND status='active'
                """,
                (guild_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def thread_by_id(self, guild_id: int, thread_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM english_threads
                 WHERE guild_id=? AND thread_id=? AND status='active'
                """,
                (guild_id, thread_id),
            ).fetchone()
        return dict(row) if row else None

    def touch_thread(self, guild_id: int, thread_id: int) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE english_threads
                   SET last_activity=?
                 WHERE guild_id=? AND thread_id=? AND status='active'
                """,
                (_now(), guild_id, thread_id),
            )

    def save_thread(
        self,
        *,
        guild_id: int,
        user_id: int,
        thread_id: int,
        channel_id: int,
    ) -> dict[str, Any]:
        now = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO english_threads (
                    guild_id, user_id, thread_id, channel_id, status,
                    created_at, last_activity
                ) VALUES (?, ?, ?, ?, 'active', ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    thread_id=excluded.thread_id,
                    channel_id=excluded.channel_id,
                    status='active',
                    last_activity=excluded.last_activity
                """,
                (guild_id, user_id, thread_id, channel_id, now, now),
            )
            row = connection.execute(
                """
                SELECT * FROM english_threads
                 WHERE guild_id=? AND user_id=?
                """,
                (guild_id, user_id),
            ).fetchone()
        return dict(row)


class EnglishLearningManager:
    def __init__(
        self,
        state: StateStore,
        service: GroqService,
    ) -> None:
        self.state = state
        self.service = service
        self.db = EnglishLearningDatabase(Path(state.path).parent / "english_learning.db")
        self.renderer = EnglishLearningRenderer()

    async def profile(self, guild_id: int, user_id: int) -> EnglishProfile:
        return await asyncio.to_thread(self.db.profile, guild_id, user_id)

    async def settings_for(self, guild_id: int) -> dict[str, Any]:
        values = await self.state.get_section(ENGLISH_SETTINGS_SECTION)
        raw = values.get(str(guild_id))
        if not isinstance(raw, dict):
            return {
                "enabled": True,
                "closed_reason": "Aprenda Inglês com Bella está em manutenção no momento.",
                "panel_channel_id": None,
                "panel_message_id": None,
                "panel_render_mode": "v1",
                "panel_v1_payload": {},
                "panel_v2_payload": {},
                "manager_role_ids": [],
                "message_content": "",
                "panel_title": "Aprenda Inglês com Bella",
                "panel_description": ENGLISH_PANEL_DEFAULT_DESCRIPTION,
                "panel_color": ENGLISH_COLOR,
                "panel_author_name": "",
                "panel_author_icon_url": "",
                "panel_thumbnail_url": "",
                "panel_image_url": "",
                "panel_image_position": "end",
                "panel_footer": "Bella English Learning • escolha uma opção abaixo",
                "panel_footer_icon_url": "",
                "panel_fields": [dict(item) for item in ENGLISH_PANEL_DEFAULT_FIELDS],
                "closed_block_title": "Sistema pausado no momento",
                "closed_block_template": "{motivo}",
            }
        fields = raw.get("panel_fields")
        if not isinstance(fields, list):
            fields = ENGLISH_PANEL_DEFAULT_FIELDS
        return {
            "enabled": bool(raw.get("enabled", True)),
            "closed_reason": str(
                raw.get(
                    "closed_reason",
                    "Aprenda Inglês com Bella está em manutenção no momento.",
                )
            ),
            "panel_channel_id": raw.get("panel_channel_id"),
            "panel_message_id": raw.get("panel_message_id"),
            "panel_render_mode": "v2" if str(raw.get("panel_render_mode", "v1")).lower() == "v2" else "v1",
            "panel_v1_payload": dict(raw.get("panel_v1_payload") or {}) if isinstance(raw.get("panel_v1_payload"), dict) else {},
            "panel_v2_payload": dict(raw.get("panel_v2_payload") or {}) if isinstance(raw.get("panel_v2_payload"), dict) else {},
            "manager_role_ids": [
                int(role_id)
                for role_id in (raw.get("manager_role_ids") or [])
                if str(role_id).isdigit()
            ],
            "message_content": str(raw.get("message_content") or "")[:1800],
            "panel_title": str(raw.get("panel_title") or "Aprenda Inglês com Bella")[:256],
            "panel_description": str(raw.get("panel_description") or ENGLISH_PANEL_DEFAULT_DESCRIPTION)[:4000],
            "panel_color": _safe_panel_color(raw.get("panel_color"), ENGLISH_COLOR),
            "panel_author_name": str(raw.get("panel_author_name") or "")[:256],
            "panel_author_icon_url": str(raw.get("panel_author_icon_url") or "")[:500],
            "panel_thumbnail_url": str(raw.get("panel_thumbnail_url") or "")[:500],
            "panel_image_url": str(raw.get("panel_image_url") or "")[:500],
            "panel_image_position": _panel_image_position(raw.get("panel_image_position", "end")),
            "panel_footer": str(raw.get("panel_footer") or "Bella English Learning • escolha uma opção abaixo")[:2048],
            "panel_footer_icon_url": str(raw.get("panel_footer_icon_url") or "")[:500],
            "panel_fields": _normalize_panel_fields(fields),
            "closed_block_title": str(raw.get("closed_block_title", "Sistema pausado no momento"))[:256],
            "closed_block_template": str(raw.get("closed_block_template", "{motivo}"))[:1800],
        }

    async def set_enabled(
        self,
        guild_id: int,
        enabled: bool,
        *,
        reason: str = "",
    ) -> dict[str, Any]:
        current = await self.settings_for(guild_id)
        payload = {
            **current,
            "enabled": enabled,
            "closed_reason": reason.strip()[:500]
            or current.get("closed_reason")
            or "Aprenda Inglês com Bella está em manutenção no momento.",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        await self.state.update_section(
            ENGLISH_SETTINGS_SECTION,
            str(guild_id),
            payload,
        )
        return payload

    async def save_public_panel(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
    ) -> dict[str, Any]:
        current = await self.settings_for(guild_id)
        payload = {
            **current,
            "panel_channel_id": int(channel_id),
            "panel_message_id": int(message_id),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        await self.state.update_section(
            ENGLISH_SETTINGS_SECTION,
            str(guild_id),
            payload,
        )
        return payload

    async def set_panel_render_mode(
        self,
        guild_id: int,
        mode: str,
    ) -> dict[str, Any]:
        current = await self.settings_for(guild_id)
        payload = {
            **current,
            "panel_render_mode": "v2" if str(mode).lower() == "v2" else "v1",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        await self.state.update_section(
            ENGLISH_SETTINGS_SECTION,
            str(guild_id),
            payload,
        )
        return payload

    async def save_panel_payload(
        self,
        guild_id: int,
        *,
        mode: str,
        editor_payload: dict[str, Any],
    ) -> dict[str, Any]:
        current = await self.settings_for(guild_id)
        key = "panel_v2_payload" if str(mode).lower() == "v2" else "panel_v1_payload"
        payload = {
            **current,
            key: dict(editor_payload),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        await self.state.update_section(ENGLISH_SETTINGS_SECTION, str(guild_id), payload)
        return payload

    async def update_closed_block(
        self,
        guild_id: int,
        *,
        title: str,
        template: str,
    ) -> dict[str, Any]:
        current = await self.settings_for(guild_id)
        payload = {
            **current,
            "closed_block_title": title.strip()[:256],
            "closed_block_template": template.strip()[:1800],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        await self.state.update_section(ENGLISH_SETTINGS_SECTION, str(guild_id), payload)
        return payload

    async def update_panel_text(
        self,
        guild_id: int,
        *,
        message_content: str,
        title: str,
        description: str,
        footer: str,
        fields: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        current = await self.settings_for(guild_id)
        payload = {
            **current,
            "message_content": message_content.strip()[:1800],
            "panel_title": title.strip()[:256] or "Aprenda Inglês com Bella",
            "panel_description": description.strip()[:4000] or ENGLISH_PANEL_DEFAULT_DESCRIPTION,
            "panel_footer": footer.strip()[:2048] or "Bella English Learning • escolha uma opção abaixo",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if fields is not None:
            payload["panel_fields"] = _normalize_panel_fields(fields)
        await self.state.update_section(
            ENGLISH_SETTINGS_SECTION,
            str(guild_id),
            payload,
        )
        return payload

    async def update_panel_visual(
        self,
        guild_id: int,
        *,
        color: str,
        author_name: str,
        author_icon_url: str,
        thumbnail_url: str,
        image_url: str,
        footer_icon_url: str,
        image_position: str = "end",
    ) -> dict[str, Any]:
        urls = {
            "panel_author_icon_url": author_icon_url.strip(),
            "panel_thumbnail_url": thumbnail_url.strip(),
            "panel_image_url": image_url.strip(),
            "panel_footer_icon_url": footer_icon_url.strip(),
        }
        for label, value in urls.items():
            if value and not _valid_panel_url(value):
                raise ValueError(f"A URL de {label} precisa começar com http:// ou https://.")
        current = await self.settings_for(guild_id)
        payload = {
            **current,
            "panel_color": _safe_panel_color(color, ENGLISH_COLOR),
            "panel_author_name": author_name.strip()[:256],
            "panel_image_position": _panel_image_position(image_position),
            **{key: value[:500] for key, value in urls.items()},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        await self.state.update_section(
            ENGLISH_SETTINGS_SECTION,
            str(guild_id),
            payload,
        )
        return payload

    async def set_manager_roles(
        self,
        guild_id: int,
        role_ids: list[int],
    ) -> dict[str, Any]:
        current = await self.settings_for(guild_id)
        payload = {
            **current,
            "manager_role_ids": [int(role_id) for role_id in role_ids[:10]],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        await self.state.update_section(
            ENGLISH_SETTINGS_SECTION,
            str(guild_id),
            payload,
        )
        return payload

    async def ensure_available(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Use esse painel dentro de um servidor.",
                ephemeral=True,
            )
            return False
        settings = await self.settings_for(interaction.guild.id)
        if settings.get("enabled", True):
            return True
        reason = str(settings.get("closed_reason") or "Sistema em manutenção.")
        await interaction.response.send_message(
            f"O Aprenda Inglês com Bella está pausado no momento.\nMotivo: {reason}",
            ephemeral=True,
        )
        return False

    async def profile_card(
        self,
        *,
        guild: discord.Guild,
        user: discord.abc.User,
        avatar_bytes: bytes | None,
    ) -> tuple[discord.File, discord.Embed, discord.ui.View]:
        profile = await self.profile(guild.id, user.id)
        rank = await asyncio.to_thread(self.db.rank_position, guild.id, user.id, "xp")
        friends = await asyncio.to_thread(self.db.friends, guild.id, user.id)
        image = self.renderer.profile_image(
            profile,
            display_name=getattr(user, "display_name", user.name),
            avatar_bytes=avatar_bytes,
            rank=rank,
            friends=len(friends),
        )
        file = discord.File(image, filename="bella-english-profile.png")
        embed = english_profile_embed(profile, user, rank)
        embed.set_image(url="attachment://bella-english-profile.png")
        return file, embed, EnglishProfileView(self, guild.id, user.id)

    async def dashboard_card(
        self,
        *,
        guild: discord.Guild,
        user: discord.abc.User,
    ) -> tuple[discord.File, discord.Embed]:
        data = await asyncio.to_thread(self.db.dashboard, guild.id, user.id)
        image = self.renderer.dashboard_image(data)
        file = discord.File(image, filename="bella-english-dashboard.png")
        embed = english_dashboard_embed(data["profile"], user)
        embed.set_image(url="attachment://bella-english-dashboard.png")
        return file, embed

    async def set_objective(
        self,
        guild_id: int,
        user_id: int,
        objective: str,
    ) -> EnglishProfile:
        return await asyncio.to_thread(self.db.set_objective, guild_id, user_id, objective)

    async def generate_quiz(
        self,
        guild_id: int,
        user_id: int,
        category: str,
        *,
        theme: str = "",
    ) -> dict[str, Any]:
        category = _normalize_quiz_category(category)
        profile = await self.profile(guild_id, user_id)
        fallback = random_quiz(category)
        if not self.service.available:
            return fallback
        recent_rows = await asyncio.to_thread(
            self.db.history,
            guild_id,
            user_id,
            limit=12,
        )
        recent_questions = [
            str(row.get("payload", ""))
            for row in recent_rows
            if str(row.get("kind", "")).startswith("quiz")
        ][:6]
        schema = _dynamic_quiz_schema()
        prompt = (
            "Crie UM quiz inédito de inglês para o módulo Aprenda Inglês com Bella.\n"
            "O quiz precisa ensinar algo útil, não repetir perguntas genéricas.\n\n"
            f"Categoria: {category}\n"
            f"Tema extra pedido: {theme or 'nenhum'}\n"
            f"Nível do aluno: {profile.cefr_level}\n"
            f"Objetivo: {OBJECTIVES.get(profile.objective, profile.objective)}\n"
            f"Motivo do aluno: {profile.goal_reason or 'não informado'}\n"
            f"Foco: {profile.learning_focus}\n"
            f"Perguntas recentes para evitar repetir:\n{chr(10).join(recent_questions) or 'Nenhuma.'}\n\n"
            "Regras:\n"
            "- Gere uma pergunta objetiva com 4 alternativas.\n"
            "- A resposta correta deve estar em answer_index, de 0 a 3.\n"
            "- A explicação deve ensinar o motivo da resposta, em português natural.\n"
            "- Respeite a categoria escolhida pelo aluno.\n"
            "- Só use Linux, terminal, Git, código, APIs ou programação quando a categoria for Tech English ou o tema extra pedir isso claramente.\n"
            "- Se a categoria não for Tech English, use situações de estudo, conversa, rotina, escrita, pronúncia ou trabalho comum.\n"
            "- Varie o tipo de pergunta dentro da categoria: tradução, complete a frase, erro comum, pronúncia, vocabulário ou escrita natural.\n"
            "- Não use emojis.\n"
            "- Não use Markdown pesado."
        )
        try:
            data = await self.service.generate_json(
                prompt,
                schema,
                max_output_tokens=750,
                timeout_seconds=8,
                use_cache=False,
            )
            return _normalize_dynamic_quiz(data, category)
        except Exception:
            return fallback

    async def create_learning_thread(
        self,
        interaction: discord.Interaction,
        *,
        send_onboarding_prompt: bool = True,
    ) -> discord.Thread | None:
        if not await self.ensure_available(interaction):
            return None
        if (
            interaction.guild is None
            or not isinstance(interaction.user, discord.Member)
            or not isinstance(interaction.channel, discord.TextChannel)
        ):
            await interaction.response.send_message(
                "Use esse painel dentro de um canal de texto do servidor.",
                ephemeral=True,
            )
            return None
        existing = await asyncio.to_thread(
            self.db.active_thread,
            interaction.guild.id,
            interaction.user.id,
        )
        if existing:
            thread = interaction.guild.get_thread(int(existing["thread_id"]))
            if thread is not None and not thread.archived:
                await interaction.response.send_message(
                    f"Seu espaço de estudos já está aberto em {thread.mention}.",
                    ephemeral=True,
                )
                return thread
        bot_member = interaction.guild.me
        if bot_member is None:
            await interaction.response.send_message(
                "A Bella ainda está iniciando. Tenta de novo em alguns segundos.",
                ephemeral=True,
            )
            return None
        permissions = interaction.channel.permissions_for(bot_member)
        if not permissions.create_private_threads or not permissions.send_messages_in_threads:
            await interaction.response.send_message(
                "Preciso das permissões Criar Threads Privadas e Enviar Mensagens em Threads.",
                ephemeral=True,
            )
            return None
        await interaction.response.defer(ephemeral=True, thinking=True)
        profile = await self.profile(interaction.guild.id, interaction.user.id)
        thread = await interaction.channel.create_thread(
            name=f"ingles-{_slug(interaction.user.display_name)[:45]}-{random.randint(1000, 9999)}",
            type=discord.ChannelType.private_thread,
            invitable=False,
            reason=f"Espaço de estudos de inglês criado por {interaction.user}",
        )
        await thread.add_user(interaction.user)
        await asyncio.to_thread(
            self.db.save_thread,
            guild_id=interaction.guild.id,
            user_id=interaction.user.id,
            thread_id=thread.id,
            channel_id=interaction.channel.id,
        )
        embed = english_thread_welcome_embed(profile, interaction.user)
        await thread.send(
            content=interaction.user.mention,
            embed=embed,
            view=EnglishStudyView(
                self,
                interaction.guild.id,
                interaction.user.id,
            ),
            allowed_mentions=discord.AllowedMentions(
                users=[interaction.user],
                roles=False,
                everyone=False,
            ),
        )
        if send_onboarding_prompt and not profile.onboarding_completed:
            await thread.send(_english_onboarding_prompt(interaction.user))
        await interaction.followup.send(
            f"Criei sua thread de estudos em {thread.mention}.",
            ephemeral=True,
        )
        return thread

    async def start_conversation_thread(
        self,
        interaction: discord.Interaction,
        *,
        theme: str,
    ) -> None:
        thread = await self.create_learning_thread(
            interaction,
            send_onboarding_prompt=False,
        )
        if thread is None or interaction.guild is None:
            return
        profile = await self.profile(interaction.guild.id, interaction.user.id)
        if not profile.onboarding_completed:
            await _send_learning_text(thread, _english_onboarding_prompt(interaction.user))
            return
        text = await self.ai_teacher_response(
            prompt=(
                f"Comece uma prática de conversação em inglês sobre: {theme}.\n"
                "Não dê consultoria sobre o tema. O foco é ensinar inglês.\n"
                "Abra com uma frase curta e natural em português, diga qual frase "
                "o aluno pode usar em inglês para começar, explique rapidamente e "
                "faça uma pergunta em inglês para ele responder.\n"
                "Use no máximo 8 linhas."
            ),
            user_profile=profile,
            mode="conversation:start",
            max_tokens=450,
        )
        await _send_learning_text(thread, text)

    async def _complete_onboarding(
        self,
        message: discord.Message,
        profile: EnglishProfile,
    ) -> str:
        objective = _infer_objective_from_goal(message.content)
        focus = _infer_learning_focus(message.content)
        updated = await asyncio.to_thread(
            self.db.update_learning_profile,
            guild_id=message.guild.id if message.guild else profile.guild_id,
            user_id=message.author.id,
            objective=objective,
            goal_reason=message.content.strip(),
            learning_focus=",".join(focus),
            onboarding_completed=True,
            onboarding_step="done",
        )
        prompt = (
            "O aluno acabou de explicar por que quer aprender inglês. "
            "Transforme isso em uma resposta de professor real, não consultoria genérica.\n\n"
            f"Resposta do aluno:\n{message.content.strip()}\n\n"
            "Faça assim:\n"
            "1. Confirme que entendeu o objetivo e o motivo dele em linguagem natural.\n"
            "2. Diga que você vai treinar escrita, pronúncia e conversa com base nisso.\n"
            "3. Monte um mini plano inicial com 3 frentes, bem curto.\n"
            "4. Ensine uma frase útil em inglês para o objetivo dele.\n"
            "5. Mostre pronúncia aproximada em português brasileiro.\n"
            "6. Peça para ele responder em inglês com 1 ou 2 frases.\n\n"
            "Não use emoji. Não use tom corporativo. Não passe de 12 linhas."
        )
        return await self.ai_teacher_response(
            prompt=prompt,
            user_profile=updated,
            mode="onboarding:complete",
            max_tokens=650,
        )

    async def handle_learning_thread_message(self, message: discord.Message) -> bool:
        if (
            message.guild is None
            or not isinstance(message.channel, discord.Thread)
            or not message.content.strip()
        ):
            return False
        thread_row = await asyncio.to_thread(
            self.db.thread_by_id,
            message.guild.id,
            message.channel.id,
        )
        if not thread_row or int(thread_row["user_id"]) != message.author.id:
            return False
        profile = await self.profile(message.guild.id, message.author.id)
        if not profile.onboarding_completed:
            async with message.channel.typing():
                text = await self._complete_onboarding(message, profile)
            await _send_learning_text(message.channel, text)
            return True
        history: list[str] = []
        try:
            async for item in message.channel.history(limit=8, before=message):
                if item.author.bot and not item.content:
                    continue
                text = item.content.strip()
                if text:
                    history.append(f"{item.author.display_name}: {text[:350]}")
        except Exception:
            history = []
        history.reverse()
        prompt = (
            "Você está dentro de uma thread privada do módulo Aprenda Inglês com Bella.\n"
            "O aluno quer praticar e aprender inglês. Não responda como consultor de carreira, "
            "nem transforme a conversa em conselho genérico se o aluno só estiver tentando praticar.\n\n"
            "Regras da resposta:\n"
            "- Se o aluno escrever em inglês, responda ao conteúdo, corrija suavemente os erros e dê uma versão mais natural.\n"
            "- Se o aluno escrever em português, mostre como ele poderia dizer aquilo em inglês e peça para tentar.\n"
            "- Explique no máximo 2 pontos por vez para não ficar pesado.\n"
            "- Termine com uma pergunta curta em inglês para a conversa continuar.\n"
            "- Use português brasileiro natural, exemplos em inglês e nenhum emoji.\n"
            "- Seja professor/amigo: direto, útil e leve.\n"
            "- Para conversa casual, responda curto; para dúvida de inglês, explique melhor.\n\n"
            f"Histórico recente:\n{chr(10).join(history[-6:]) or 'Sem histórico relevante.'}\n\n"
            f"Mensagem atual do aluno:\n{message.content.strip()}"
        )
        async with message.channel.typing():
            text = await self.ai_teacher_response(
                prompt=prompt,
                user_profile=profile,
                mode="conversation:reply",
                max_tokens=650,
            )
        await asyncio.to_thread(
            self.db.record_activity,
            guild_id=message.guild.id,
            user_id=message.author.id,
            kind="conversation",
            xp=35,
            duration_seconds=180,
            correct=0,
            total=0,
            words=_extract_words(message.content),
            payload={"thread_id": message.channel.id},
        )
        await asyncio.to_thread(
            self.db.touch_thread,
            message.guild.id,
            message.channel.id,
        )
        await _send_learning_text(message.channel, text)
        return True

    async def record_quiz_answer(
        self,
        *,
        guild_id: int,
        user_id: int,
        quiz: dict[str, Any],
        selected_index: int,
    ) -> tuple[bool, EnglishProfile]:
        correct = int(quiz["answer"]) == selected_index
        profile = await asyncio.to_thread(
            self.db.record_activity,
            guild_id=guild_id,
            user_id=user_id,
            kind="quiz",
            xp=90 if correct else 25,
            duration_seconds=180,
            correct=1 if correct else 0,
            total=1,
            words=(str(quiz.get("word", "")),),
            payload={"question": quiz["question"], "selected": selected_index},
        )
        return correct, profile

    async def ai_teacher_response(
        self,
        *,
        prompt: str,
        user_profile: EnglishProfile,
        mode: str,
        max_tokens: int = 700,
    ) -> str:
        fallback = _local_teacher_fallback(prompt, user_profile, mode)
        if not self.service.available:
            return fallback
        system_prompt = (
            "Você é a Bella no módulo Aprenda Inglês com Bella. "
            f"Persona oficial da Bella:\n{BELLA_PERSONA_RUNTIME}\n\n"
            "Você atua como professor particular real de inglês, não como chatbot. "
            "Seu foco é fazer o aluno evoluir em escrita, pronúncia, leitura e conversa. "
            "Nunca responda apenas como consultor de carreira ou conselheiro genérico: "
            "sempre transforme o tema em prática de inglês. "
            "Corrija suavemente, mostre versões naturais, explique pronúncia quando útil "
            "e puxe o aluno para responder em inglês. "
            "Fale em português brasileiro natural, sem tom corporativo e sem emojis."
        )
        full_prompt = (
            f"Perfil do aluno: nível {user_profile.cefr_level}, objetivo "
            f"{OBJECTIVES.get(user_profile.objective, user_profile.objective)}, "
            f"XP {user_profile.xp}, acertos {user_profile.accuracy:.1f}%.\n"
            f"Motivo informado: {user_profile.goal_reason or 'ainda não informado'}\n"
            f"Foco de estudo: {user_profile.learning_focus}\n"
            f"Triagem inicial concluída: {'sim' if user_profile.onboarding_completed else 'não'}\n"
            f"Modo: {mode}\n\n"
            f"Pedido do aluno:\n{prompt}"
        )
        try:
            text = await self.service.generate_text(
                full_prompt,
                max_output_tokens=max_tokens,
                timeout_seconds=8,
                system_prompt=system_prompt,
                use_cache=False,
            )
            return text.strip() or fallback
        except Exception:
            return fallback


class EnglishProfileView(discord.ui.View):
    def __init__(self, manager: EnglishLearningManager, guild_id: int, user_id: int) -> None:
        super().__init__(timeout=180)
        self.manager = manager
        self.guild_id = guild_id
        self.user_id = user_id

    @discord.ui.button(label="Dashboard", style=discord.ButtonStyle.primary)
    async def dashboard(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[Any],
    ) -> None:
        if interaction.guild is None:
            return
        user = interaction.guild.get_member(self.user_id) or interaction.user
        file, embed = await self.manager.dashboard_card(guild=interaction.guild, user=user)
        await interaction.response.send_message(file=file, embed=embed, ephemeral=True)

    @discord.ui.button(label="Plano", style=discord.ButtonStyle.secondary)
    async def plan(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[Any],
    ) -> None:
        profile = await self.manager.profile(self.guild_id, self.user_id)
        await interaction.response.send_message(
            embed=english_plan_embed(profile),
            ephemeral=True,
        )

    @discord.ui.button(label="Continuar", style=discord.ButtonStyle.success)
    async def continue_study(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[Any],
    ) -> None:
        profile = await self.manager.profile(self.guild_id, self.user_id)
        await interaction.response.send_message(
            embed=english_continue_embed(profile),
            view=EnglishStudyView(self.manager, self.guild_id, self.user_id),
            ephemeral=True,
        )


class EnglishLearningPanelView(discord.ui.View):
    def __init__(self, manager: EnglishLearningManager, *, disabled: bool = False) -> None:
        super().__init__(timeout=None)
        self.manager = manager
        if disabled:
            for child in self.children:
                if hasattr(child, "disabled"):
                    child.disabled = True

    @discord.ui.button(
        label="Começar agora",
        style=discord.ButtonStyle.success,
        custom_id=ENGLISH_PANEL_START_CUSTOM_ID,
        row=0,
    )
    async def start(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[Any],
    ) -> None:
        await self.manager.create_learning_thread(interaction)

    @discord.ui.button(
        label="Meu perfil",
        style=discord.ButtonStyle.primary,
        custom_id=ENGLISH_PANEL_PROFILE_CUSTOM_ID,
        row=0,
    )
    async def profile(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[Any],
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Use esse painel dentro de um servidor.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        avatar = await _avatar_bytes(interaction.user)
        file, embed, view = await self.manager.profile_card(
            guild=interaction.guild,
            user=interaction.user,
            avatar_bytes=avatar,
        )
        await interaction.followup.send(
            file=file,
            embed=embed,
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Teste de nível",
        style=discord.ButtonStyle.secondary,
        custom_id=ENGLISH_PANEL_PLACEMENT_CUSTOM_ID,
        row=0,
    )
    async def placement(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[Any],
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Use esse painel dentro de um servidor.",
                ephemeral=True,
            )
            return
        if not await self.manager.ensure_available(interaction):
            return
        view = EnglishPlacementView(
            self.manager,
            interaction.guild.id,
            interaction.user.id,
        )
        await interaction.response.send_message(
            embed=view.embed(),
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Quiz rápido",
        style=discord.ButtonStyle.secondary,
        custom_id=ENGLISH_PANEL_QUIZ_CUSTOM_ID,
        row=1,
    )
    async def quiz(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[Any],
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Use esse painel dentro de um servidor.",
                ephemeral=True,
            )
            return
        if not await self.manager.ensure_available(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        quiz = await self.manager.generate_quiz(
            interaction.guild.id,
            interaction.user.id,
            "tech",
        )
        await interaction.followup.send(
            embed=make_quiz_embed(quiz, "tech"),
            view=EnglishQuizView(
                self.manager,
                interaction.guild.id,
                interaction.user.id,
                quiz,
                category="tech",
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Ranking",
        style=discord.ButtonStyle.secondary,
        custom_id=ENGLISH_PANEL_RANKING_CUSTOM_ID,
        row=1,
    )
    async def ranking(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[Any],
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Use esse painel dentro de um servidor.",
                ephemeral=True,
            )
            return
        rows = await asyncio.to_thread(
            self.manager.db.ranking,
            interaction.guild.id,
            "xp",
            limit=10,
        )
        await interaction.response.send_message(
            embed=english_ranking_embed(rows, "xp"),
            ephemeral=True,
        )


class EnglishStudyView(discord.ui.View):
    def __init__(self, manager: EnglishLearningManager, guild_id: int, user_id: int) -> None:
        super().__init__(timeout=86400)
        self.manager = manager
        self.guild_id = guild_id
        self.user_id = user_id

    async def _ensure_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            "Esse espaço de estudos é de outro aluno. Abre o seu pelo painel de inglês.",
            ephemeral=True,
        )
        return False

    @discord.ui.select(
        placeholder="Escolha o tipo de estudo ou use os botões abaixo",
        row=0,
        options=[
            discord.SelectOption(label="Grammar", value="grammar"),
            discord.SelectOption(label="Vocabulary", value="vocabulary"),
            discord.SelectOption(label="Tech English", value="tech"),
            discord.SelectOption(label="Conversation", value="conversation"),
            discord.SelectOption(label="Writing", value="writing"),
        ],
    )
    async def select_study(
        self,
        interaction: discord.Interaction,
        select: discord.ui.Select[Any],
    ) -> None:
        if not await self._ensure_owner(interaction):
            return
        profile = await self.manager.profile(self.guild_id, self.user_id)
        topic = select.values[0]
        prompt = (
            f"Crie uma mini lição prática de {topic} para o aluno continuar agora. "
            "Inclua explicação curta e 1 exercício simples. "
            "Se fizer sentido, inclua uma frase em inglês e pronúncia aproximada. "
            "Não use emoji."
        )
        text = await self.manager.ai_teacher_response(
            prompt=prompt,
            user_profile=profile,
            mode=f"study:{topic}",
            max_tokens=700,
        )
        await asyncio.to_thread(
            self.manager.db.record_activity,
            guild_id=self.guild_id,
            user_id=self.user_id,
            kind=f"study:{topic}",
            xp=60,
            duration_seconds=300,
            correct=0,
            total=0,
            words=_extract_words(text),
        )
        await interaction.response.send_message(text[:1900])

    @discord.ui.button(label="Perfil", style=discord.ButtonStyle.primary, row=1)
    async def profile_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[Any],
    ) -> None:
        if interaction.guild is None or not await self._ensure_owner(interaction):
            return
        await interaction.response.defer(thinking=True)
        user = interaction.guild.get_member(self.user_id) or interaction.user
        avatar = await _avatar_bytes(user)
        file, embed, view = await self.manager.profile_card(
            guild=interaction.guild,
            user=user,
            avatar_bytes=avatar,
        )
        await interaction.followup.send(file=file, embed=embed, view=view)

    @discord.ui.button(label="Dashboard", style=discord.ButtonStyle.secondary, row=1)
    async def dashboard_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[Any],
    ) -> None:
        if interaction.guild is None or not await self._ensure_owner(interaction):
            return
        await interaction.response.defer(thinking=True)
        user = interaction.guild.get_member(self.user_id) or interaction.user
        file, embed = await self.manager.dashboard_card(
            guild=interaction.guild,
            user=user,
        )
        await interaction.followup.send(file=file, embed=embed)

    @discord.ui.button(label="Plano", style=discord.ButtonStyle.secondary, row=1)
    async def plan_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[Any],
    ) -> None:
        if not await self._ensure_owner(interaction):
            return
        profile = await self.manager.profile(self.guild_id, self.user_id)
        await interaction.response.send_message(embed=english_plan_embed(profile))

    @discord.ui.button(label="Ranking", style=discord.ButtonStyle.secondary, row=1)
    async def ranking_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[Any],
    ) -> None:
        if not await self._ensure_owner(interaction):
            return
        rows = await asyncio.to_thread(
            self.manager.db.ranking,
            self.guild_id,
            "xp",
            limit=10,
        )
        await interaction.response.send_message(embed=english_ranking_embed(rows, "xp"))

    @discord.ui.button(label="Quiz", style=discord.ButtonStyle.success, row=1)
    async def quiz_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[Any],
    ) -> None:
        if not await self._ensure_owner(interaction):
            return
        await interaction.response.send_message(
            embed=english_quiz_picker_embed(),
            view=EnglishQuizCategoryView(
                self.manager,
                self.guild_id,
                self.user_id,
            ),
        )

    @discord.ui.button(label="Teste de nível", style=discord.ButtonStyle.secondary, row=2)
    async def placement_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[Any],
    ) -> None:
        if not await self._ensure_owner(interaction):
            return
        view = EnglishPlacementView(self.manager, self.guild_id, self.user_id)
        await interaction.response.send_message(embed=view.embed(), view=view)


QUIZ_CATEGORY_OPTIONS: tuple[tuple[str, str, str], ...] = (
    (
        "vocabulario",
        "Vocabulário útil",
        "Palavras e frases para usar no dia a dia.",
    ),
    (
        "gramatica",
        "Gramática",
        "Estruturas, tempos verbais e frases corretas.",
    ),
    (
        "conversacao",
        "Conversação",
        "Perguntas e respostas para falar melhor.",
    ),
    (
        "escrita",
        "Escrita",
        "Como escrever frases, mensagens e textos.",
    ),
    (
        "pronuncia",
        "Pronúncia",
        "Sons, leitura em voz alta e pronúncia aproximada.",
    ),
    (
        "tech",
        "Tech English",
        "Inglês para programação, trabalho e documentação.",
    ),
)


class EnglishQuizCategoryView(discord.ui.View):
    def __init__(self, manager: EnglishLearningManager, guild_id: int, user_id: int) -> None:
        super().__init__(timeout=180)
        self.manager = manager
        self.guild_id = guild_id
        self.user_id = user_id

    async def _ensure_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            "Esse quiz é de outro aluno. Abre o seu com `/ingles quiz`.",
            ephemeral=True,
        )
        return False

    @discord.ui.select(
        placeholder="Escolha o que você quer treinar agora",
        options=[
            discord.SelectOption(label=label, value=value, description=description)
            for value, label, description in QUIZ_CATEGORY_OPTIONS
        ],
    )
    async def select_category(
        self,
        interaction: discord.Interaction,
        select: discord.ui.Select[Any],
    ) -> None:
        if not await self._ensure_owner(interaction):
            return
        category = _normalize_quiz_category(select.values[0])
        await interaction.response.defer(thinking=True)
        quiz = await self.manager.generate_quiz(
            self.guild_id,
            self.user_id,
            category,
        )
        await interaction.followup.send(
            embed=make_quiz_embed(quiz, category),
            view=EnglishQuizView(
                self.manager,
                self.guild_id,
                self.user_id,
                quiz,
                category=category,
            ),
        )


class EnglishQuizView(discord.ui.View):
    def __init__(
        self,
        manager: EnglishLearningManager,
        guild_id: int,
        user_id: int,
        quiz: dict[str, Any],
        *,
        category: str | None = None,
    ) -> None:
        super().__init__(timeout=180)
        self.manager = manager
        self.guild_id = guild_id
        self.user_id = user_id
        self.quiz = quiz
        self.category = _normalize_quiz_category(category or str(quiz.get("category", "tech")))
        self.next_button = _NextQuizButton()
        for index, option in enumerate(quiz["options"]):
            self.add_item(_QuizAnswerButton(index, str(option)))
        self.add_item(self.next_button)


class _QuizAnswerButton(discord.ui.Button[Any]):
    def __init__(self, index: int, label: str) -> None:
        super().__init__(label=label[:80], style=discord.ButtonStyle.secondary)
        self.index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, EnglishQuizView):
            return
        if interaction.user.id != view.user_id:
            await interaction.response.send_message(
                "Esse quiz não é seu, rapaz. Usa `/ingles quiz` pra abrir um.",
                ephemeral=True,
            )
            return
        correct, profile = await view.manager.record_quiz_answer(
            guild_id=view.guild_id,
            user_id=view.user_id,
            quiz=view.quiz,
            selected_index=self.index,
        )
        for item in view.children:
            if isinstance(item, _QuizAnswerButton):
                item.disabled = True
            if isinstance(item, _NextQuizButton):
                item.disabled = False
        result = "Boa, acertou." if correct else "Quase. Essa aqui escapou."
        embed = discord.Embed(
            title=result,
            description=(
                f"Resposta certa: **{view.quiz['options'][view.quiz['answer']]}**\n\n"
                f"{view.quiz['explanation']}\n\n"
                f"XP atual: **{profile.xp}**"
            ),
            color=discord.Color.green() if correct else discord.Color.orange(),
        )
        await interaction.response.edit_message(embed=embed, view=view)


class _NextQuizButton(discord.ui.Button[Any]):
    def __init__(self) -> None:
        super().__init__(
            label="Próximo quiz",
            style=discord.ButtonStyle.success,
            row=1,
            disabled=True,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, EnglishQuizView):
            return
        if interaction.user.id != view.user_id:
            await interaction.response.send_message(
                "Esse quiz não é seu. Abre um com `/ingles quiz`.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=True)
        quiz = await view.manager.generate_quiz(
            view.guild_id,
            view.user_id,
            view.category,
        )
        await interaction.edit_original_response(
            embed=make_quiz_embed(quiz, view.category),
            view=EnglishQuizView(
                view.manager,
                view.guild_id,
                view.user_id,
                quiz,
                category=view.category,
            ),
        )


class EnglishPlacementView(discord.ui.View):
    QUESTIONS = [
        ("I ____ a developer.", ["am", "is", "are"], 0),
        ("Yesterday I ____ a bug.", ["fix", "fixed", "fixing"], 1),
        ("A pull request should be ____ before merge.", ["reviewed", "review", "reviewing"], 0),
        ("If I had more time, I ____ refactor this module.", ["will", "would", "was"], 1),
        ("The API has been deprecated, so we need to ____ the integration.", ["migrate", "migration", "migrated"], 0),
    ]

    def __init__(self, manager: EnglishLearningManager, guild_id: int, user_id: int) -> None:
        super().__init__(timeout=300)
        self.manager = manager
        self.guild_id = guild_id
        self.user_id = user_id
        self.index = 0
        self.answers: list[int] = []
        self._rebuild()

    def _rebuild(self) -> None:
        self.clear_items()
        question, options, _answer = self.QUESTIONS[self.index]
        for idx, option in enumerate(options):
            self.add_item(_PlacementButton(idx, option))

    def embed(self) -> discord.Embed:
        question, _options, _answer = self.QUESTIONS[self.index]
        return discord.Embed(
            title=f"Teste de nivelamento • {self.index + 1}/{len(self.QUESTIONS)}",
            description=f"Escolha a alternativa correta:\n\n**{question}**",
            color=discord.Color(ENGLISH_COLOR),
        )


class _PlacementButton(discord.ui.Button[Any]):
    def __init__(self, index: int, label: str) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, EnglishPlacementView):
            return
        if interaction.user.id != view.user_id:
            await interaction.response.send_message(
                "Esse teste é de outro aluno. Abre o seu com `/ingles nivelamento`.",
                ephemeral=True,
            )
            return
        _question, _options, answer = view.QUESTIONS[view.index]
        view.answers.append(2 if self.index == answer else 0)
        view.index += 1
        if view.index >= len(view.QUESTIONS):
            profile = await asyncio.to_thread(
                view.manager.db.record_placement,
                view.guild_id,
                view.user_id,
                view.answers,
            )
            view.clear_items()
            await interaction.response.edit_message(
                embed=english_placement_result_embed(profile),
                view=view,
            )
            return
        view._rebuild()
        await interaction.response.edit_message(embed=view.embed(), view=view)


class EnglishLearningRenderer:
    width = 1500
    height = 850

    def profile_image(
        self,
        profile: EnglishProfile,
        *,
        display_name: str,
        avatar_bytes: bytes | None,
        rank: int | None,
        friends: int,
    ) -> io.BytesIO:
        image, draw = self._canvas()
        self._header(draw, "Aprenda Inglês com Bella", "Perfil premium de evolução")
        self._avatar(image, avatar_bytes, (85, 165, 245, 325))
        draw.text((280, 175), _truncate(display_name, 26), font=_font(48, bold=True), fill=(250, 250, 255))
        draw.text(
            (282, 235),
            f"{profile.title}  •  {profile.cefr_level}  •  Nível {profile.game_level}",
            font=_font(24, bold=True),
            fill=(146, 160, 255),
        )
        self._progress_bar(
            draw,
            (280, 292, 900, 332),
            profile.level_progress,
            XP_PER_LEVEL,
            label=f"{profile.level_progress}/{XP_PER_LEVEL} XP para o próximo nível",
        )
        self._panel(draw, (80, 370, 700, 740), "Estatísticas")
        self._metric_grid(
            draw,
            [
                ("XP total", str(profile.xp)),
                ("Sequência", f"{profile.streak} dia(s)"),
                ("Tempo estudado", _duration(profile.study_seconds)),
                ("Palavras", str(profile.words_learned)),
                ("Exercícios", str(profile.exercises_done)),
                ("Acertos", f"{profile.accuracy:.1f}%"),
            ],
            origin=(115, 425),
        )
        self._panel(draw, (740, 370, 1420, 740), "Jornada")
        rows = [
            ("Objetivo atual", OBJECTIVES.get(profile.objective, profile.objective)),
            ("Próxima lição", profile.current_lesson),
            ("Ranking", f"#{rank}" if rank else "Sem posição"),
            ("Conquistas", str(len(profile.achievements))),
            ("Amigos de estudo", str(friends)),
            ("Última atividade", _format_date(profile.last_activity)),
        ]
        y = 420
        for label, value in rows:
            draw.text((780, y), label, font=_font(18, bold=True), fill=(170, 176, 205))
            draw.text((780, y + 24), _truncate(value, 58), font=_font(21, bold=True), fill=(250, 250, 255))
            y += 52
        self._footer(draw)
        return self._output(image)

    def dashboard_image(self, data: dict[str, Any]) -> io.BytesIO:
        profile: EnglishProfile = data["profile"]
        daily: dict[str, dict[str, int]] = data["daily"]
        image, draw = self._canvas()
        self._header(draw, "English Intelligence Dashboard", "Evolução semanal, mensal e anual")
        days = _recent_days(14)
        xp_values = [int(daily.get(day, {}).get("xp", 0)) for day in days]
        time_values = [int(daily.get(day, {}).get("duration_seconds", 0)) // 60 for day in days]
        self._kpi_row(
            draw,
            [
                ("XP", str(profile.xp)),
                ("Nível", str(profile.game_level)),
                ("Sequência", f"{profile.streak}d"),
                ("Acertos", f"{profile.accuracy:.1f}%"),
                ("Tempo", _duration(profile.study_seconds)),
            ],
        )
        self._panel(draw, (80, 330, 930, 740), "XP ganho nos últimos 14 dias")
        self._line_chart(draw, (125, 395, 890, 690), xp_values, days, (85, 124, 250))
        self._panel(draw, (970, 330, 1420, 740), "Minutos estudados")
        self._bar_chart(draw, (1015, 395, 1375, 690), time_values, days, (65, 211, 146))
        self._footer(draw)
        return self._output(image)

    def _canvas(self) -> tuple[Image.Image, ImageDraw.ImageDraw]:
        image = Image.new("RGB", (self.width, self.height), (3, 3, 8))
        draw = ImageDraw.Draw(image)
        for y in range(self.height):
            shade = int(18 + 24 * y / self.height)
            draw.line((0, y, self.width, y), fill=(3, 4, shade))
        draw.rectangle((0, 0, self.width, 110), fill=(5, 7, 20))
        return image, draw

    def _header(self, draw: ImageDraw.ImageDraw, title: str, subtitle: str) -> None:
        draw.text((75, 42), title, font=_font(42, bold=True), fill=(250, 250, 255))
        draw.text((78, 92), subtitle, font=_font(19), fill=(160, 170, 210))
        draw.rounded_rectangle((1250, 45, 1410, 83), radius=19, fill=(20, 31, 78))
        _center(draw, (1250, 45, 1410, 83), "ENGLISH", _font(16, bold=True), (120, 165, 255))

    def _panel(self, draw: ImageDraw.ImageDraw, bounds: tuple[int, int, int, int], title: str) -> None:
        draw.rounded_rectangle(bounds, radius=28, fill=(12, 14, 29), outline=(42, 50, 94), width=2)
        draw.text((bounds[0] + 28, bounds[1] + 24), title, font=_font(24, bold=True), fill=(245, 247, 255))

    def _avatar(self, image: Image.Image, avatar_bytes: bytes | None, bounds: tuple[int, int, int, int]) -> None:
        left, top, right, bottom = bounds
        size = min(right - left, bottom - top)
        try:
            avatar = Image.open(io.BytesIO(avatar_bytes or b"")).convert("RGB")
            avatar = avatar.resize((size, size), Image.Resampling.LANCZOS)
        except Exception:
            avatar = Image.new("RGB", (size, size), (32, 38, 62))
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
        draw = ImageDraw.Draw(image)
        draw.ellipse((left - 8, top - 8, right + 8, bottom + 8), fill=(86, 126, 255))
        image.paste(avatar, (left, top), mask)

    def _progress_bar(
        self,
        draw: ImageDraw.ImageDraw,
        bounds: tuple[int, int, int, int],
        value: int,
        maximum: int,
        *,
        label: str,
    ) -> None:
        left, top, right, bottom = bounds
        draw.rounded_rectangle(bounds, radius=18, fill=(21, 25, 48))
        ratio = min(1.0, max(0.0, value / max(1, maximum)))
        draw.rounded_rectangle((left, top, left + (right - left) * ratio, bottom), radius=18, fill=(91, 124, 250))
        draw.text((left, bottom + 10), label, font=_font(16), fill=(180, 188, 220))

    def _metric_grid(self, draw: ImageDraw.ImageDraw, rows: list[tuple[str, str]], *, origin: tuple[int, int]) -> None:
        x0, y0 = origin
        for index, (label, value) in enumerate(rows):
            col = index % 2
            row = index // 2
            x = x0 + col * 285
            y = y0 + row * 90
            draw.rounded_rectangle((x, y, x + 250, y + 68), radius=16, fill=(18, 22, 43))
            draw.text((x + 18, y + 12), label, font=_font(15, bold=True), fill=(150, 160, 205))
            draw.text((x + 18, y + 34), value, font=_font(22, bold=True), fill=(248, 250, 255))

    def _kpi_row(self, draw: ImageDraw.ImageDraw, cards: list[tuple[str, str]]) -> None:
        width = 250
        x = 80
        for label, value in cards:
            draw.rounded_rectangle((x, 175, x + width, 270), radius=20, fill=(12, 14, 30), outline=(42, 50, 94))
            draw.text((x + 22, 195), label, font=_font(16, bold=True), fill=(153, 165, 210))
            draw.text((x + 22, 225), value, font=_font(28, bold=True), fill=(250, 250, 255))
            x += width + 35

    def _line_chart(
        self,
        draw: ImageDraw.ImageDraw,
        bounds: tuple[int, int, int, int],
        values: list[int],
        labels: list[str],
        color: tuple[int, int, int],
    ) -> None:
        left, top, right, bottom = bounds
        maximum = max(values or [1]) or 1
        for idx in range(5):
            y = top + idx * (bottom - top) / 4
            draw.line((left, y, right, y), fill=(37, 43, 75), width=1)
        points = []
        for index, value in enumerate(values):
            x = left + index * (right - left) / max(1, len(values) - 1)
            y = bottom - (bottom - top) * value / maximum
            points.append((x, y))
        if len(points) >= 2:
            draw.line(points, fill=color, width=5, joint="curve")
        for x, y in points:
            draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=(245, 247, 255))
        self._axis(draw, labels, left, right, bottom + 10)

    def _bar_chart(
        self,
        draw: ImageDraw.ImageDraw,
        bounds: tuple[int, int, int, int],
        values: list[int],
        labels: list[str],
        color: tuple[int, int, int],
    ) -> None:
        left, top, right, bottom = bounds
        maximum = max(values or [1]) or 1
        slot = (right - left) / max(1, len(values))
        for index, value in enumerate(values):
            height = max(4, (bottom - top) * value / maximum) if value else 4
            x = left + index * slot + slot / 2
            draw.rounded_rectangle((x - slot * 0.25, bottom - height, x + slot * 0.25, bottom), radius=6, fill=color)
        self._axis(draw, labels, left, right, bottom + 10)

    def _axis(self, draw: ImageDraw.ImageDraw, labels: list[str], left: int, right: int, y: int) -> None:
        if not labels:
            return
        for index in _label_indices(len(labels), 5):
            x = left + index * (right - left) / max(1, len(labels) - 1)
            text = labels[index][5:]
            box = draw.textbbox((0, 0), text, font=_font(12))
            draw.text((x - (box[2] - box[0]) / 2, y), text, font=_font(12), fill=(145, 155, 195))

    def _footer(self, draw: ImageDraw.ImageDraw) -> None:
        draw.text((75, 800), "BELLA ENGLISH LEARNING  •  Tech English", font=_font(14, bold=True), fill=(140, 150, 190))
        generated = datetime.now().astimezone().strftime("%d/%m/%Y • %H:%M")
        box = draw.textbbox((0, 0), generated, font=_font(14))
        draw.text((1425 - (box[2] - box[0]), 800), generated, font=_font(14), fill=(140, 150, 190))

    def _output(self, image: Image.Image) -> io.BytesIO:
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        output.seek(0)
        return output


def english_profile_embed(
    profile: EnglishProfile,
    user: discord.abc.User,
    rank: int | None,
) -> discord.Embed:
    embed = discord.Embed(
        title="Aprenda Inglês com Bella",
        description=(
            f"Perfil de {user.mention} com progresso, XP, ranking, conquistas "
            "e plano de estudos."
        ),
        color=discord.Color(ENGLISH_COLOR),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Nível", value=f"{profile.cefr_level} • Lv. {profile.game_level}")
    embed.add_field(name="XP", value=str(profile.xp))
    embed.add_field(name="Ranking", value=f"#{rank}" if rank else "Sem posição")
    embed.add_field(name="Objetivo", value=OBJECTIVES.get(profile.objective, profile.objective), inline=False)
    embed.set_footer(text="Bella English Learning")
    return embed


def _valid_panel_url(value: str) -> bool:
    return bool(re.fullmatch(r"https?://\S{4,500}", str(value or "").strip()))


def _safe_panel_color(value: Any, fallback: int) -> int:
    if isinstance(value, int):
        return max(0, min(0xFFFFFF, value))
    text = str(value or "").strip().removeprefix("#")
    try:
        return max(0, min(0xFFFFFF, int(text, 16)))
    except (TypeError, ValueError):
        return fallback


def _panel_image_position(value: Any) -> str:
    normalized = (
        str(value or "")
        .strip()
        .casefold()
        .replace("í", "i")
        .replace("ó", "o")
        .replace("é", "e")
        .replace("ã", "a")
        .replace("ç", "c")
    )
    if normalized in {"top", "topo", "inicio", "início", "antes"}:
        return "top"
    if normalized in {"middle", "meio", "centro"}:
        return "middle"
    return "end"


def _normalize_panel_fields(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return [dict(item) for item in ENGLISH_PANEL_DEFAULT_FIELDS]
    fields: list[dict[str, Any]] = []
    for item in value[:25]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:256]
        field_value = str(item.get("value") or "").strip()[:1024]
        if not name and not field_value:
            continue
        fields.append(
            {
                "name": name,
                "value": field_value or "-",
                "inline": bool(item.get("inline", False)),
            }
        )
    return fields or [dict(item) for item in ENGLISH_PANEL_DEFAULT_FIELDS]


def parse_panel_field_lines(raw: str) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for line in str(raw or "").splitlines()[:25]:
        clean = line.strip()
        if not clean:
            continue
        parts = [part.strip() for part in clean.split("|", 2)]
        while len(parts) < 3:
            parts.append("")
        name, value, inline = parts
        if name or value:
            fields.append(
                {
                    "name": name[:256],
                    "value": value[:1024] or "-",
                    "inline": inline.casefold() in {"sim", "s", "true", "1", "inline"},
                }
            )
    return fields


def panel_field_lines(fields: list[dict[str, Any]]) -> str:
    return "\n".join(
        "|".join(
            (
                str(item.get("name") or ""),
                str(item.get("value") or "").replace("\n", " "),
                "sim" if item.get("inline") else "não",
            )
        )
        for item in _normalize_panel_fields(fields)
    )


def english_panel_embed(
    settings: dict[str, Any] | None = None,
    *,
    include_image: bool = True,
    include_fields: bool = True,
    include_footer: bool = True,
) -> discord.Embed:
    settings = settings or {}
    enabled = bool(settings.get("enabled", True))
    color = _safe_panel_color(settings.get("panel_color"), ENGLISH_COLOR)
    embed = discord.Embed(
        title=str(settings.get("panel_title") or "Aprenda Inglês com Bella")[:256],
        description=str(settings.get("panel_description") or ENGLISH_PANEL_DEFAULT_DESCRIPTION)[:4096],
        color=discord.Color(color if enabled else 0xF59E0B),
        timestamp=datetime.now(timezone.utc),
    )
    author_name = str(settings.get("panel_author_name") or "").strip()
    author_icon_url = str(settings.get("panel_author_icon_url") or "").strip()
    if author_name:
        kwargs: dict[str, Any] = {"name": author_name[:256]}
        if _valid_panel_url(author_icon_url):
            kwargs["icon_url"] = author_icon_url
        embed.set_author(**kwargs)
    if include_fields:
        for item in _normalize_panel_fields(settings.get("panel_fields") or ENGLISH_PANEL_DEFAULT_FIELDS)[:25]:
            embed.add_field(
                name=str(item.get("name") or "\u200b")[:256],
                value=str(item.get("value") or "-")[:1024],
                inline=bool(item.get("inline", False)),
            )
    thumbnail_url = str(settings.get("panel_thumbnail_url") or "").strip()
    image_url = str(settings.get("panel_image_url") or "").strip()
    if _valid_panel_url(thumbnail_url):
        embed.set_thumbnail(url=thumbnail_url)
    if include_image and _valid_panel_url(image_url):
        embed.set_image(url=image_url)
    footer = str(settings.get("panel_footer") or "Bella English Learning • escolha uma opção abaixo").strip()
    footer_icon_url = str(settings.get("panel_footer_icon_url") or "").strip()
    if include_footer and footer:
        kwargs = {"text": footer[:2048]}
        if _valid_panel_url(footer_icon_url):
            kwargs["icon_url"] = footer_icon_url
        embed.set_footer(**kwargs)
    return embed


def english_panel_embeds(settings: dict[str, Any] | None = None) -> list[discord.Embed]:
    settings = settings or {}
    custom_payload = settings.get("panel_v1_payload")
    if isinstance(custom_payload, dict):
        raw_embeds = custom_payload.get("embeds")
        if isinstance(raw_embeds, list) and raw_embeds:
            result: list[discord.Embed] = []
            for raw in raw_embeds[:10]:
                if not isinstance(raw, dict):
                    continue
                draft = EmbedDraft.from_dict(raw)
                draft.buttons = []
                result.extend(draft.to_embeds())
                if len(result) >= 10:
                    break
            if result:
                return result[:10]
    image_url = str(settings.get("panel_image_url") or "").strip()
    if not _valid_panel_url(image_url):
        return [english_panel_embed(settings, include_image=False)]
    position = _panel_image_position(settings.get("panel_image_position", "end"))
    color = _safe_panel_color(settings.get("panel_color"), ENGLISH_COLOR)
    media = discord.Embed(color=discord.Color(color if bool(settings.get("enabled", True)) else 0xF59E0B))
    media.set_image(url=image_url)
    if position == "top":
        return [media, english_panel_embed(settings, include_image=False)]
    if position == "middle":
        fields = _normalize_panel_fields(settings.get("panel_fields") or ENGLISH_PANEL_DEFAULT_FIELDS)
        if fields:
            return [
                english_panel_embed(settings, include_image=False, include_fields=False, include_footer=False),
                media,
                english_panel_embed(settings, include_image=False, include_fields=True, include_footer=True),
            ]
        return [english_panel_embed(settings, include_image=False), media]
    return [english_panel_embed(settings, include_image=True)]


def english_thread_welcome_embed(
    profile: EnglishProfile,
    user: discord.abc.User,
) -> discord.Embed:
    embed = discord.Embed(
        title="Sua área de estudos foi criada",
        description=(
            f"Boa, {user.mention}. Essa thread é seu espaço privado para estudar "
            "inglês com a Bella.\n\n"
            "Você pode continuar uma lição, fazer quiz, treinar Tech English, "
            "praticar conversação ou pedir correção de frases."
        ),
        color=discord.Color(ENGLISH_COLOR),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Nível atual", value=profile.cefr_level)
    embed.add_field(name="Objetivo", value=OBJECTIVES.get(profile.objective, profile.objective))
    embed.add_field(name="Próxima lição", value=profile.current_lesson, inline=False)
    embed.set_footer(text="Use o menu para estudar ou os botões para consultar perfil, ranking, plano e quiz")
    return embed


def english_dashboard_embed(profile: EnglishProfile, user: discord.abc.User) -> discord.Embed:
    embed = discord.Embed(
        title="Dashboard de Inglês",
        description=f"Evolução visual de {user.mention}.",
        color=discord.Color(ENGLISH_COLOR),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="XP ganho", value=str(profile.xp))
    embed.add_field(name="Tempo estudado", value=_duration(profile.study_seconds))
    embed.add_field(name="Progresso", value=f"{profile.level_progress}/{XP_PER_LEVEL} XP")
    embed.set_footer(text="Bella English Learning")
    return embed


def english_plan_embed(profile: EnglishProfile) -> discord.Embed:
    objective = OBJECTIVES.get(profile.objective, profile.objective)
    embed = discord.Embed(
        title="Plano de Estudos",
        description=f"Plano adaptado para: **{objective}**.",
        color=discord.Color(ENGLISH_COLOR),
    )
    for index, item in enumerate(_plan_steps(profile), start=1):
        embed.add_field(name=f"{index}. {item[0]}", value=item[1], inline=False)
    embed.set_footer(text=f"Próxima lição: {profile.current_lesson}")
    return embed


def english_continue_embed(profile: EnglishProfile) -> discord.Embed:
    embed = discord.Embed(
        title="Continuar Estudos",
        description=(
            f"Boa. Sua próxima etapa é **{profile.current_lesson}**.\n\n"
            "Escolha abaixo como quer estudar agora."
        ),
        color=discord.Color(ENGLISH_COLOR),
    )
    embed.add_field(name="Objetivo", value=OBJECTIVES.get(profile.objective, profile.objective))
    embed.add_field(name="Nível", value=profile.cefr_level)
    embed.add_field(name="Sequência", value=f"{profile.streak} dia(s)")
    return embed


def english_ranking_embed(rows: list[dict[str, Any]], metric: str) -> discord.Embed:
    labels = {
        "xp": "XP",
        "sequencia": "Sequência",
        "exercicios": "Exercícios",
        "palavras": "Palavras aprendidas",
        "tempo": "Tempo estudado",
    }
    embed = discord.Embed(
        title=f"Ranking de Inglês • {labels.get(metric, metric)}",
        color=discord.Color(ENGLISH_COLOR),
        timestamp=datetime.now(timezone.utc),
    )
    medals = ["1º", "2º", "3º"]
    lines = []
    for index, row in enumerate(rows, start=1):
        prefix = medals[index - 1] if index <= 3 else f"{index}º"
        score = int(row.get("score", 0) or 0)
        score_text = _duration(score) if metric == "tempo" else str(score)
        lines.append(f"**{prefix}** <@{row['user_id']}> — {score_text}")
    embed.description = "\n".join(lines) or "Ainda não existe ranking."
    return embed


def english_achievements_embed(profile: EnglishProfile) -> discord.Embed:
    embed = discord.Embed(
        title="Conquistas",
        description="Badges desbloqueadas no módulo de inglês.",
        color=discord.Color(ENGLISH_COLOR),
    )
    if not profile.achievements:
        embed.description = "Nenhuma conquista ainda. Faz um quiz e já resolve isso."
        return embed
    for key in profile.achievements[:20]:
        title, description = ACHIEVEMENTS.get(key, (key, "Conquista desbloqueada."))
        embed.add_field(name=title, value=description, inline=False)
    return embed


def english_history_embed(rows: list[dict[str, Any]]) -> discord.Embed:
    embed = discord.Embed(
        title="Histórico de Estudos",
        color=discord.Color(ENGLISH_COLOR),
        timestamp=datetime.now(timezone.utc),
    )
    if not rows:
        embed.description = "Ainda não há atividades registradas."
        return embed
    for row in rows[:10]:
        embed.add_field(
            name=f"{row['kind']} • {_format_date(row['created_at'])}",
            value=(
                f"XP: **{row['xp']}**\n"
                f"Tempo: **{_duration(int(row['duration_seconds'] or 0))}**\n"
                f"Acertos: **{row['correct']}/{row['total']}**"
            ),
            inline=False,
        )
    return embed


def english_word_embed(kind: str) -> discord.Embed:
    data = {
        "palavra": ("iterate", "repetir e melhorar algo em ciclos", "We need to iterate on this feature."),
        "expressao": ("ship it", "publicar ou entregar algo", "The fix is ready. Let's ship it."),
        "phrasal": ("set up", "configurar, preparar", "I need to set up the project locally."),
        "giria": ("no-brainer", "decisão óbvia", "Using version control is a no-brainer."),
    }.get(kind, ("deploy", "publicar uma aplicação", "We deploy every Friday."))
    embed = discord.Embed(
        title=f"{kind.title()} do dia",
        description=f"**{data[0]}** — {data[1]}",
        color=discord.Color(ENGLISH_COLOR),
    )
    embed.add_field(name="Exemplo", value=data[2], inline=False)
    embed.add_field(name="Contexto de uso", value="Muito comum em tecnologia e trabalho remoto.", inline=False)
    return embed


def english_placement_result_embed(profile: EnglishProfile) -> discord.Embed:
    embed = discord.Embed(
        title="Nivelamento concluído",
        description=(
            f"Seu nível estimado ficou em **{profile.cefr_level}**.\n\n"
            "Use `/ingles plano` para ver o plano recomendado ou `/ingles continuar` "
            "para começar direto."
        ),
        color=discord.Color(ENGLISH_COLOR),
    )
    embed.add_field(name="XP recebido", value="120")
    embed.add_field(name="Próxima etapa", value=profile.current_lesson)
    return embed


def make_quiz_embed(quiz: dict[str, Any], category: str) -> discord.Embed:
    source = "gerado pela Groq" if quiz.get("source") == "groq" else "modo local"
    difficulty = str(quiz.get("difficulty") or "").strip()
    footer = "Escolha uma alternativa abaixo"
    if difficulty:
        footer = f"{footer} • {difficulty}"
    footer = f"{footer} • {source}"
    embed = discord.Embed(
        title=f"Quiz de {_quiz_category_label(category)}",
        description=str(quiz["question"]),
        color=discord.Color(ENGLISH_COLOR),
    )
    embed.set_footer(text=footer)
    return embed


def english_quiz_picker_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Escolha o foco do quiz",
        description=(
            "Antes de começar, escolhe o que você quer treinar agora. "
            "Assim a Bella gera perguntas no assunto certo, em vez de cair direto em Tech English."
        ),
        color=discord.Color(ENGLISH_COLOR),
    )
    embed.add_field(
        name="Opções disponíveis",
        value=(
            "Vocabulário, gramática, conversação, escrita, pronúncia ou Tech English."
        ),
        inline=False,
    )
    embed.set_footer(text="Depois de escolher, o quiz é gerado automaticamente.")
    return embed


def random_quiz(category: str) -> dict[str, Any]:
    category = _normalize_quiz_category(category)
    quiz = dict(random.choice(QUIZ_BANK.get(category) or QUIZ_BANK["vocabulario"]))
    quiz["category"] = category
    quiz["source"] = "local"
    quiz["difficulty"] = "rápido"
    return quiz


def _normalize_quiz_category(category: str) -> str:
    category = str(category or "vocabulario").strip().casefold()
    aliases = {
        "vocabulary": "vocabulario",
        "vocabulário": "vocabulario",
        "grammar": "gramatica",
        "gramática": "gramatica",
        "conversation": "conversacao",
        "conversação": "conversacao",
        "writing": "escrita",
        "pronunciation": "pronuncia",
        "pronúncia": "pronuncia",
    }
    category = aliases.get(category, category)
    allowed = {"vocabulario", "gramatica", "tech", "conversacao", "escrita", "pronuncia"}
    return category if category in allowed else "vocabulario"


def _quiz_category_label(category: str) -> str:
    return {
        "vocabulario": "Vocabulário",
        "gramatica": "Gramática",
        "tech": "Tech English",
        "conversacao": "Conversação",
        "escrita": "Escrita",
        "pronuncia": "Pronúncia",
    }.get(_normalize_quiz_category(category), "Vocabulário")


def _dynamic_quiz_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 4,
                "maxItems": 4,
            },
            "answer_index": {"type": "integer", "minimum": 0, "maximum": 3},
            "explanation": {"type": "string"},
            "word": {"type": "string"},
            "difficulty": {"type": "string"},
            "skill": {"type": "string"},
        },
        "required": ["question", "options", "answer_index", "explanation"],
        "additionalProperties": True,
    }


def _normalize_dynamic_quiz(data: dict[str, Any], category: str) -> dict[str, Any]:
    options = [str(item).strip() for item in data.get("options", []) if str(item).strip()]
    if len(options) != 4 or len({option.casefold() for option in options}) != 4:
        raise ValueError("Quiz dinâmico sem 4 alternativas únicas.")
    answer = int(data.get("answer_index", data.get("answer", -1)))
    if answer < 0 or answer > 3:
        raise ValueError("Índice de resposta inválido.")
    question = _clean_quiz_text(data.get("question"), 520)
    explanation = _clean_quiz_text(data.get("explanation"), 850)
    if len(question) < 8 or len(explanation) < 12:
        raise ValueError("Quiz dinâmico incompleto.")
    indexed_options = list(enumerate(options))
    random.shuffle(indexed_options)
    shuffled_options = [option for _, option in indexed_options]
    shuffled_answer = next(
        index for index, (original_index, _) in enumerate(indexed_options)
        if original_index == answer
    )
    return {
        "question": question,
        "options": shuffled_options,
        "answer": shuffled_answer,
        "explanation": explanation,
        "word": _clean_quiz_text(data.get("word") or data.get("skill") or category, 80),
        "category": _normalize_quiz_category(category),
        "difficulty": _clean_quiz_text(data.get("difficulty") or "adaptativo", 40),
        "source": "groq",
    }


def _clean_quiz_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text[:limit].strip()


def _profile_from_row(row: sqlite3.Row) -> EnglishProfile:
    data = dict(row)
    return EnglishProfile(
        guild_id=int(data["guild_id"]),
        user_id=int(data["user_id"]),
        xp=int(data["xp"] or 0),
        cefr_level=str(data["cefr_level"] or "A1"),
        objective=str(data["objective"] or "documentacao"),
        streak=int(data["streak"] or 0),
        best_streak=int(data["best_streak"] or 0),
        study_seconds=int(data["study_seconds"] or 0),
        words_learned=int(data["words_learned"] or 0),
        exercises_done=int(data["exercises_done"] or 0),
        correct_answers=int(data["correct_answers"] or 0),
        wrong_answers=int(data["wrong_answers"] or 0),
        conversations=int(data["conversations"] or 0),
        started_at=str(data["started_at"]),
        last_activity=data["last_activity"],
        current_lesson=str(data["current_lesson"] or "Primeiros passos"),
        title=str(data["title"] or "Dev em evolução"),
        theme=str(data["theme"] or "midnight"),
        goal_reason=str(data.get("goal_reason") or ""),
        learning_focus=str(data.get("learning_focus") or "escrita,pronuncia,conversacao"),
        onboarding_completed=bool(int(data.get("onboarding_completed") or 0)),
        onboarding_step=str(data.get("onboarding_step") or "goal"),
        achievements=_json_list(data["achievements"]),
    )


def _earned_achievements(
    *,
    kind: str,
    streak: int,
    study_seconds: int,
    words_learned: int,
    exercises_done: int,
    correct: int,
    total: int,
) -> set[str]:
    earned = {"first_exercise"}
    if kind == "conversation":
        earned.add("first_conversation")
    if kind == "quiz":
        earned.add("first_quiz")
    if streak >= 7:
        earned.add("first_week")
    if streak >= 30:
        earned.add("streak_30")
    if streak >= 100:
        earned.add("streak_100")
    if words_learned >= 100:
        earned.add("words_100")
    if words_learned >= 500:
        earned.add("words_500")
    if correct and total and correct == total:
        earned.add("no_mistakes")
    if study_seconds >= 36000:
        earned.add("marathon")
    if kind.startswith("study:tech") or kind == "quiz:tech":
        earned.add("tech_master")
    return earned


def _placement_level(score: int) -> str:
    if score <= 2:
        return "A1"
    if score <= 4:
        return "A2"
    if score <= 6:
        return "B1"
    if score <= 8:
        return "B2"
    return "C1"


def _lesson_for_objective(objective: str) -> str:
    return {
        "vaga-internacional": "Apresentação profissional em entrevistas",
        "trabalho-remoto": "Comunicação assíncrona no trabalho remoto",
        "documentacao": "Leitura de documentação técnica",
        "clientes": "Conversas com clientes e alinhamento de escopo",
        "escrita": "Frases naturais para escrita profissional",
        "zero": "Base essencial do inglês",
    }.get(objective, "Leitura de documentação técnica")


def _plan_steps(profile: EnglishProfile) -> list[tuple[str, str]]:
    objective = profile.objective
    base = [
        ("Nivelamento", f"Nível atual estimado: **{profile.cefr_level}**."),
        ("Vocabulário base", "Aprender termos recorrentes em tecnologia e rotina."),
        ("Revisão inteligente", "Rever palavras e erros frequentes em ciclos curtos."),
    ]
    if objective == "vaga-internacional":
        base += [
            ("Entrevistas", "Treinar respostas sobre experiência, projetos e impacto."),
            ("Tech English", "Git, APIs, system design, code review e trabalho remoto."),
        ]
    elif objective == "documentacao":
        base += [
            ("Leitura técnica", "Praticar README, docs, issues e mensagens de erro."),
            ("Resumo em inglês", "Explicar o que aprendeu com frases curtas."),
        ]
    else:
        base += [
            ("Conversação", "Praticar perguntas e respostas guiadas."),
            ("Escrita", "Corrigir frases e criar alternativas naturais."),
        ]
    return base


def _english_onboarding_prompt(user: discord.abc.User) -> str:
    return (
        f"{user.mention}, antes de começar a aula de verdade, me fala rapidinho:\n\n"
        "Por que você quer aprender inglêsó\n"
        "Onde você quer usar isso: trabalho, programação, entrevistas, viagem, clientes ou outro motivo?\n"
        "E o que mais quer melhorar agora: escrever, pronunciar, conversar ou ler documentação?\n\n"
        "Pode responder em português mesmo. Com isso eu monto seu treino do jeito certo."
    )


async def _send_learning_text(channel: Any, text: str) -> None:
    for part in _split_learning_text(text):
        await channel.send(part)


def _split_learning_text(text: str, limit: int = 1900) -> list[str]:
    text = (text or "").strip()
    if not text:
        return ["Boa. Me manda uma frase em inglês pra eu corrigir com você."]
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for paragraph in re.split(r"\n{2,}", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
            current = ""
        while len(paragraph) > limit:
            cut = paragraph.rfind(" ", 0, limit)
            if cut < 600:
                cut = limit
            parts.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].strip()
        current = paragraph
    if current:
        parts.append(current)
    return parts


def _infer_objective_from_goal(text: str) -> str:
    normalized = _plain(text)
    checks = [
        ("vaga-internacional", ("vaga", "emprego", "job", "interview", "entrevista", "exterior", "internacional")),
        ("trabalho-remoto", ("remoto", "remote", "freela", "freelancer")),
        ("documentacao", ("documentacao", "docs", "readme", "api", "github", "codigo", "programacao")),
        ("clientes", ("cliente", "clientes", "reuniao", "meeting", "call", "suporte")),
        ("escrita", ("escrever", "escrita", "email", "mensagem", "texto", "curriculo", "linkedin")),
        ("zero", ("zero", "iniciante", "comecar", "basico", "aprender do zero")),
    ]
    for objective, keywords in checks:
        if any(keyword in normalized for keyword in keywords):
            return objective
    return "documentacao"


def _infer_learning_focus(text: str) -> list[str]:
    normalized = _plain(text)
    focus: list[str] = []
    checks = [
        ("escrita", ("escrever", "escrita", "texto", "email", "curriculo", "mensagem")),
        ("pronuncia", ("pronuncia", "pronunciar", "sotaque", "falar melhor", "speaking")),
        ("conversacao", ("conversa", "conversar", "falar", "call", "meeting", "entrevista")),
        ("leitura", ("ler", "leitura", "documentacao", "docs", "readme")),
        ("tech english", ("programacao", "codigo", "dev", "api", "github", "linux", "tech")),
    ]
    for label, keywords in checks:
        if any(keyword in normalized for keyword in keywords):
            focus.append(label)
    return focus or ["escrita", "pronuncia", "conversacao"]


def _plain(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.casefold())
    without_accents = "".join(
        char for char in normalized if not unicodedata.combining(char)
    )
    return re.sub(r"\s+", " ", without_accents)


def _local_teacher_fallback(prompt: str, profile: EnglishProfile, mode: str) -> str:
    if "onboarding" in mode:
        return (
            "Boa, agora entendi melhor o caminho.\n\n"
            f"Vou montar seu treino com foco em {profile.learning_focus.replace(',', ', ')} "
            f"e no objetivo de {OBJECTIVES.get(profile.objective, profile.objective).lower()}.\n\n"
            "Pra começar, usa esta frase:\n"
            "`I want to improve my English because I want better opportunities.`\n\n"
            "Pronúncia aproximada:\n"
            "ai uónt tu imprúv mai ínglish bicóz ai uónt béter oportunítis\n\n"
            "Agora adapta essa frase pro seu caso e me manda em inglês."
        )
    if "conversation" in mode:
        return (
            "Boa. Vamos treinar do jeito certo.\n\n"
            "Tenta responder em inglês com uma frase simples:\n"
            "`I am learning English because...`\n\n"
            "Pronúncia aproximada:\n"
            "ai ém lârning ínglish bicóz\n\n"
            "Completa essa frase com seu motivo."
        )
    if mode == "correction":
        return (
            "Boa. Vou te passar uma correção simples:\n\n"
            f"Texto original:\n{prompt}\n\n"
            "Tenta manter frases curtas, usar sujeito claro e conferir tempo verbal. "
            "Se quiser, manda uma frase menor que eu corrijo ponto por ponto."
        )
    if "interview" in mode:
        return (
            "Fechado. Vamos simular uma entrevista.\n\n"
            "Pergunta: Tell me about a project you built and what problem it solved.\n\n"
            "Responde em inglês do jeito que conseguir. Depois eu corrijo e melhoro sua resposta."
        )
    return (
        "Boa. Pra continuar agora, treina esta frase:\n\n"
        "`I am learning English to work better with technology.`\n\n"
        "Agora tenta adaptar ela para o seu objetivo atual."
    )


def _json_list(value: Any) -> list[str]:
    try:
        data = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return [str(item) for item in data] if isinstance(data, list) else []


def _date_only(value: str | None) -> str | None:
    if not value:
        return None
    return str(value)[:10]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_date(value: str | None) -> str:
    if not value:
        return "Nunca"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%d/%m/%Y")
    except ValueError:
        return str(value)[:10]


def _duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}min"
    return f"{minutes}min"


def _recent_days(count: int) -> list[str]:
    today = date.today()
    return [(today - timedelta(days=count - index - 1)).isoformat() for index in range(count)]


def _label_indices(length: int, maximum: int) -> list[int]:
    if length <= maximum:
        return list(range(length))
    return sorted({round(index * (length - 1) / (maximum - 1)) for index in range(maximum)})


def _extract_words(text: str) -> list[str]:
    words = []
    for item in text.replace("`", " ").replace(".", " ").replace(",", " ").split():
        if item.isascii() and item.isalpha() and 3 <= len(item) <= 18:
            words.append(item.lower())
    return words[:8]


def _truncate(value: str, limit: int) -> str:
    value = str(value)
    return value if len(value) <= limit else value[: max(0, limit - 1)] + "…"


def _slug(value: str) -> str:
    cleaned = []
    for char in str(value).casefold():
        if char.isalnum():
            cleaned.append(char)
        elif cleaned and cleaned[-1] != "-":
            cleaned.append("-")
    return "".join(cleaned).strip("-") or "aluno"


async def _avatar_bytes(user: discord.abc.User) -> bytes | None:
    try:
        return await user.display_avatar.replace(size=256).read()
    except (discord.HTTPException, AttributeError, OSError):
        return None


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _center(
    draw: ImageDraw.ImageDraw,
    bounds: tuple[int, int, int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int],
) -> None:
    box = draw.textbbox((0, 0), text, font=font)
    x = bounds[0] + (bounds[2] - bounds[0] - (box[2] - box[0])) / 2
    y = bounds[1] + (bounds[3] - bounds[1] - (box[3] - box[1])) / 2
    draw.text((x, y), text, font=font, fill=fill)
