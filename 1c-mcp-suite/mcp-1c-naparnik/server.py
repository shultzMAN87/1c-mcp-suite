"""
MCP-сервер: 1С:Напарник (code.1c.ai)
======================================
Прокси к API 1С:Напарник для:
  - генерации описания (документирующего комментария) процедуры/функции
  - ревью и исправления кода
  - добавления кода по описанию
  - объяснения кода
  - проверки кода на ошибки и производительность
  - произвольных вопросов по 1С

Требуется токен API (получить на code.1c.ai → Профиль → API токен).
Нужна активная подписка ИТС.

API endpoints (реверс из mini-ai-1c / naparnik_client.rs):
  POST /chat_api/v1/conversations                          — создание диалога
  POST /chat_api/v1/conversations/{conv_id}/messages        — отправка сообщения (SSE)
"""

import os
import json
import httpx
import logging

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("1C Naparnik")
logger = logging.getLogger(__name__)

ONEC_AI_TOKEN = os.environ.get("ONEC_AI_TOKEN", "")
ONEC_AI_BASE_URL = os.environ.get("ONEC_AI_BASE_URL", "https://code.1c.ai")
ONEC_AI_TIMEOUT = int(os.environ.get("ONEC_AI_TIMEOUT", "120"))


class OneCNaparnikClient:
    """HTTP-клиент к chat_api 1С:Напарник (реверс из mini-ai-1c)."""

    def __init__(self, token: str, base_url: str = "https://code.1c.ai"):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.conversation_id: str | None = None
        self.last_message_uuid: str | None = None

    def _headers(self) -> dict:
        """Заголовки запроса (аналог build_headers из naparnik_client.rs)."""
        return {
            "Authorization": self.token,
            "Content-Type": "application/json; charset=utf-8",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/chat//",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }

    async def create_conversation(self, skill_name: str = "raw") -> str:
        """
        Создаёт новый диалог (POST /chat_api/v1/conversations).
        Возвращает conversation_id (uuid).
        """
        url = f"{self.base_url}/chat_api/v1/conversations"
        payload = {
            "ui_language": "ru",
            "programming_language": "1C (BSL)",
            "script_language": "ru",
            "skill_name": skill_name,
            "is_chat": True,
        }

        async with httpx.AsyncClient(timeout=ONEC_AI_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=self._headers())
            if resp.status_code != 200:
                raise Exception(f"Ошибка создания диалога: HTTP {resp.status_code}: {resp.text[:500]}")
            data = resp.json()
            self.conversation_id = data["uuid"]
            self.last_message_uuid = data.get("root_message_uuid")
            return self.conversation_id

    async def send_message(self, message: str) -> str:
        """
        Отправляет сообщение в диалог Напарника и собирает SSE-ответ.
        POST /chat_api/v1/conversations/{conv_id}/messages
        """
        if not self.token:
            return json.dumps({"error": "Токен ONEC_AI_TOKEN не задан. Получите его на code.1c.ai"})

        try:
            if not self.conversation_id:
                await self.create_conversation()

            url = f"{self.base_url}/chat_api/v1/conversations/{self.conversation_id}/messages"
            payload = {
                "role": "user",
                "content": {
                    "content": {
                        "instruction": message,
                    },
                },
                "parent_uuid": self.last_message_uuid,
            }

            async with httpx.AsyncClient(timeout=ONEC_AI_TIMEOUT) as client:
                resp = await client.post(url, json=payload, headers=self._headers())

                if resp.status_code == 401:
                    return json.dumps({"error": "Невалидный токен. Проверьте ONEC_AI_TOKEN."})
                if resp.status_code == 403:
                    return json.dumps({"error": "Доступ запрещён. Проверьте подписку ИТС."})
                if resp.status_code != 200:
                    return json.dumps({"error": f"HTTP {resp.status_code}: {resp.text[:500]}"})

                return self._parse_sse_response(resp.text)

        except httpx.TimeoutException:
            return json.dumps({"error": f"Таймаут ({ONEC_AI_TIMEOUT} сек)"})
        except Exception as e:
            return json.dumps({"error": f"Ошибка: {e}"})

    async def send_message_new_conversation(self, message: str, skill_name: str = "raw") -> str:
        """
        Создаёт новый диалог и отправляет сообщение.
        Используется для одноразовых запросов (ревью, объясни и т.д.).
        """
        if not self.token:
            return json.dumps({"error": "Токен ONEC_AI_TOKEN не задан. Получите его на code.1c.ai"})

        try:
            await self.create_conversation(skill_name)
            return await self.send_message(message)
        except Exception as e:
            return json.dumps({"error": f"Ошибка: {e}"})

    def _parse_sse_response(self, text: str) -> str:
        """Парсит SSE-ответ и собирает текст (формат из naparnik_client.rs)."""
        parts = []
        assistant_uuid = None

        for line in text.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)

                # Запоминаем uuid ассистента для цепочки сообщений
                if chunk.get("role") == "assistant" and chunk.get("uuid"):
                    assistant_uuid = chunk["uuid"]

                # Собираем контент из content_delta
                delta = chunk.get("content_delta")
                if delta and isinstance(delta, dict):
                    content = delta.get("content", "")
                    if content:
                        parts.append(content)

                # Или из content (финальный блок)
                content = chunk.get("content")
                if content and isinstance(content, dict):
                    inner = content.get("content", "")
                    if isinstance(inner, str) and inner:
                        parts.append(inner)

            except json.JSONDecodeError:
                continue

        # Обновляем last_message_uuid для цепочки
        if assistant_uuid:
            self.last_message_uuid = assistant_uuid

        result = "".join(parts).strip()
        return result if result else "(пустой ответ от Напарника)"


