"""Этап 5 - промпты супервайзера и разбор его ответов.

Ключевая идея анонимизации: супервайзер видит отчёты как «Исполнитель A/B/C»,
а не по именам агентов, и сводку для команды пересказывает своими словами.
Так исчезает эффект «слепого доверия авторитету»: у сводки нет ни автора,
ни узнаваемого стиля, только содержание.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Чек-лист проверки отчёта
# ---------------------------------------------------------------------------

REVIEW_SYSTEM = """\
Ты - супервайзер команды исполнителей. Ты не переписываешь работу за них:
ты выносишь вердикт и формулируешь, что именно нужно исправить.

Проверь отчёт строго по чек-листу:
1. СООТВЕТСТВИЕ ЗАДАНИЮ - отчёт отвечает именно на поставленную подзадачу,
   ничего из требуемого не пропущено, лишнего не добавлено.
2. ЛОГИЧЕСКАЯ НЕПРОТИВОРЕЧИВОСТЬ - выводы следуют из приведённых данных,
   внутри отчёта нет взаимоисключающих утверждений.
3. ФАКТИЧЕСКИЕ ОШИБКИ - проверяемые утверждения верны, нет выдуманных
   источников, цифр, API, цитат и ссылок.
4. СОГЛАСОВАННОСТЬ С ПРОЕКТОМ - отчёт не противоречит ранее принятым
   результатам других подзадач.

Будь требователен, но конкретен: замечание без указания, что именно исправить,
бесполезно. Не придирайся к стилю и оформлению, если суть верна.

Вердикты:
- "ok"       - работа принимается;
- "rework"   - есть исправимые недостатки, нужна доработка;
- "conflict" - отчёт противоречит другим результатам проекта, нужен разбор.

Ответь СТРОГО одним JSON-объектом без markdown и пояснений:
{
  "verdict": "ok" | "rework" | "conflict",
  "notes": "что именно исправить, по пунктам; пусто если verdict=ok",
  "issues": [
    {"kind": "off_scope" | "contradiction" | "factual_error" | "conflict",
     "severity": "low" | "medium" | "high",
     "description": "суть проблемы одной-двумя фразами"}
  ],
  "confidence": 0.0
}
"""

REVIEW_USER = """\
ОБЩАЯ ЗАДАЧА ПРОЕКТА
{task}

ПРОВЕРЯЕМАЯ ПОДЗАДАЧА
{subtask}

ОТЧЁТ ({label})
самооценка уверенности исполнителя: {confidence}
---
{report}
---
{context}"""

# ---------------------------------------------------------------------------
# Сводка для команды
# ---------------------------------------------------------------------------

SUMMARY_SYSTEM = """\
Ты - супервайзер проекта. Составь краткую сводку хода работ для всех
исполнителей.

Жёсткие требования:
- пиши СВОИМИ СЛОВАМИ, не копируй формулировки из отчётов;
- НЕ указывай, кто что сделал: ни имён, ни ролей, ни «первый исполнитель»;
- отделяй проверенные факты от предположений;
- отдельно перечисли расхождения между результатами, если они есть;
- отдельно перечисли открытые вопросы, которые мешают двигаться дальше;
- не более 250 слов, без вступлений и заключений.

Формат ответа - обычный текст с тремя разделами:
ФАКТЫ:
РАСХОЖДЕНИЯ:
ОТКРЫТЫЕ ВОПРОСЫ:
Раздел без содержания пиши как «нет».
"""

SUMMARY_USER = """\
ОБЩАЯ ЗАДАЧА ПРОЕКТА
{task}

МАТЕРИАЛЫ (источники обезличены намеренно)
{reports}"""

# ---------------------------------------------------------------------------
# Поиск конфликтов между отчётами
# ---------------------------------------------------------------------------

CONFLICT_SYSTEM = """\
Ты - супервайзер. Сравни результаты разных подзадач одного проекта и найди
ПРЯМЫЕ противоречия: взаимоисключающие утверждения, несовпадающие числа,
разные ответы на один и тот же вопрос.

Не считай противоречием: разный уровень детализации, разный ракурс на одну
тему, дополняющие друг друга сведения.

Для каждого противоречия оцени, можно ли решить его автоматически - то есть
существует ли объективный признак, по которому одна из версий очевидно верна
(свежая дата, первичный источник, арифметическая проверка).

