from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping


HEALTHCHECK_ATTEMPTS = 2
HEALTHCHECK_TIMEOUT_SECONDS = 8.0
HEALTHCHECK_RETRY_DELAY_SECONDS = 0.2


class HealthcheckFailure(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _clean_runtime_value(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().strip('"').strip("'")


def _read_pm2_runtime_env(pm2_name: str) -> Mapping[str, str]:
    try:
        result = subprocess.run(
            ["pm2", "jlist"],
            check=True,
            capture_output=True,
            text=True,
        )
        processes = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        raise HealthcheckFailure("pm2_unavailable")

    if not isinstance(processes, list):
        raise HealthcheckFailure("pm2_unavailable")
    process = next(
        (item for item in processes if isinstance(item, dict) and item.get("name") == pm2_name),
        None,
    )
    if process is None:
        raise HealthcheckFailure("pm2_process_missing")
    pm2_env = process.get("pm2_env")
    if not isinstance(pm2_env, dict) or pm2_env.get("status") != "online":
        raise HealthcheckFailure("pm2_process_not_online")

    pid = process.get("pid")
    if not pid:
        raise HealthcheckFailure("runtime_pid_missing")
    try:
        entries = Path(f"/proc/{int(pid)}/environ").read_bytes().split(b"\0")
    except (OSError, TypeError, ValueError):
        raise HealthcheckFailure("runtime_environment_unavailable")

    environment = {
        key: value
        for key, value in (pm2_env.get("env", {}) or {}).items()
        if isinstance(key, str) and isinstance(value, str)
    }
    for entry in entries:
        if b"=" not in entry:
            continue
        key, value = entry.split(b"=", 1)
        try:
            environment[key.decode("utf-8")] = value.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return environment


def _load_app_runtime_token(app_dir: str | Path) -> str:
    app_path = str(Path(app_dir).resolve())
    if app_path not in sys.path:
        sys.path.insert(0, app_path)
    previous_token = os.environ.pop("BOT_TOKEN", None)
    previous_testing = os.environ.pop("TESTING", None)
    try:
        config = importlib.import_module("config")
        token = _clean_runtime_value(getattr(config, "BOT_TOKEN", ""))
    except Exception:
        token = ""
    finally:
        if previous_token is not None:
            os.environ["BOT_TOKEN"] = previous_token
        if previous_testing is not None:
            os.environ["TESTING"] = previous_testing
    return token


def _safe_exception_reason(error: BaseException) -> str:
    if isinstance(error, asyncio.TimeoutError):
        return "timeout"
    if isinstance(error, (ConnectionError, OSError)):
        return "network_error"
    return "client_error"


async def _close_bot(bot: object) -> None:
    session = getattr(bot, "session", None)
    close = getattr(session, "close", None)
    if close is None:
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await asyncio.wait_for(result, timeout=1.0)
    except Exception:
        return


async def _probe_telegram(
    bot_factory: Callable[[str], object],
    token: str,
    proxy: str,
) -> str | None:
    previous_proxy = os.environ.get("TELEGRAM_PROXY")
    os.environ["TELEGRAM_PROXY"] = proxy
    last_reason = "client_error"
    try:
        for attempt in range(HEALTHCHECK_ATTEMPTS):
            bot = None
            try:
                bot = bot_factory(token)
                response = await asyncio.wait_for(
                    bot.get_me(),
                    timeout=HEALTHCHECK_TIMEOUT_SECONDS,
                )
                if response is not None and getattr(response, "id", None) is not None:
                    return None
                last_reason = "empty_response"
            except Exception as error:
                last_reason = _safe_exception_reason(error)
            finally:
                await _close_bot(bot)
            if attempt + 1 < HEALTHCHECK_ATTEMPTS:
                await asyncio.sleep(HEALTHCHECK_RETRY_DELAY_SECONDS)
    finally:
        if previous_proxy is None:
            os.environ.pop("TELEGRAM_PROXY", None)
        else:
            os.environ["TELEGRAM_PROXY"] = previous_proxy
    return last_reason


def run_healthcheck(
    pm2_name: str,
    app_dir: str | Path,
    *,
    runtime_env_reader: Callable[[str], Mapping[str, str]] = _read_pm2_runtime_env,
    bot_factory: Callable[[str], object] | None = None,
    output: Callable[[str], None] = print,
) -> bool:
    try:
        runtime_env = runtime_env_reader(pm2_name)
        token = _clean_runtime_value(runtime_env.get("BOT_TOKEN"))
        if not token:
            # The app loads BOT_TOKEN from its dotenv-backed runtime config; PM2 does not export it.
            token = _load_app_runtime_token(app_dir)
        proxy = _clean_runtime_value(runtime_env.get("TELEGRAM_PROXY"))
        if not token:
            raise HealthcheckFailure("missing_runtime_token")
        if not proxy:
            raise HealthcheckFailure("missing_runtime_proxy")

        if bot_factory is None:
            sys.path.insert(0, str(Path(app_dir).resolve()))
            from telegram_client import create_telegram_bot

            bot_factory = create_telegram_bot

        reason = asyncio.run(_probe_telegram(bot_factory, token, proxy))
    except HealthcheckFailure as error:
        output(f"Telegram via configured proxy: FAIL ({error.reason})")
        return False
    except Exception as error:
        output(f"Telegram via configured proxy: FAIL ({_safe_exception_reason(error)})")
        return False

    if reason is None:
        output("Telegram via configured proxy: OK")
        return True
    output(f"Telegram via configured proxy: FAIL ({reason})")
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pm2-name", required=True)
    parser.add_argument("--app-dir", required=True)
    args = parser.parse_args()
    return 0 if run_healthcheck(args.pm2_name, args.app_dir) else 1


if __name__ == "__main__":
    raise SystemExit(main())