naparnik = OneCNaparnikClient(ONEC_AI_TOKEN, ONEC_AI_BASE_URL)


# ─── Сценарий 1: Генерация описания (документирующего комментария) ──────────

@mcp.tool()
async def naparnik_generate_comment(code: str, context: str = "") -> str:
    """
    Сгенерировать документирующий комментарий для процедуры/функции 1С.
    Аналог «Сгенерируй документирующий комментарий» (Alt+I,G) в EDT.

    Параметры:
      code    — текст процедуры/функции на языке 1С (BSL)
      context — контекст модуля (например, "Модуль менеджера справочника Номенклатура")
    """
    ctx = f"Контекст: {context}\n\n" if context else ""
    prompt = (
        f"{ctx}Сгенерируй документирующий комментарий для следующей процедуры/функции 1С. "
        "Комментарий должен соответствовать стандартам 1С: "
        "описание назначения, параметры с типами, возвращаемое значение (если функция). "
        "Формат: строки комментария начинаются с // перед определением процедуры/функции.\n\n"
        f"```bsl\n{code}\n```"
    )
    return await naparnik.send_message_new_conversation(prompt)


# ─── Сценарий 2: Ревью и Исправление кода ──────────────────────────────────

@mcp.tool()
async def naparnik_review(code: str, context: str = "") -> str:
    """
    Провести ревью кода 1С через 1С:Напарник.
    Аналог «Ревью» (Alt+I,R) в EDT.

    Параметры:
      code    — текст кода на языке 1С (BSL)
      context — контекст модуля
    """
    ctx = f"Контекст: {context}\n\n" if context else ""
    prompt = (
        f"{ctx}Проведи code review следующего кода 1С. "
        "Оцени: 1) корректность логики, 2) производительность, "
        "3) соответствие стандартам разработки 1С, 4) обработку ошибок, "
        "5) именование переменных. Предложи конкретные улучшения.\n\n"
        f"```bsl\n{code}\n```"
    )
    return await naparnik.send_message_new_conversation(prompt)


@mcp.tool()
async def naparnik_fix(code: str, error_description: str = "", context: str = "") -> str:
    """
    Исправить код 1С через 1С:Напарник.
    Аналог «Исправь» (Alt+I,C) в EDT.

    Параметры:
      code              — текст кода с ошибкой на языке 1С (BSL)
      error_description — описание ошибки или проблемы (если известно)
      context           — контекст модуля
    """
    ctx = f"Контекст: {context}\n\n" if context else ""
    err = f"Описание проблемы: {error_description}\n\n" if error_description else ""
    prompt = (
        f"{ctx}{err}Исправь следующий код 1С. "
        "Найди ошибки (синтаксис, логика, производительность) и предложи "
        "исправленный вариант кода. Объясни, что было не так.\n\n"
        f"```bsl\n{code}\n```"
    )
    return await naparnik.send_message_new_conversation(prompt)


# ─── Сценарий 3: Добавление кода по описанию ───────────────────────────────

@mcp.tool()
async def naparnik_add_code(description: str, context: str = "", existing_code: str = "") -> str:
    """
    Сгенерировать код 1С по описанию задачи через 1С:Напарник.
    Аналог «Добавь код» (Alt+I,A) в EDT.

    Параметры:
      description   — описание того, что нужно реализовать
      context       — контекст модуля (например, "Модуль формы документа РеализацияТоваров")
      existing_code — существующий код модуля (для контекста)
    """
    ctx = f"Контекст: {context}\n\n" if context else ""
    existing = f"Существующий код модуля:\n```bsl\n{existing_code}\n```\n\n" if existing_code else ""
    prompt = (
        f"{ctx}{existing}Напиши код на языке 1С (BSL) для следующей задачи:\n"
        f"{description}\n\n"
        "Код должен соответствовать стандартам разработки 1С, "
        "содержать комментарии и обработку ошибок."
    )
    return await naparnik.send_message_new_conversation(prompt)


