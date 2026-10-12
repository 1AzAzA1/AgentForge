"""Репозитории - единственная точка доступа к БД.

UI и ядро никогда не пишут SQL напрямую: это упрощает будущую замену
хранилища и гарантирует, что секреты шифруются в одном месте.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from core.security.crypto import (
    SecretBox,
    Session,
    derive_master_key,
    hash_password,
    new_salt,
    verify_password,
)
from storage.db import Database, utcnow
from storage.models import (
    Agent,
    ApiKey,
    Budget,
    Incident,
    Report,
    Subtask,
    Summary,
    Task,
    User,
    Workspace,
    dumps,
)


def _loads(raw: str | None) -> dict:
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class UserRepo:
    """Локальные профили: регистрация, вход, смена пароля."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def list_usernames(self) -> list[str]:
        return [r["username"] for r in self.db.query("SELECT username FROM users ORDER BY username")]

    def exists(self, username: str) -> bool:
        return self.db.query_one("SELECT 1 FROM users WHERE username = ?", (username,)) is not None

    def create(self, username: str, password: str) -> Session:
        """Создаёт профиль и сразу возвращает открытую сессию."""
        verify_salt, kdf_salt = new_salt(), new_salt()
        uid = self.db.insert(
            "INSERT INTO users(username, password_hash, verify_salt, kdf_salt, created_at) "
            "VALUES (?,?,?,?,?)",
            (username, hash_password(password, verify_salt), verify_salt, kdf_salt, utcnow()),
        )
        return Session(uid, username, SecretBox(derive_master_key(password, kdf_salt)))

    def authenticate(self, username: str, password: str) -> Session | None:
        """Проверяет пароль и выводит мастер-ключ шифрования."""
        row = self.db.query_one("SELECT * FROM users WHERE username = ?", (username,))
        if not row or not verify_password(password, row["verify_salt"], row["password_hash"]):
            return None
        key = derive_master_key(password, row["kdf_salt"])
        return Session(row["id"], row["username"], SecretBox(key))

    def get(self, user_id: int) -> User | None:
        row = self.db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
        return User.from_row(row) if row else None

    def change_password(self, session: Session, old_password: str, new_password: str) -> bool:
        """Меняет пароль и ПЕРЕШИФРОВЫВАЕТ все API-ключи новым мастер-ключом."""
        row = self.db.query_one("SELECT * FROM users WHERE id = ?", (session.user_id,))
        if not row or not verify_password(old_password, row["verify_salt"], row["password_hash"]):
            return False

        old_box = session.box
        new_verify_salt, new_kdf_salt = new_salt(), new_salt()
        new_box = SecretBox(derive_master_key(new_password, new_kdf_salt))

        # Сначала расшифровываем всё старым ключом (если что-то не читается,
        # исключение вылетит до первой записи), затем пишем одной транзакцией:
        # сбой посередине не должен оставить часть ключей на новом мастер-ключе
        # при старом пароле - такие ключи было бы уже не расшифровать.
        rows = self.db.query(
            "SELECT id, secret_blob FROM api_keys WHERE user_id = ? AND secret_blob IS NOT NULL",
            (session.user_id,),
        )
        reencrypted = [
            (new_box.encrypt(old_box.decrypt(r["secret_blob"])), r["id"]) for r in rows
        ]
        # Ключ поискового API лежит в настройках воркспейсов, зашифрованный
        # тем же мастер-ключом. Без перешифровки он молча пропал бы после
        # смены пароля: расшифровать его новым ключом уже нельзя.
        resealed_ws = []
        for ws in self.db.query(
            "SELECT id, settings_json FROM workspaces WHERE user_id = ?", (session.user_id,)
        ):
            settings = _loads(ws["settings_json"])
            token = settings.get("search_api_key") or ""
            if isinstance(token, str) and token.startswith(SecretCodec.PREFIX):
                plain = SecretCodec(session).open(token)
                settings["search_api_key"] = (
                    SecretCodec.PREFIX + base64.b64encode(new_box.encrypt(plain)).decode("ascii")
                    if plain else ""
                )
                resealed_ws.append((dumps(settings), ws["id"]))
        new_hash = hash_password(new_password, new_verify_salt)
        with self.db.transaction() as conn:
            conn.executemany("UPDATE api_keys SET secret_blob = ? WHERE id = ?", reencrypted)
            conn.executemany("UPDATE workspaces SET settings_json = ? WHERE id = ?", resealed_ws)
            conn.execute(
                "UPDATE users SET password_hash = ?, verify_salt = ?, kdf_salt = ? WHERE id = ?",
                (new_hash, new_verify_salt, new_kdf_salt, session.user_id),
            )
        session.box = new_box
        return True

    def save_settings(self, user_id: int, settings: dict) -> None:
        self.db.execute(
            "UPDATE users SET settings_json = ? WHERE id = ?", (dumps(settings), user_id)
        )