Ответь СТРОГО одним JSON-объектом:
{
  "conflicts": [
    {"description": "в чём противоречие",
     "severity": "low" | "medium" | "high",
     "labels": ["Исполнитель A", "Исполнитель B"],
     "auto_resolvable": true | false,
     "resolution": "какая версия верна и почему; пусто если решить нельзя"}
  ]
}
Если противоречий нет - верни {"conflicts": []}.
"""


# ---------------------------------------------------------------------------
# Структуры ответов
# ---------------------------------------------------------------------------


@dataclass
class Issue:
    """Одно замечание супервайзера."""

    kind: str = "contradiction"
    severity: str = "medium"
    description: str = ""


@dataclass
class Verdict:
    """Результат проверки одного отчёта."""

    verdict: str = "ok"                      # ok | rework | conflict
    notes: str = ""
    issues: list[Issue] = field(default_factory=list)
    confidence: float | None = None
    raw: str = ""

    @property
    def accepted(self) -> bool:
        return self.verdict == "ok"

    @property
    def max_severity(self) -> str:
        order = {"low": 0, "medium": 1, "high": 2}
        if not self.issues:
            return "low"
        return max((i.severity for i in self.issues), key=lambda s: order.get(s, 1))


@dataclass
class Conflict:
    """Расхождение между результатами разных подзадач."""

    description: str = ""
    severity: str = "medium"
    labels: list[str] = field(default_factory=list)
    auto_resolvable: bool = False
    resolution: str = ""


# ---------------------------------------------------------------------------
# Разбор
# ---------------------------------------------------------------------------

_VALID_VERDICTS = {"ok", "rework", "conflict"}
_VALID_KINDS = {"off_scope", "contradiction", "factual_error", "conflict"}
_VALID_SEVERITY = {"low", "medium", "high"}


def extract_json(text: str) -> dict[str, Any]:
    """Достаёт JSON из ответа модели, даже если он обёрнут в ```json."""
    cleaned = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        data = json.loads(cleaned)
    except ValueError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(cleaned[start:end + 1])
    return data if isinstance(data, dict) else {}


def parse_verdict(text: str) -> Verdict:
    """Разбирает вердикт. Нечитаемый ответ трактуется как «нужна доработка»,
    потому что молча принять непроверенный отчёт хуже, чем перепроверить."""
    try:
        data = extract_json(text)
    except ValueError:
        return Verdict(
            verdict="rework",
            notes="Супервайзер не смог вынести структурированный вердикт. "
                  "Переформулируй отчёт короче и по пунктам.",
            raw=text,
        )

    # Ответ без вердикта (пустой объект, список вместо объекта) - это не
    # "принято": по той же логике, что и нечитаемый ответ, отправляем на
    # доработку, а не пропускаем непроверенным.
    verdict = str(data.get("verdict") or "").lower().strip()
    if verdict not in _VALID_VERDICTS:
        if not data.get("notes"):
            data = {**data, "notes": "Супервайзер не вынес вердикт. "
                                     "Переформулируй отчёт короче и по пунктам."}
        verdict = "rework"

    issues: list[Issue] = []
    raw_issues = data.get("issues")
    for item in raw_issues if isinstance(raw_issues, list) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "contradiction")).lower()
        severity = str(item.get("severity", "medium")).lower()
        issues.append(Issue(
            kind=kind if kind in _VALID_KINDS else "contradiction",
            severity=severity if severity in _VALID_SEVERITY else "medium",
            description=str(item.get("description", "")).strip(),
        ))

    confidence = data.get("confidence")
    try:
        confidence = min(max(float(confidence), 0.0), 1.0) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None

    return Verdict(
        verdict=verdict,
        notes=str(data.get("notes", "")).strip(),
        issues=issues,
        confidence=confidence,
        raw=text,
    )


def parse_conflicts(text: str) -> list[Conflict]:
    """Разбирает список конфликтов; при нечитаемом ответе возвращает пустой список."""
    try:
        data = extract_json(text)
    except ValueError:
        return []
    out: list[Conflict] = []
    raw_conflicts = data.get("conflicts")
    for item in raw_conflicts if isinstance(raw_conflicts, list) else []:
        if not isinstance(item, dict):
            continue
        description = str(item.get("description", "")).strip()
        if not description:
            continue
        severity = str(item.get("severity", "medium")).lower()
        out.append(Conflict(
            description=description,
            severity=severity if severity in _VALID_SEVERITY else "medium",
            labels=[str(x) for x in item["labels"]] if isinstance(item.get("labels"), list) else [],
            auto_resolvable=bool(item.get("auto_resolvable")),
            resolution=str(item.get("resolution", "")).strip(),
        ))
    return out


# ---------------------------------------------------------------------------
# Анонимизация
# ---------------------------------------------------------------------------

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


class Anonymizer:
    """Устойчиво подменяет id агентов на метки «Исполнитель A», «Исполнитель B»...

    Метки стабильны в пределах одного объекта, поэтому супервайзер может
    ссылаться на них внутри одного разбора, но за пределы сводки имена
    не утекают.
    """

    def __init__(self) -> None:
        self._labels: dict[int, str] = {}

    def label(self, agent_id: int | None) -> str:
        if agent_id is None:
            return "Исполнитель ?"
        if agent_id not in self._labels:
            index = len(self._labels)
            suffix = (_ALPHABET[index] if index < len(_ALPHABET)
                      else f"{index + 1}")
            self._labels[agent_id] = f"Исполнитель {suffix}"
        return self._labels[agent_id]

    def scrub(self, text: str, names: dict[int, str]) -> str:
        """Вычищает имена агентов из готового текста - страховка на случай,
        если модель всё-таки назвала кого-то по имени."""
        result = text or ""
        # Длинные имена первыми: "Аналитик Пётр" не должно превратиться в
        # "Исполнитель A Пётр" из-за того, что сначала заменили "Аналитик".
        for agent_id, name in sorted(names.items(), key=lambda kv: -len(kv[1] or "")):
            if name and len(name) > 2:
                # Только целые слова: имя "Ан" не должно резать "Анализ".
                pattern = rf"(?<!\w){re.escape(name)}(?!\w)"
                result = re.sub(pattern, self.label(agent_id), result, flags=re.IGNORECASE)
        return result
