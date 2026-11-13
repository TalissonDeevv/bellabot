from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from requests import Response, Session
from requests.exceptions import RequestException, Timeout


SQUARE_BASE_URL = "https://api.squarecloud.app/v2"
STATUS_INTERVAL_SECONDS = 5 * 60
AI_ANALYSIS_INTERVAL_SECONDS = 10 * 60
REQUEST_TIMEOUT_SECONDS = 15
RATE_LIMIT_RETRY_SECONDS = 5
MONITOR_LOG_PATH = Path("monitor.log")


@dataclass(frozen=True)
class AppStatus:
    status: str
    app_name: str
    raw: dict[str, Any]


class SquareCloudClient:
    def __init__(
        self,
        api_key: str,
        app_id: str,
        *,
        session: Session | None = None,
    ) -> None:
        self.api_key = api_key
        self.app_id = app_id
        self.session = session or requests.Session()

    def get_status(self) -> AppStatus:
        data = self._get_json(f"/apps/{self.app_id}/status")
        payload = _extract_payload(data)
        status = str(
            payload.get("status")
            or payload.get("state")
            or payload.get("currentStatus")
            or "unknown"
        ).lower()
        app_name = str(
            payload.get("name")
            or payload.get("app_name")
            or payload.get("appName")
            or self.app_id
        )
        return AppStatus(status=status, app_name=app_name, raw=data)

    def get_logs(self, *, lines: int = 50) -> str:
        data = self._get_json(f"/apps/{self.app_id}/logs")
        payload = _extract_payload(data)
        logs = payload.get("logs") or payload.get("log") or payload.get("content") or data
        if isinstance(logs, list):
            selected = [str(item) for item in logs[-lines:]]
            return "\n".join(selected)
        text = str(logs)
        return "\n".join(text.splitlines()[-lines:])

    def analyze_logs(self, logs: str) -> str:
        if not logs.strip():
            return "Sem logs recentes suficientes para análise."
        prompt = (
            "Analise estes logs da aplicação. Identifique erros críticos (5xx, crashes), "
            "resuma o comportamento recente e diga se a app está saudável ou precisa de atenção."
        )
        body = {
            "model": "cubic",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Você é um analista operacional. Seja direto, cite riscos, "
                        "severidade e próximas ações. Não exponha segredos."
                    ),
                },
                {"role": "user", "content": f"{prompt}\n\nLogs:\n{logs[-12000:]}"},
            ],
            "temperature": 0.2,
            "max_tokens": 450,
        }
        data = self._post_json("/ai/chat/completions", body)
        return _extract_ai_text(data)

    def _get_json(self, path: str) -> dict[str, Any]:
        response = self._request("GET", path)
        return _decode_json(response)

    def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self._request("POST", path, json=body)
        return _decode_json(response)

    def _request(self, method: str, path: str, **kwargs: Any) -> Response:
        url = f"{SQUARE_BASE_URL}{path}"
        headers = {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
        }
        for attempt in range(2):
            response = self.session.request(
                method,
                url,
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
                **kwargs,
            )
            if response.status_code == 429 and attempt == 0:
                time.sleep(RATE_LIMIT_RETRY_SECONDS)
                continue
            if response.status_code != 200:
                raise RuntimeError(
                    f"Square Cloud retornou HTTP {response.status_code}: {response.text[:500]}"
                )
            return response
        raise RuntimeError("Square Cloud retornou rate limit após nova tentativa.")


def run_monitor(client: SquareCloudClient, *, mode: str) -> None:
    last_ai_analysis_at = 0.0
    print("Monitor Square Cloud iniciado. Pressione Ctrl+C para parar.")
    while True:
        started_at = time.time()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            status = client.get_status()
            _write_monitor_log(timestamp, status.status, status.app_name)
            print(f"[{timestamp}] STATUS: {status.status.upper()} | {status.app_name}")
            if status.status in {"stopped", "error"}:
                print(f"ALERTA: aplicação em estado {status.status.upper()}.")
                # TODO: enviar webhook para Discord ou Telegram quando configurado.
            should_analyze = (
                mode in {"ai", "both"}
                and status.status == "running"
                and time.time() - last_ai_analysis_at >= AI_ANALYSIS_INTERVAL_SECONDS
            )
            if should_analyze:
                logs = client.get_logs(lines=50)
                summary = client.analyze_logs(logs)
                last_ai_analysis_at = time.time()
                print("Resumo da IA:")
                print(summary)
        except KeyboardInterrupt:
            raise
        except Timeout:
            print(f"[{timestamp}] ERRO: timeout ao consultar Square Cloud.")
        except RequestException as exc:
            print(f"[{timestamp}] ERRO DE REDE: {exc}")
        except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
            print(f"[{timestamp}] ERRO: {exc}")

        interval = AI_ANALYSIS_INTERVAL_SECONDS if mode == "ai" else STATUS_INTERVAL_SECONDS
        elapsed = time.time() - started_at
        time.sleep(max(1.0, interval - elapsed))


def _extract_payload(data: dict[str, Any]) -> dict[str, Any]:
    payload = data.get("response") or data.get("data") or data.get("result") or data
    return payload if isinstance(payload, dict) else data


def _extract_ai_text(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict) and message.get("content"):
            return str(message["content"]).strip()
        if isinstance(choices[0], dict) and choices[0].get("text"):
            return str(choices[0]["text"]).strip()
    payload = _extract_payload(data)
    for key in ("content", "text", "summary", "message"):
        value = payload.get(key)
        if value:
            return str(value).strip()
    return json.dumps(data, ensure_ascii=False)[:1200]


def _decode_json(response: Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise ValueError(f"Resposta JSON inválida: {response.text[:500]}") from exc
    if not isinstance(data, dict):
        raise ValueError("A API retornou uma estrutura JSON inesperada.")
    return data


def _write_monitor_log(timestamp: str, status: str, app_name: str) -> None:
    with MONITOR_LOG_PATH.open("a", encoding="utf-8") as file:
        file.write(f"[{timestamp}] {status.upper()} | {app_name}\n")


def _load_client() -> SquareCloudClient:
    load_dotenv(encoding="utf-8-sig")
    api_key = os.getenv("SQ_API_KEY", "").strip()
    app_id = os.getenv("SQ_APP_ID", "Bella Bot").strip() or "Bella Bot"
    if not api_key:
        raise RuntimeError("Configure SQ_API_KEY no .env antes de iniciar o monitor.")
    return SquareCloudClient(api_key, app_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor Square Cloud da Bella.")
    parser.add_argument(
        "--mode",
        choices=("status", "ai", "both"),
        default="both",
        help="status: só saúde; ai: status + análise a cada 10 minutos; both: padrão.",
    )
    args = parser.parse_args()
    client = _load_client()
    try:
        run_monitor(client, mode=args.mode)
    except KeyboardInterrupt:
        print("\nMonitor encerrado.")


if __name__ == "__main__":
    main()
