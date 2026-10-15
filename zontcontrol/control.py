"""Control CLI: offline preview by default, explicit --execute for sending."""

import argparse
import json
import os
import sys

from .client import API_URL, ZontClient, ZontError
from .commands import prepare_command
from . import __version__


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Подготовить или отправить команду ZONT")
    parser.add_argument("--version", action="version", version=f"zontcontrol {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--device-id", type=int, required=True, help="ID прибора")
    common.add_argument("--element-id", type=int, required=True, help="ID контура, режима или кнопки")
    common.add_argument("--execute", action="store_true", help="Отправить команду; по умолчанию только предпросмотр")
    common.add_argument("--contact", help="Контактный email для API")
    common.add_argument("--timeout", type=float, default=15, help="Таймаут запроса в секундах")
    commands = parser.add_subparsers(dest="action", required=True)
    temp = commands.add_parser("set-temp", parents=[common], help="Задать целевую температуру")
    temp.add_argument("--temperature", type=float, required=True)
    mode = commands.add_parser("activate-mode", parents=[common], help="Активировать режим отопления")
    mode.add_argument("--duration", type=int, help="Длительность режима в минутах")
    commands.add_parser("press-button", parents=[common], help="Нажать простую кнопку")
    switch = commands.add_parser("set-switch", parents=[common], help="Установить состояние переключателя")
    switch.add_argument("--state", choices=["on", "off"], required=True)
    regulator = commands.add_parser("set-regulator", parents=[common], help="Задать значение регулятора")
    regulator.add_argument("--value", type=float, required=True)
    commands.add_parser("run-scenario", parents=[common], help="Запустить сценарий")
    guard = commands.add_parser("set-guard", parents=[common], help="Включить или выключить охрану")
    guard.add_argument("--state", choices=["on", "off"], required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        parameters = {}
        if args.action == "set-temp":
            parameters["target_temp"] = args.temperature
        if args.action == "activate-mode" and args.duration is not None:
            parameters["duration"] = args.duration
        if args.action == "set-switch":
            parameters["target_state"] = args.state == "on"
        if args.action == "set-regulator":
            parameters["target_value"] = args.value
        if args.action == "set-guard":
            parameters["enable"] = args.state == "on"
        command = prepare_command(args.action, args.device_id, args.element_id, **parameters)
        if not args.execute:
            result = command.preview(API_URL)
        else:
            token = os.environ.get("ZONT_TOKEN")
            if token is None or not token.strip():
                raise ValueError(
                    "Не задана ZONT_TOKEN. Получите токен: python3 -m zontcontrol.get_token "
                    "--contact YOUR_EMAIL; затем export ZONT_TOKEN='полученный_токен'."
                )
            contact = args.contact or os.environ.get("ZONT_CONTACT")
            if not contact:
                raise ValueError("Укажите --contact или переменную окружения ZONT_CONTACT.")
            client = ZontClient(contact, token=token, timeout=args.timeout)
            result = client.apply_command(command, execute=True)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ZontError, ValueError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        print("Операция прервана; перед повтором проверьте состояние прибора.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