# ─── Объяснение кода ───────────────────────────────────────────────────────

@mcp.tool()
async def naparnik_explain(code: str, context: str = "") -> str:
    """
    Объяснить код 1С через 1С:Напарник.
    Аналог «Объясни» (Alt+I,E) в EDT.

    Параметры:
      code    — текст кода на языке 1С (BSL)
      context — контекст модуля
    """
    ctx = f"Контекст: {context}\n\n" if context else ""
    prompt = (
        f"{ctx}Подробно объясни, что делает следующий код 1С. "
        "Опиши логику работы, назначение переменных, "
        "используемые механизмы платформы.\n\n"
        f"```bsl\n{code}\n```"
    )
    return await naparnik.send_message_new_conversation(prompt)


# ─── Проверка кода ─────────────────────────────────────────────────────────

@mcp.tool()
async def naparnik_check_code(code: str) -> str:
    """
    Проверить код 1С через 1С:Напарник на ошибки.
    Проверяет синтаксис, логику, производительность и соответствие стандартам.

    Параметр code — текст кода на языке 1С (BSL).
    """
    prompt = (
        "Проверь следующий код 1С на ошибки. "
        "Проверь синтаксис, логику, возможные проблемы производительности, "
        "соответствие стандартам разработки 1С. "
        "Укажи конкретные строки и предложи исправления.\n\n"
        f"```bsl\n{code}\n```"
    )
    return await naparnik.send_message_new_conversation(prompt)


# ─── Объяснение элемента платформы ─────────────────────────────────────────

@mcp.tool()
async def naparnik_explain_syntax(element: str) -> str:
    """
    Объяснить синтаксис или элемент платформы 1С через 1С:Напарник.

    Параметр element — название элемента (например, "Запрос", "ТаблицаЗначений",
    "РегистрСведений", "ОбщийМодуль").
    """
    prompt = (
        f"Подробно объясни элемент платформы 1С: {element}. "
        "Опиши назначение, синтаксис использования, основные методы и свойства, "
        "приведи примеры кода."
    )
    return await naparnik.send_message_new_conversation(prompt)


# ─── Произвольный вопрос ───────────────────────────────────────────────────

@mcp.tool()
async def naparnik_ask(question: str) -> str:
    """
    Произвольный вопрос к 1С:Напарник.

    Параметр question — вопрос по платформе 1С, разработке, стандартам и т.д.
    """
    return await naparnik.send_message_new_conversation(question)


# ─── Проверка соединения ──────────────────────────────────────────────────

@mcp.tool()
async def naparnik_check_connection() -> str:
    """
    Проверить подключение к 1С:Напарник.
    Создаёт диалог и возвращает результат.
    """
    if not ONEC_AI_TOKEN:
        return json.dumps({
            "status": "error",
            "message": "Токен ONEC_AI_TOKEN не задан. Получите его на code.1c.ai → Профиль → API токен"
        })

    try:
        conv_id = await naparnik.create_conversation()
        return json.dumps({
            "status": "ok",
            "conversation_id": conv_id,
            "message": "Подключение к 1С:Напарник успешно."
        })
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": str(e)
        })


if __name__ == "__main__":
    if not ONEC_AI_TOKEN:
        print("⚠ ONEC_AI_TOKEN не задан. Установите переменную окружения.")
        print("  Получить токен: code.1c.ai → Профиль → API токен")
    else:
        print(f"✓ Токен 1С:Напарник задан ({ONEC_AI_TOKEN[:8]}...)")
    print("\nДоступные инструменты:")
    print("  • naparnik_generate_comment — Сценарий 1: Генерация описания процедуры/функции")
    print("  • naparnik_review           — Сценарий 2: Ревью кода")
    print("  • naparnik_fix              — Сценарий 2: Исправление кода")
    print("  • naparnik_add_code         — Сценарий 3: Добавление кода по описанию")
    print("  • naparnik_explain          — Объяснение кода")
    print("  • naparnik_check_code       — Проверка кода на ошибки")
    print("  • naparnik_explain_syntax   — Объяснение элемента платформы")
    print("  • naparnik_ask              — Произвольный вопрос")
    print("  • naparnik_check_connection — Проверка подключения")