class WorkspaceRepo:
    """Воркспейсы = параллельные проекты со своим набором агентов."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def list(self, user_id: int, include_archived: bool = False) -> list[Workspace]:
        sql = "SELECT * FROM workspaces WHERE user_id = ?"
        if not include_archived:
            sql += " AND archived = 0"
        sql += " ORDER BY updated_at DESC"
        return [Workspace.from_row(r) for r in self.db.query(sql, (user_id,))]

    def get(self, ws_id: int) -> Workspace | None:
        row = self.db.query_one("SELECT * FROM workspaces WHERE id = ?", (ws_id,))
        return Workspace.from_row(row) if row else None

    def create(self, user_id: int, name: str, description: str, settings: dict) -> Workspace:
        now = utcnow()
        ws_id = self.db.insert(
            "INSERT INTO workspaces(user_id, name, description, settings_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (user_id, name, description, dumps(settings), now, now),
        )
        return self.get(ws_id)  # type: ignore[return-value]

    def update(self, ws_id: int, **fields: Any) -> None:
        allowed = {"name", "description", "archived"}
        sets, params = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k} = ?")
                params.append(v)
            elif k == "settings":
                sets.append("settings_json = ?")
                params.append(dumps(v))
        if not sets:
            return
        sets.append("updated_at = ?")
        params.extend([utcnow(), ws_id])
        self.db.execute(f"UPDATE workspaces SET {', '.join(sets)} WHERE id = ?", params)

    def delete(self, ws_id: int) -> None:
        self.db.execute("DELETE FROM workspaces WHERE id = ?", (ws_id,))

    def agent_count(self, ws_id: int) -> int:
        row = self.db.query_one("SELECT COUNT(*) c FROM agents WHERE workspace_id = ?", (ws_id,))
        return int(row["c"]) if row else 0


class ApiKeyRepo:
    """Хранилище API-ключей. Секрет шифруется мастер-ключом сессии."""

    def __init__(self, db: Database, session: Session) -> None:
        self.db = db
        self.session = session

    def list(self) -> list[ApiKey]:
        rows = self.db.query(
            "SELECT * FROM api_keys WHERE user_id = ? ORDER BY provider, label",
            (self.session.user_id,),
        )
        return [ApiKey.from_row(r) for r in rows]

    def get(self, key_id: int) -> ApiKey | None:
        row = self.db.query_one(
            "SELECT * FROM api_keys WHERE id = ? AND user_id = ?",
            (key_id, self.session.user_id),
        )
        return ApiKey.from_row(row) if row else None

    def reveal(self, key_id: int) -> str:
        """Расшифровывает секрет. Вызывается только в момент запроса к провайдеру."""
        row = self.db.query_one(
            "SELECT secret_blob FROM api_keys WHERE id = ? AND user_id = ?",
            (key_id, self.session.user_id),
        )
        if not row or row["secret_blob"] is None:
            return ""
        return self.session.box.decrypt(row["secret_blob"])

    def create(self, label: str, provider: str, secret: str,
               base_url: str = "", meta: dict | None = None) -> ApiKey:
        blob = self.session.box.encrypt(secret) if secret else None
        key_id = self.db.insert(
            "INSERT INTO api_keys(user_id, label, provider, base_url, secret_blob, "
            "meta_json, created_at) VALUES (?,?,?,?,?,?,?)",
            (self.session.user_id, label, provider, base_url, blob,
             dumps(meta or {}), utcnow()),
        )
        return self.get(key_id)  # type: ignore[return-value]

    def update(self, key_id: int, label: str, base_url: str,
               secret: str | None = None, meta: dict | None = None) -> None:
        sets = ["label = ?", "base_url = ?"]
        params: list[Any] = [label, base_url]
        if secret is not None:
            sets.append("secret_blob = ?")
            params.append(self.session.box.encrypt(secret) if secret else None)
        if meta is not None:
            sets.append("meta_json = ?")
            params.append(dumps(meta))
        params.extend([key_id, self.session.user_id])
        self.db.execute(
            f"UPDATE api_keys SET {', '.join(sets)} WHERE id = ? AND user_id = ?", params
        )

    def update_meta(self, key_id: int, **changes: Any) -> None:
        """Правит только метаданные (кэш моделей и т.п.), не трогая подпись и адрес.

        Фоновая проверка ключа пишет результат сюда: запись целиком затёрла
        бы правку, которую пользователь успел сделать, пока шёл запрос.
        """
        key = self.get(key_id)
        if key is None:
            return
        meta = {**key.meta, **changes}
        self.db.execute(
            "UPDATE api_keys SET meta_json = ? WHERE id = ? AND user_id = ?",
            (dumps(meta), key_id, self.session.user_id),
        )

    def delete(self, key_id: int) -> None:
        self.db.execute(
            "DELETE FROM api_keys WHERE id = ? AND user_id = ?", (key_id, self.session.user_id)
        )


class AgentRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def list(self, ws_id: int) -> list[Agent]:
        rows = self.db.query(
            "SELECT * FROM agents WHERE workspace_id = ? ORDER BY is_supervisor DESC, id",
            (ws_id,),
        )
        return [Agent.from_row(r) for r in rows]

    def get(self, agent_id: int) -> Agent | None:
        row = self.db.query_one("SELECT * FROM agents WHERE id = ?", (agent_id,))
        return Agent.from_row(row) if row else None

    def create(self, ws_id: int, name: str, role: str, system_prompt: str,
               api_key_id: int | None, provider: str, model: str,
               params: dict, is_supervisor: bool = False) -> Agent:
        agent_id = self.db.insert(
            "INSERT INTO agents(workspace_id, name, role, system_prompt, api_key_id, provider, "
            "model, params_json, is_supervisor, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ws_id, name, role, system_prompt, api_key_id, provider, model,
             dumps(params), int(is_supervisor), utcnow()),
        )
        return self.get(agent_id)  # type: ignore[return-value]

    def update(self, agent_id: int, **fields: Any) -> None:
        allowed = {"name", "role", "system_prompt", "api_key_id", "provider",
                   "model", "enabled", "status", "is_supervisor"}
        sets, params = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k} = ?")
                params.append(int(v) if isinstance(v, bool) else v)
            elif k == "params":
                sets.append("params_json = ?")
                params.append(dumps(v))
        if not sets:
            return
        params.append(agent_id)
        self.db.execute(f"UPDATE agents SET {', '.join(sets)} WHERE id = ?", params)

    def set_status(self, agent_id: int, status: str) -> None:
        self.db.execute("UPDATE agents SET status = ? WHERE id = ?", (status, agent_id))

    def delete(self, agent_id: int) -> None:
        self.db.execute("DELETE FROM agents WHERE id = ?", (agent_id,))


class TaskRepo:
    """Задачи и подзадачи воркспейса."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # задачи
    def current(self, ws_id: int) -> Task | None:
        """Последняя (активная) задача воркспейса."""
        row = self.db.query_one(
            "SELECT * FROM tasks WHERE workspace_id = ? ORDER BY id DESC LIMIT 1", (ws_id,)
        )
        return Task.from_row(row) if row else None

    def get(self, task_id: int) -> Task | None:
        row = self.db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return Task.from_row(row) if row else None

    def create(self, ws_id: int, title: str, description: str,
               result_format: str = "auto", token_limit: int | None = None) -> Task:
        now = utcnow()
        task_id = self.db.insert(
            "INSERT INTO tasks(workspace_id, title, description, result_format, token_limit, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (ws_id, title, description, result_format, token_limit, now, now),
        )
        return self.get(task_id)  # type: ignore[return-value]

    def update(self, task_id: int, **fields: Any) -> None:
        allowed = {"title", "description", "status", "result_format", "token_limit"}
        sets = [f"{k} = ?" for k in fields if k in allowed]
        params = [v for k, v in fields.items() if k in allowed]
        if not sets:
            return
        sets.append("updated_at = ?")
        params.extend([utcnow(), task_id])
        self.db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params)

    # подзадачи
    def subtasks(self, task_id: int) -> list[Subtask]:
        rows = self.db.query(
            "SELECT * FROM subtasks WHERE task_id = ? ORDER BY order_index, id", (task_id,)
        )
        return [Subtask.from_row(r) for r in rows]

    def get_subtask(self, subtask_id: int) -> Subtask | None:
        row = self.db.query_one("SELECT * FROM subtasks WHERE id = ?", (subtask_id,))
        return Subtask.from_row(row) if row else None

    def add_subtask(self, task_id: int, title: str, description: str = "",
                    agent_id: int | None = None) -> Subtask:
        row = self.db.query_one(
            "SELECT COALESCE(MAX(order_index), -1) + 1 AS n FROM subtasks WHERE task_id = ?",
            (task_id,),
        )
        now = utcnow()
        sid = self.db.insert(
            "INSERT INTO subtasks(task_id, agent_id, title, description, order_index, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (task_id, agent_id, title, description, int(row["n"]) if row else 0, now, now),
        )
        return self.get_subtask(sid)  # type: ignore[return-value]

    def update_subtask(self, subtask_id: int, **fields: Any) -> None:
        allowed = {"agent_id", "title", "description", "status", "order_index",
                   "depends_on", "result", "rework_count", "tokens_in",
                   "tokens_out", "cost_usd"}
        sets = [f"{k} = ?" for k in fields if k in allowed]
        params = [v for k, v in fields.items() if k in allowed]
        if not sets:
            return
        sets.append("updated_at = ?")
        params.extend([utcnow(), subtask_id])
        self.db.execute(f"UPDATE subtasks SET {', '.join(sets)} WHERE id = ?", params)

    def delete_subtask(self, subtask_id: int) -> None:
        self.db.execute("DELETE FROM subtasks WHERE id = ?", (subtask_id,))

    def reorder(self, ordered_ids: list[int]) -> None:
        """Переписывает order_index по переданному порядку id."""
        self.db.executemany(
            "UPDATE subtasks SET order_index = ? WHERE id = ?",
            [(i, sid) for i, sid in enumerate(ordered_ids)],
        )


