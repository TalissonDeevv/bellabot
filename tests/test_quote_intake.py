from __future__ import annotations

import unittest

from website_guard.quote_intake import (
    BOT_BLOCKS,
    GENERAL_BLOCKS,
    IntakeState,
    QuoteIntake,
    is_bare_answer,
    is_quote_category,
)


class ScriptedAI:
    """Responde 'ok' para todas as perguntas pendentes com o texto do cliente."""

    def __init__(self, vague_ids: set[str] | None = None) -> None:
        self.vague_ids = set(vague_ids or ())
        self.calls = 0

    async def generate_json(self, prompt, schema, **kwargs):
        self.calls += 1
        if "RESUMO ADMINISTRATIVO" in prompt:
            return {"titulo": "Bot de Discord", "resumo": "Cliente quer um bot.", "escopo": "x", "orcamento_prazo": "y"}
        ids = [line.split("id=")[1].split(":")[0] for line in prompt.splitlines() if line.startswith("- id=")]
        message = prompt.split("MENSAGEM DO CLIENTE: ")[1].split("\n")[0]
        answers = []
        for qid in ids:
            if qid in self.vague_ids:
                answers.append({"id": qid, "status": "vague", "value": "várias coisas"})
                self.vague_ids.discard(qid)
            else:
                options = ["Bot (Discord, Telegram, WhatsApp)"] if qid == "tipo_servico" and "bot" in message else []
                answers.append({"id": qid, "status": "ok", "value": message, "options": options})
        return {"answers": answers, "client_question": "", "follow_up": ""}


class QuoteIntakeTests(unittest.IsolatedAsyncioTestCase):
    def test_category_detection_and_bare_answers(self) -> None:
        self.assertTrue(is_quote_category("Solicitar Orçamento"))
        self.assertTrue(is_quote_category("orcamento"))
        self.assertFalse(is_quote_category("Suporte"))
        self.assertTrue(is_bare_answer("Sim"))
        self.assertTrue(is_bare_answer("não."))
        self.assertFalse(is_bare_answer("sim, tenho servidor na Contabo"))

    async def test_bare_yes_is_rejected_without_ai(self) -> None:
        ai = ScriptedAI()
        intake = QuoteIntake(ai)
        state = IntakeState()
        result = await intake.handle("sim", state)
        self.assertIn("com suas palavras", result.reply)
        self.assertEqual(ai.calls, 0)
        self.assertEqual(state.block_index, 0)

    async def test_general_flow_finishes_and_summarizes(self) -> None:
        intake = QuoteIntake(ScriptedAI())
        state = IntakeState()
        for _ in GENERAL_BLOCKS:
            result = await intake.handle("um sistema web de agendamento para clínica", state)
        self.assertTrue(result.finished)
        self.assertFalse(state.discord_bot)
        summary = await intake.summary(state, "Fulano")
        self.assertEqual(summary["origem"], "ia")
        self.assertIn("agendamento", intake.answers_document(state, "Fulano"))

    async def test_discord_bot_adds_bot_form(self) -> None:
        intake = QuoteIntake(ScriptedAI())
        state = IntakeState()
        await intake.handle("quero um bot pro meu servidor do discord", state)
        self.assertTrue(state.discord_bot)
        self.assertEqual(len(state.blocks()), len(GENERAL_BLOCKS) + len(BOT_BLOCKS))

    async def test_vague_answer_is_asked_again(self) -> None:
        intake = QuoteIntake(ScriptedAI(vague_ids={"descricao"}))
        state = IntakeState(block_index=1)
        result = await intake.handle("é um projeto legal", state)
        self.assertEqual(state.block_index, 1)
        self.assertIn("Descreva o projeto", result.reply)
        await intake.handle("um app que agenda consultas e manda lembrete no WhatsApp", state)
        self.assertEqual(state.block_index, 2)

    async def test_ai_down_keeps_raw_answer_and_moves_on(self) -> None:
        class Down:
            async def generate_json(self, *args, **kwargs):
                raise RuntimeError("fora do ar")

        intake = QuoteIntake(Down())
        state = IntakeState()
        await intake.handle("Fulano, quero um site institucional novo do zero", state)
        self.assertEqual(state.block_index, 1)
        self.assertTrue(state.answers["nome"]["raw"])
        summary = await intake.summary(state, "Fulano")
        self.assertEqual(summary["origem"], "contingencia")

    def test_state_round_trip(self) -> None:
        state = IntakeState(block_index=3, discord_bot=True, answers={"nome": {"status": "ok", "value": "Ana"}})
        again = IntakeState.from_dict(state.to_dict())
        self.assertEqual(again.block_index, 3)
        self.assertTrue(again.discord_bot)


if __name__ == "__main__":
    unittest.main()
