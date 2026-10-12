"""Песочница для исполнения кода агентами.

Ответ на вопрос 3: интерфейс ``Sandbox`` с двумя реализациями.

``SubprocessSandbox`` (по умолчанию, работает везде)
    * отдельный процесс в одноразовом временном каталоге;
    * ``setsid`` + убийство всей группы процессов по таймауту;
    * POSIX: ``RLIMIT_CPU``, ``RLIMIT_AS``, ``RLIMIT_FSIZE``, ``RLIMIT_NPROC``;
    * вычищенное окружение (нет API-ключей и прочих переменных хоста);
    * сеть по умолчанию отключается подстановкой недоступного прокси -
      это не жёсткая изоляция, а барьер «по умолчанию».

``DockerSandbox`` (если найден работающий Docker)
    * ``--network none``, ``--read-only``, ``--pids-limit``, ``--memory``,
      ``--cpus``, непривилегированный пользователь, ``--rm``;
    * настоящая изоляция ФС и сети.

Выбор ``auto`` берёт Docker, когда он доступен, иначе subprocess.
Абсолютной защиты subprocess-режим не даёт, и в UI об этом сказано прямо.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

# Языки, которые разрешено исполнять. Shell намеренно не включён в Docker-режиме
# по умолчанию - он нужен реже, а рисков даёт больше.
LANG_COMMANDS: dict[str, list[str]] = {
    "python": [sys.executable or "python3", "-I", "{file}"],
    "bash": ["bash", "{file}"],
    "node": ["node", "{file}"],
}
LANG_EXT = {"python": ".py", "bash": ".sh", "node": ".js"}


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    backend: str = ""

    def as_text(self, limit: int = 8000) -> str:
        """Компактное представление для отправки модели."""
        parts = [f"[{self.backend}] exit_code={self.exit_code}"]
        if self.timed_out:
            parts.append("ПРЕВЫШЕН ТАЙМАУТ ВЫПОЛНЕНИЯ")
        if self.stdout.strip():
            parts.append("STDOUT:\n" + self.stdout[:limit])
        if self.stderr.strip():
            parts.append("STDERR:\n" + self.stderr[:limit])
        if len(self.stdout) > limit or len(self.stderr) > limit:
            parts.append("(вывод обрезан)")
        return "\n\n".join(parts)


class Sandbox(ABC):
    """Интерфейс песочницы."""

    name = "sandbox"

    @abstractmethod
    async def run(self, code: str, language: str, workdir: Path,
                  timeout: int, memory_mb: int, network: bool) -> SandboxResult:
        ...


# ---------------------------------------------------------------------------


def _preexec(memory_mb: int, cpu_seconds: int):  # pragma: no cover - POSIX-only
    """Ограничения ресурсов для дочернего процесса (POSIX)."""
    import resource

    def apply() -> None:
        os.setsid()  # своя группа процессов: убьём её целиком по таймауту
        mem = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
        except (ValueError, OSError):
            pass

    return apply


class SubprocessSandbox(Sandbox):
    """Исполнение в отдельном процессе с ограничением ресурсов."""

    name = "subprocess"

    async def run(self, code: str, language: str, workdir: Path,
                  timeout: int, memory_mb: int, network: bool) -> SandboxResult:
        if language not in LANG_COMMANDS:
            return SandboxResult(1, "", f"Язык «{language}» не поддерживается",
                                 backend=self.name)

        workdir.mkdir(parents=True, exist_ok=True)
        tmpdir = Path(tempfile.mkdtemp(prefix="aiorc_run_", dir=str(workdir)))
        script = tmpdir / f"script{LANG_EXT[language]}"
        script.write_text(code, "utf-8")

        cmd = [part.format(file=str(script)) for part in LANG_COMMANDS[language]]
        if shutil.which(cmd[0]) is None and not Path(cmd[0]).exists():
            shutil.rmtree(tmpdir, ignore_errors=True)
            return SandboxResult(127, "", f"Интерпретатор «{cmd[0]}» не найден в системе",
                                 backend=self.name)

        # Чистое окружение: никаких ключей и токенов хоста.
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmpdir),
            "TMPDIR": str(tmpdir),
            "LANG": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if not network:
            # Грубый, но действенный барьер для большинства http-библиотек.
            env.update({"http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9",
                        "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
                        "no_proxy": ""})

        kwargs: dict = {}
        if os.name == "posix":
            kwargs["preexec_fn"] = _preexec(memory_mb, timeout)
        else:  # Windows: своя группа процессов, чтобы корректно убивать дерево
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(tmpdir), env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL, **kwargs,
            )
        except OSError as exc:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return SandboxResult(126, "", f"Не удалось запустить процесс: {exc}",
                                 backend=self.name)

        timed_out = False
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            _kill_tree(proc)
            out, err = b"", "Процесс остановлен по таймауту".encode("utf-8")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        return SandboxResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=out.decode("utf-8", "replace"),
            stderr=err.decode("utf-8", "replace"),
            timed_out=timed_out,
            backend=self.name,
        )


def _kill_tree(proc) -> None:
    """Убивает процесс вместе со всеми потомками.

    На Windows ``proc.kill()`` завершает только сам процесс: запущенные им
    дочерние (``subprocess``, ``multiprocessing``) продолжили бы работать
    после таймаута. ``taskkill /T`` снимает всё дерево.
    """
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), 9)
        else:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if proc.returncode is None:
                proc.kill()
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


class DockerSandbox(Sandbox):
    """Исполнение в одноразовом контейнере с отключённой сетью."""

    name = "docker"
    IMAGES = {"python": "python:3.12-slim", "bash": "debian:stable-slim",
              "node": "node:22-slim"}

    async def run(self, code: str, language: str, workdir: Path,
                  timeout: int, memory_mb: int, network: bool) -> SandboxResult:
        if language not in self.IMAGES:
            return SandboxResult(1, "", f"Язык «{language}» не поддерживается",
                                 backend=self.name)
        workdir.mkdir(parents=True, exist_ok=True)
        tmpdir = Path(tempfile.mkdtemp(prefix="aiorc_dock_", dir=str(workdir)))
        script = tmpdir / f"script{LANG_EXT[language]}"
        script.write_text(code, "utf-8")

        inner = {"python": ["python", "/work/" + script.name],
                 "bash": ["bash", "/work/" + script.name],
                 "node": ["node", "/work/" + script.name]}[language]

        # Имя нужно, чтобы по таймауту остановить именно контейнер: убийство
        # процесса docker CLI оставило бы контейнер работать в фоне.
        name = f"aiorc-{uuid.uuid4().hex[:12]}"
        cmd = [
            "docker", "run", "--rm", "--name", name,
            "--network", "bridge" if network else "none",
            "--memory", f"{memory_mb}m", "--memory-swap", f"{memory_mb}m",
            "--cpus", "1", "--pids-limit", "128",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", "1000:1000",
            # Корень контейнера только для чтения; писать можно в /tmp и в
            # одноразовый каталог со скриптом.
            "--read-only", "--tmpfs", "/tmp:rw,size=64m",
            "-v", f"{tmpdir}:/work",
            "-w", "/work",
            self.IMAGES[language], *inner,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
            )
            timed_out = False
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout + 15)
            except asyncio.TimeoutError:
                timed_out = True
                await _docker_kill(name)
                proc.kill()
                out, err = b"", "Контейнер остановлен по таймауту".encode("utf-8")
            return SandboxResult(
                proc.returncode if proc.returncode is not None else -1,
                out.decode("utf-8", "replace"), err.decode("utf-8", "replace"),
                timed_out, self.name,
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


async def _docker_kill(name: str) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "kill", name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=10)
    except Exception:  # noqa: BLE001 - контейнер мог уже завершиться сам
        pass


# результат проверки Docker кэшируется: ``docker info`` занимает секунды
_DOCKER_CACHE: dict[str, float | bool] = {}
_DOCKER_TTL = 60.0


def docker_available(use_cache: bool = True) -> bool:
    """Проверяет, что Docker установлен и демон отвечает."""
    now = time.monotonic()
    if use_cache and _DOCKER_CACHE and now - float(_DOCKER_CACHE["at"]) < _DOCKER_TTL:
        return bool(_DOCKER_CACHE["ok"])
    ok = False
    if shutil.which("docker") is not None:
        try:
            res = subprocess.run(["docker", "info", "--format", "{{json .ServerVersion}}"],
                                 capture_output=True, timeout=5,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            ok = res.returncode == 0 and bool(json.loads(res.stdout or b'""'))
        except Exception:  # noqa: BLE001
            ok = False
    _DOCKER_CACHE.update(ok=ok, at=now)
    return ok


async def docker_available_async() -> bool:
    """То же, но без блокировки общего asyncio-лупа (и интерфейса вместе с ним)."""
    return await asyncio.to_thread(docker_available)


async def get_sandbox(backend: str = "auto") -> Sandbox:
    """Фабрика песочницы по настройке воркспейса."""
    if backend == "docker":
        return DockerSandbox()
    if backend == "subprocess":
        return SubprocessSandbox()
    return DockerSandbox() if await docker_available_async() else SubprocessSandbox()