class ReportRepo:
    """Отчёты агентов и сводки супервайзера (этапы 4-5)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def add_report(self, ws_id: int, task_id: int | None, subtask_id: int | None,
                   agent_id: int | None, content: str, confidence: float | None = None,
                   tokens_in: int = 0, tokens_out: int = 0, cost_usd: float = 0.0) -> int:
        return self.db.insert(
            "INSERT INTO reports(workspace_id, task_id, subtask_id, agent_id, content, "
            "confidence, tokens_in, tokens_out, cost_usd, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ws_id, task_id, subtask_id, agent_id, content, confidence,
             tokens_in, tokens_out, cost_usd, utcnow()),
        )

    def list_reports(self, ws_id: int, limit: int = 100) -> list[Report]:
        rows = self.db.query(
            "SELECT * FROM reports WHERE workspace_id = ? ORDER BY id DESC LIMIT ?",
            (ws_id, limit),
        )
        return [Report.from_row(r) for r in rows]

    def unreviewed(self, ws_id: int) -> list[Report]:
        rows = self.db.query(
            "SELECT * FROM reports WHERE workspace_id = ? AND reviewed = 0 ORDER BY id",
            (ws_id,),
        )
        return [Report.from_row(r) for r in rows]

    def mark_reviewed(self, report_id: int, verdict: str, notes: str = "") -> None:
        self.db.execute(
            "UPDATE reports SET reviewed = 1, review_verdict = ?, review_notes = ? WHERE id = ?",
            (verdict, notes, report_id),
        )

    def add_summary(self, ws_id: int, task_id: int | None, content: str,
                    trigger: str, delivered_to: list[int]) -> int:
        return self.db.insert(
            "INSERT INTO summaries(workspace_id, task_id, content, trigger, delivered_to, "
            "created_at) VALUES (?,?,?,?,?,?)",
            (ws_id, task_id, content, trigger,
             ",".join(str(i) for i in delivered_to), utcnow()),
        )

    def list_summaries(self, ws_id: int, limit: int = 50) -> list[Summary]:
        rows = self.db.query(
            "SELECT * FROM summaries WHERE workspace_id = ? ORDER BY id DESC LIMIT ?",
            (ws_id, limit),
        )
        return [Summary.from_row(r) for r in rows]


class IncidentRepo:
    """История инцидентов: что супервайзер счёл ошибкой и чем это кончилось."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, ws_id: int, kind: str, description: str, severity: str = "medium",
            task_id: int | None = None, subtask_id: int | None = None,
            report_id: int | None = None) -> int:
        return self.db.insert(
            "INSERT INTO incidents(workspace_id, task_id, subtask_id, report_id, kind, "
            "severity, description, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (ws_id, task_id, subtask_id, report_id, kind, severity, description, utcnow()),
        )

    def list(self, ws_id: int, limit: int = 100) -> list[Incident]:
        rows = self.db.query(
            "SELECT * FROM incidents WHERE workspace_id = ? ORDER BY id DESC LIMIT ?",
            (ws_id, limit),
        )
        return [Incident.from_row(r) for r in rows]

    def resolve_for_subtask(self, subtask_id: int, resolution: str) -> int:
        """Закрывает открытые инциденты подзадачи - например, после доработки.

        Возвращает количество закрытых записей.
        """
        cur = self.db.execute(
            "UPDATE incidents SET status = 'resolved', resolution = ?, resolved_at = ? "
            "WHERE subtask_id = ? AND status IN ('open', 'escalated')",
            (resolution, utcnow(), subtask_id),
        )
        return cur.rowcount or 0

    def resolve(self, incident_id: int, status: str, resolution: str) -> None:
        self.db.execute(
            "UPDATE incidents SET status = ?, resolution = ?, resolved_at = ? WHERE id = ?",
            (status, resolution, utcnow(), incident_id),
        )


