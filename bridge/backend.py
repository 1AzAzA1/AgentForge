"""Корневой объект моста: профиль, воркспейс, события ядра, уведомления.

Окно видит его как цель ``backend``; контроллеры страниц доступны под своими
именами (``agents``, ``run`` ...).
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import i18n as catalog
from app.config import APP_NAME, APP_VERSION, PATHS, AppSettings
from app.i18n import available_languages, set_language, tr
from bridge.state import Hub, State, Timer, error_text, fmt_money, fmt_tokens, rpc
from core.events import Event, EventBus, EventType
from core.orchestrator import Orchestrator
from core.security.crypto import (
    Session,
    keyring_available,
    keyring_delete_password,
    keyring_get_password,
    keyring_store_password,
)
from storage.db import Database
from storage.repositories import Repos, UserRepo

log = logging.getLogger("aiorc.ui")

MIN_PASSWORD_LEN = 8
MOTION_LEVELS = ("full", "reduced", "off")


def strings() -> dict[str, str]:
    """Строки текущего языка. Русский словарь базовый: если в английском
    чего-то нет, строка всё равно не пропадёт из интерфейса."""
    merged = dict(catalog.RU)
    merged.update(catalog.catalog_for(catalog.current_language()))
    return merged


class Backend(State):
    """Состояние приложения, общее для всех экранов."""

    name = "backend"

    def __init__(self, db: Database, settings: AppSettings, hub: Hub | None = None) -> None:
        super().__init__(hub or Hub())
        self.db = db
        self.settings = settings
        self.users = UserRepo(db)
        self.session: Session | None = None
        self.repos: Repos | None = None
        self.bus: EventBus | None = None
        self.orchestrator: Orchestrator | None = None
        self.workspace_id: int | None = None

        from bridge.pages import build_controllers

        self.controllers = build_controllers(self)
        for name, controller in self.controllers.items():
            setattr(self, name, controller)

        self._state_timer = Timer(1000, self._tick)
        self._set(appName=APP_NAME, appVersion=APP_VERSION, dataRoot=str(PATHS.home),
                  keyringAvailable=keyring_available(),
                  loggedIn=False, username="", workspaceId=-1, workspaceName="",
                  running=False, paused=False, pendingApprovals=0, authBusy=False,
                  runElapsed="", runTokens="0", runCost="$0",
                  motion=settings.motion if settings.motion in MOTION_LEVELS else "full")
        self._publish_language()
        self.refresh_profiles()

    def _publish_language(self) -> None:
        self._set(strings=strings(), lang=catalog.current_language(),
                  languages=[{"code": c, "title": t} for c, t in available_languages()])
        for controller in self.controllers.values():
            controller.publish_extras()

    def snapshot_all(self) -> dict[str, Any]:
        return {"backend": self.snapshot(),
                **{name: c.snapshot() for name, c in self.controllers.items()}}

    def target(self, name: str) -> State | None:
        return self if name == "backend" else self.controllers.get(name)

    def toast(self, kind: str, title: str, message: str = "") -> None:
        """Уведомление в углу окна: вид (success|info|warning|error), заголовок, текст."""
        self.emit("toast", kind, title, message)

    @property
    def running(self) -> bool:
        return bool(self._s.get("running"))

    # профиль
    def refresh_profiles(self) -> None:
        names = self.users.list_usernames()
        last = self.settings.last_username if self.settings.last_username in names else \
            (names[0] if names else "")
        self._set(profiles=names, lastUsername=last,
                  rememberDefault=bool(self.settings.remember_master_password))

    def _saved_password(self, username: str) -> str:
        if username and self.settings.remember_master_password and keyring_available():
            return keyring_get_password(username) or ""
        return ""

    @rpc
    def hasSavedPassword(self, username: str) -> bool:  # noqa: N802
        """Есть ли пароль в хранилище ОС. Сам пароль окну не отдаётся."""
        return bool(self._saved_password(username))

    @rpc
    async def signIn(self, username: str, password: str, remember: bool) -> str:  # noqa: N802
        """Вход в профиль. Пустой пароль значит "взять сохранённый в хранилище ОС".

        Возвращает текст ошибки или пустую строку.
        """
        username = (username or "").strip()
        password = password or self._saved_password(username)
        if not username or not password:
            return tr("login.bad_credentials")
        self._set(authBusy=True)
        try:
            # Argon2id занимает десятые доли секунды, поэтому в отдельном потоке.
            session = await asyncio.to_thread(self.users.authenticate, username, password)
        except Exception as exc:  # noqa: BLE001
            return error_text(exc)
        finally:
            self._set(authBusy=False)
        if session is None:
            return tr("login.bad_credentials")
        self.settings.last_username = username
        self.settings.remember_master_password = bool(remember)
        self.settings.save()
        if remember:
            keyring_store_password(username, password)
        else:
            keyring_delete_password(username)
        self._open_session(session)
        return ""

    @rpc
    async def signUp(self, username: str, password: str, password2: str) -> str:  # noqa: N802
        username = (username or "").strip()
        error = ""
        if not username:
            error = tr("login.need_username")
        elif self.users.exists(username):
            error = tr("login.user_exists")
        elif len(password or "") < MIN_PASSWORD_LEN:
            error = tr("login.password_short")
        elif password != password2:
            error = tr("login.password_mismatch")
        if error:
            return error
        self._set(authBusy=True)
        try:
            session = await asyncio.to_thread(self.users.create, username, password)
        except Exception as exc:  # noqa: BLE001
            return error_text(exc)
        finally:
            self._set(authBusy=False)
        self.settings.last_username = username
        self.settings.save()
        self.refresh_profiles()
        self._open_session(session)
        self.toast("success", tr("toast.profile_created"), username)
        return ""

    def _open_session(self, session: Session) -> None:
        self.session = session
        self.repos = Repos(self.db, session)
        fixed = self.repos.recover_interrupted_runs()
        if fixed:
            log.info("После аварийного завершения исправлено статусов: %s", fixed)
        self.bus = EventBus()
        self.bus.subscribe(self._on_event)
        self.orchestrator = Orchestrator(self.repos, self.bus)
        self._set(loggedIn=True, username=session.username)
        self._restore_workspace()
        self._state_timer.start()
        if fixed:
            self.toast("info", tr("toast.recovered"), tr("toast.recovered_text"))

    @rpc
    def logout(self) -> None:
        if self.orchestrator and self.orchestrator.state.running:
            self.orchestrator.stop()
        self._state_timer.stop()
        # Остановленный прогон ещё досылает события о завершении; экраны
        # вышедшего профиля их получать не должны.
        if self.bus is not None:
            self.bus.unsubscribe(self._on_event)
        if self.session:
            self.session.wipe()
        self.session = None
        self.repos = None
        self.bus = None
        self.orchestrator = None
        self.workspace_id = None
        for controller in self.controllers.values():
            controller.reset()
        self._set(loggedIn=False, username="", workspaceId=-1, workspaceName="",
                  running=False, paused=False, pendingApprovals=0)
        self.refresh_profiles()

    def change_password(self, old: str, new1: str, new2: str) -> str:
        """Смена пароля; возвращает текст ошибки или пустую строку."""
        if self.repos is None or self.session is None:
            return tr("login.bad_credentials")
        if len(new1 or "") < MIN_PASSWORD_LEN:
            return tr("login.password_short")
        if new1 != new2:
            return tr("login.password_mismatch")
        if not self.repos.users.change_password(self.session, old, new1):
            return tr("login.bad_credentials")
        # Сохранённый в хранилище ОС пароль иначе перестал бы подходить.
        if self.settings.remember_master_password and keyring_available():
            keyring_store_password(self.session.username, new1)
        return ""

    # воркспейс
    def _restore_workspace(self) -> None:
        assert self.repos is not None and self.session is not None
        user = self.repos.users.get(self.session.user_id)
        last = user.settings.get("last_workspace_id") if user else None
        ids = [w.id for w in self.repos.workspaces.list(self.session.user_id)]
        self.select_workspace(last if last in ids else (ids[0] if ids else None))

    def select_workspace(self, ws_id: int | None) -> None:
        if self.repos is None or self.session is None:
            return
        if self.orchestrator and self.orchestrator.state.running and ws_id != self.workspace_id:
            self.toast("warning", tr("toast.run_active"), tr("toast.run_active_text"))
            return
        self.workspace_id = ws_id
        if ws_id is not None:
            PATHS.workspace_dir(ws_id).mkdir(parents=True, exist_ok=True)
            user = self.repos.users.get(self.session.user_id)
            if user is not None:
                settings = dict(user.settings)
                settings["last_workspace_id"] = ws_id
                self.repos.users.save_settings(self.session.user_id, settings)
        self.update_workspace_name()
        for controller in self.controllers.values():
            try:
                controller.on_workspace_changed()
            except Exception:  # noqa: BLE001 - одна страница не должна ломать остальные
                log.exception("Контроллер %s не обновился", type(controller).__name__)

    def update_workspace_name(self) -> None:
        ws = self.repos.workspaces.get(self.workspace_id) if (self.repos and self.workspace_id) else None
        self._set(workspaceId=ws.id if ws else -1, workspaceName=ws.name if ws else "")

    @rpc
    def selectWorkspace(self, ws_id: int) -> None:  # noqa: N802
        self.select_workspace(ws_id if ws_id is not None and ws_id >= 0 else None)

    # настройки приложения
    @rpc
    def setLanguage(self, code: str) -> None:  # noqa: N802
        if code not in {c for c, _ in available_languages()}:
            return
        set_language(code)
        self.settings.language = code
        self.settings.save()
        self._publish_language()
        # Статусы и подписи, собранные в Python, тоже на новом языке.
        if self.repos is not None:
            for controller in self.controllers.values():
                try:
                    controller.refresh()
                except Exception:  # noqa: BLE001
                    log.exception("Не обновился после смены языка: %s", controller)

    @rpc
    def setMotion(self, level: str) -> None:  # noqa: N802
        if level not in MOTION_LEVELS:
            return
        self.settings.motion = level
        self.settings.save()
        self._set(motion=level)

    @rpc
    def openPath(self, path: str) -> None:  # noqa: N802
        """Открывает файл или каталог в системном проводнике."""
        target = Path(path)
        if not target.exists():
            self.toast("warning", tr("toast.not_found"), str(target))
            return
        if sys.platform == "win32":
            if target.is_file():
                subprocess.Popen(["explorer", "/select,", str(target)])  # noqa: S603, S607
            else:
                os.startfile(str(target))  # noqa: S606
        elif sys.platform == "darwin":
            args = ["open", "-R", str(target)] if target.is_file() else ["open", str(target)]
            subprocess.Popen(args)  # noqa: S603, S607
        else:
            folder = target if target.is_dir() else target.parent
            subprocess.Popen(["xdg-open", str(folder)])  # noqa: S603, S607

    # события ядра
    def _on_event(self, event: Event) -> None:
        if event.workspace_id is not None and self.workspace_id is not None \
                and event.workspace_id != self.workspace_id:
            return
        for controller in self.controllers.values():
            try:
                controller.on_event(event)
            except Exception:  # noqa: BLE001
                log.exception("Сбой обработки события %s", event.type)
        self._sync_run_state()
        self._toast_for(event)

    def _sync_run_state(self) -> None:
        orch = self.orchestrator
        if orch is None:
            return
        gate = orch.gate
        self._set(running=orch.state.running, paused=orch.state.paused,
                  pendingApprovals=len(gate.pending()) if gate else 0)

    def _tick(self) -> None:
        """Раз в секунду: таймер прогона и живые счётчики расхода."""
        orch = self.orchestrator
        if orch is None or not orch.state.running:
            if self._s.get("runElapsed"):
                self._set(runElapsed="")
            return
        try:
            started = datetime.fromisoformat(orch.state.started_at)
            seconds = int((datetime.now(timezone.utc) - started).total_seconds())
        except ValueError:
            seconds = 0
        minutes, sec = divmod(max(seconds, 0), 60)
        hours, minutes = divmod(minutes, 60)
        elapsed = f"{hours}:{minutes:02d}:{sec:02d}" if hours else f"{minutes:02d}:{sec:02d}"
        self._set(runElapsed=elapsed, runTokens=fmt_tokens(orch.state.tokens),
                  runCost=fmt_money(orch.state.cost))

    def _toast_for(self, event: Event) -> None:
        kind = event.type
        if kind is EventType.RUN_FINISHED:
            p = event.payload
            if p.get("stopped"):
                self.toast("warning", tr("toast.run_stopped"), event.message)
            elif p.get("failed"):
                self.toast("error", tr("toast.run_failed"), event.message)
            elif p.get("escalated"):
                self.toast("warning", tr("toast.run_review"), event.message)
            else:
                self.toast("success", tr("toast.run_done"), event.message)
        elif kind is EventType.APPROVAL_REQUESTED:
            self.toast("warning", tr("toast.need_decision"), event.message)
        elif kind is EventType.BUDGET_ALERT:
            self.toast("warning", tr("toast.budget_alert"), event.message)
        elif kind is EventType.BUDGET_EXCEEDED:
            self.toast("error", tr("toast.budget_exceeded"), event.message)
        elif kind is EventType.BUDGET_EXTENDED:
            self.toast("info", tr("toast.budget_extended"), event.message)
        elif kind is EventType.SUBTASK_FAILED:
            who = f"{event.agent_name}: " if event.agent_name else ""
            self.toast("error", tr("toast.subtask_failed"), who + event.message)

    # служебное
    def shutdown(self) -> None:
        """Закрытие окна: гасим агентов, чтобы не оставить висящих запросов."""
        self._state_timer.stop()
        if self.orchestrator and self.orchestrator.state.running:
            self.orchestrator.stop()
