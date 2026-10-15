"""Interactive utility to issue a ZONT API token and print it to stdout."""

import argparse
import getpass
import os
import sys
import warnings

from .controller.auth import create_token
from .controller.contracts import ControllerError
from . import __version__


def _prompt(label: str) -> str:
    # Prompts go to stderr so stdout contains only the resulting token.
    print(label, file=sys.stderr, end="", flush=True)
    return input().strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Получить API-токен ZONT")
    parser.add_argument("--version", action="version", version=f"zontcontrol {__version__}")
    parser.add_argument("--contact", help="Контактный email для X-ZONT-Client")
    parser.add_argument("--client-name", default="zontcontrol", help="Название приложения")
    parser.add_argument("--timeout", type=float, default=15, help="Таймаут в секундах (15)")
    args = parser.parse_args()
    try:
        contact = args.contact or os.environ.get("ZONT_CONTACT")
        if not contact:
            contact = _prompt("Контактный email для API: ")
        login = _prompt("Логин ZONT: ")
        # Refuse plaintext fallback when no terminal is available.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Пароль ZONT: ")
        token = create_token(contact, login, password, client_name=args.client_name,
                             timeout=args.timeout)
        print(token)
        return 0
    except getpass.GetPassWarning:
        print("Нужен терминал со скрытым вводом пароля.", file=sys.stderr)
    except (ControllerError, ValueError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        print("Ввод отменён.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