class ApprovalRepo:
    """Точки human-in-the-loop: заданные вопросы и принятые решения."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, ws_id: int, task_id: int | None, reason: str,
               payload: dict) -> int:
        return self.db.insert(
            "INSERT INTO approvals(workspace_id, task_id, reason, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (ws_id, task_id, reason, dumps(payload), utcnow()),
        )

    def decide(self, approval_id: int, decision: str, comment: str = "") -> None:
        self.db.execute(
            "UPDATE approvals SET decision = ?, comment = ?, decided_at = ? WHERE id = ?",
            (decision, comment, utcnow(), approval_id),
        )

    def history(self, ws_id: int, limit: int = 100) -> list[dict]:
        """История решений, новые сверху - для вкладки супервайзера."""
        rows = self.db.query(
            "SELECT * FROM approvals WHERE workspace_id = ? ORDER BY id DESC LIMIT ?",
            (ws_id, limit),
        )
        return [dict(r) for r in rows]

    def pending_count(self, ws_id: int) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) n FROM approvals WHERE workspace_id = ? AND decision = ''",
            (ws_id,),
        )
        return int(row["n"]) if row else 0


class BudgetRepo:
    """Лимиты по токенам и деньгам + журнал расхода."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, scope: str, scope_id: int) -> Budget | None:
        row = self.db.query_one(
            "SELECT * FROM budgets WHERE scope = ? AND scope_id = ?", (scope, scope_id)
        )
        return Budget.from_row(row) if row else None

    def upsert(self, scope: str, scope_id: int, token_limit: int | None,
               cost_limit_usd: float | None, alert_threshold: float = 0.8) -> None:
        self.db.execute(
            "INSERT INTO budgets(scope, scope_id, token_limit, cost_limit_usd, "
            "alert_threshold, updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(scope, scope_id) DO UPDATE SET token_limit = excluded.token_limit, "
            "cost_limit_usd = excluded.cost_limit_usd, "
            "alert_threshold = excluded.alert_threshold, updated_at = excluded.updated_at",
            (scope, scope_id, token_limit, cost_limit_usd, alert_threshold, utcnow()),
        )

    def add_usage(self, scope: str, scope_id: int, tokens: int, cost: float) -> None:
        self.db.execute(
            "UPDATE budgets SET tokens_used = tokens_used + ?, cost_used_usd = cost_used_usd + ?, "
            "updated_at = ? WHERE scope = ? AND scope_id = ?",
            (tokens, cost, utcnow(), scope, scope_id),
        )

    def log_call(self, ws_id: int | None, task_id: int | None, subtask_id: int | None,
                 agent_id: int | None, provider: str, model: str,
                 tokens_in: int, tokens_out: int, cost: float) -> None:
        self.db.execute(
            "INSERT INTO usage_log(workspace_id, task_id, subtask_id, agent_id, provider, "
            "model, tokens_in, tokens_out, cost_usd, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ws_id, task_id, subtask_id, agent_id, provider, model,
             tokens_in, tokens_out, cost, utcnow()),
        )

    def workspace_totals(self, ws_id: int) -> tuple[int, float]:
        row = self.db.query_one(
            "SELECT COALESCE(SUM(tokens_in + tokens_out), 0) t, COALESCE(SUM(cost_usd), 0) c "
            "FROM usage_log WHERE workspace_id = ?",
            (ws_id,),
        )
        return (int(row["t"]), float(row["c"])) if row else (0, 0.0)

    def usage_series(self, ws_id: int, limit: int = 300) -> list[tuple[str, int, float]]:
        """Хронология вызовов: (время, токены, стоимость).

        Возвращает последние ``limit`` записей в прямом порядке - из них
        дашборд строит кумулятивные кривые расхода.
        """
        rows = self.db.query(
            "SELECT created_at, tokens_in + tokens_out AS tokens, cost_usd FROM ("
            "  SELECT id, created_at, tokens_in, tokens_out, cost_usd FROM usage_log "
            "  WHERE workspace_id = ? ORDER BY id DESC LIMIT ?"
            ") ORDER BY id",
            (ws_id, limit),
        )
        return [(r["created_at"], int(r["tokens"]), float(r["cost_usd"])) for r in rows]

    def usage_by_agent(self, ws_id: int) -> list[tuple[int | None, int, float]]:
        """Расход в разрезе агентов: (agent_id, токены, стоимость)."""
        rows = self.db.query(
            "SELECT agent_id, COALESCE(SUM(tokens_in + tokens_out), 0) t, "
            "COALESCE(SUM(cost_usd), 0) c FROM usage_log WHERE workspace_id = ? "
            "GROUP BY agent_id ORDER BY t DESC",
            (ws_id,),
        )
        return [(r["agent_id"], int(r["t"]), float(r["c"])) for r in rows]

    def task_totals(self, task_id: int) -> tuple[int, float]:
        """Расход по конкретной задаче."""
        row = self.db.query_one(
            "SELECT COALESCE(SUM(tokens_in + tokens_out), 0) t, COALESCE(SUM(cost_usd), 0) c "
            "FROM usage_log WHERE task_id = ?",
            (task_id,),
        )
        return (int(row["t"]), float(row["c"])) if row else (0, 0.0)

    def agent_totals(self, ws_id: int) -> dict[int, tuple[int, float]]:
        """Расход по каждому агенту воркспейса."""
        rows = self.db.query(
            "SELECT agent_id, COALESCE(SUM(tokens_in + tokens_out), 0) t, "
            "COALESCE(SUM(cost_usd), 0) c FROM usage_log "
            "WHERE workspace_id = ? AND agent_id IS NOT NULL GROUP BY agent_id",
            (ws_id,),
        )
        return {int(r["agent_id"]): (int(r["t"]), float(r["c"])) for r in rows}

    def list_limits(self, scope: str, scope_ids: list[int]) -> dict[int, Budget]:
        """Лимиты нескольких объектов одного типа одним запросом."""
        if not scope_ids:
            return {}
        marks = ",".join("?" * len(scope_ids))
        rows = self.db.query(
            f"SELECT * FROM budgets WHERE scope = ? AND scope_id IN ({marks})",
            [scope, *scope_ids],
        )
        return {int(r["scope_id"]): Budget.from_row(r) for r in rows}

    def delete_limit(self, scope: str, scope_id: int) -> None:
        self.db.execute("DELETE FROM budgets WHERE scope = ? AND scope_id = ?",
                        (scope, scope_id))

    def sync_used(self, scope: str, scope_id: int, tokens: int, cost: float) -> None:
        """Записывает фактический расход в строку лимита (для отображения)."""
        self.db.execute(
            "UPDATE budgets SET tokens_used = ?, cost_used_usd = ?, updated_at = ? "
            "WHERE scope = ? AND scope_id = ?",
            (tokens, cost, utcnow(), scope, scope_id),
        )

    def incident_counts(self, ws_id: int) -> dict[str, int]:
        """Инциденты по статусам - для плашки «требуют решения»."""
        rows = self.db.query(
            "SELECT status, COUNT(*) n FROM incidents WHERE workspace_id = ? GROUP BY status",
            (ws_id,),
        )
        return {r["status"]: int(r["n"]) for r in rows}


class MessageRepo:
    """Приватная история агента. Выборки ВСЕГДА фильтруются по agent_id,
    поэтому один агент физически не может прочитать контекст другого."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, agent_id: int, role: str, content: str, subtask_id: int | None = None,
            tool_name: str = "", tool_call_id: str = "", tokens: int = 0) -> int:
        return self.db.insert(
            "INSERT INTO messages(agent_id, subtask_id, role, content, tool_name, "
            "tool_call_id, tokens, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (agent_id, subtask_id, role, content, tool_name, tool_call_id, tokens, utcnow()),
        )

    def history(self, agent_id: int, subtask_id: int | None = None,
                limit: int = 200) -> list[dict]:
        """Последние ``limit`` сообщений агента в хронологическом порядке.

        Выбираются именно последние: при длинной истории (несколько кругов
        доработки) агенту важнее свежие замечания, чем самые первые шаги.
        """
        sql = "SELECT * FROM messages WHERE agent_id = ?"
        params: list[Any] = [agent_id]
        if subtask_id is not None:
            sql += " AND subtask_id = ?"
            params.append(subtask_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in self.db.query(sql, params)]
        rows.reverse()
        return rows

    def clear(self, agent_id: int) -> None:
        self.db.execute("DELETE FROM messages WHERE agent_id = ?", (agent_id,))


class SecretCodec:
    """Шифрует короткие секреты, которые хранятся не в ``api_keys``.

    Например, ключ поискового API лежит в настройках воркспейса: там это
    строка base64, а открытым текстом она существует только в памяти.
    """

    PREFIX = "enc:"

    def __init__(self, session: Session) -> None:
        self.session = session

    def seal(self, text: str) -> str:
        if not text:
            return ""
        return self.PREFIX + base64.b64encode(self.session.box.encrypt(text)).decode("ascii")

    def open(self, token: str) -> str:
        if not token:
            return ""
        if not token.startswith(self.PREFIX):
            return token          # старое значение, сохранённое открытым текстом
        try:
            return self.session.box.decrypt(base64.b64decode(token[len(self.PREFIX):]))
        except Exception:  # noqa: BLE001 - повреждённый секрет равен отсутствующему
            return ""


class Repos:
    """Агрегатор репозиториев - удобно передавать одним объектом в UI."""

    def __init__(self, db: Database, session: Session) -> None:
        self.db = db
        self.session = session
        self.users = UserRepo(db)
        self.workspaces = WorkspaceRepo(db)
        self.keys = ApiKeyRepo(db, session)
        self.agents = AgentRepo(db)
        self.tasks = TaskRepo(db)
        self.reports = ReportRepo(db)
        self.incidents = IncidentRepo(db)
        self.budgets = BudgetRepo(db)
        self.approvals = ApprovalRepo(db)
        self.messages = MessageRepo(db)
        self.secrets = SecretCodec(session)

    def recover_interrupted_runs(self) -> int:
        """Приводит в порядок статусы после аварийного завершения приложения.

        Если процесс упал посреди прогона, в базе остаются «работающие»
        задачи и агенты, которых на самом деле никто не выполняет. Интерфейс
        показывал бы их как активные, а повторный запуск считал бы занятыми.
        Возвращает количество исправленных записей.
        """
        user_ws = "SELECT id FROM workspaces WHERE user_id = ?"
        uid = (self.session.user_id,)
        with self.db.transaction() as conn:
            changed = conn.execute(
                f"UPDATE tasks SET status = 'stopped' WHERE status = 'running' "
                f"AND workspace_id IN ({user_ws})", uid).rowcount
            changed += conn.execute(
                f"UPDATE subtasks SET status = 'paused' WHERE status = 'running' "
                f"AND task_id IN (SELECT id FROM tasks WHERE workspace_id IN ({user_ws}))",
                uid).rowcount
            changed += conn.execute(
                f"UPDATE agents SET status = 'idle' WHERE status IN ('running', 'paused') "
                f"AND workspace_id IN ({user_ws})", uid).rowcount
            conn.execute(
                "UPDATE approvals SET decision = 'cancelled', "
                "comment = 'Приложение было закрыто до решения', decided_at = ? "
                f"WHERE decision = '' AND workspace_id IN ({user_ws})",
                (utcnow(), *uid))
        return changed
