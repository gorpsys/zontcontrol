# Ревью диагностики Yandex Cloud-деплоя

Дата: 21.10.2026. PR: <https://github.com/gorpsys/zontcontrol/pull/12>.
Проверенный исходный SHA: `2d83024c7431ddc756a7af8cd2e86a7f97531fa9`.
Версия PR остаётся `0.5.1`.

## Исходный результат

Пять независимых проверок плана — quality, implementation, testing,
simplification и conventions — завершились без подтверждённых замечаний.
Они проверяли весь diff относительно `origin/master`.

Отдельный запуск `wunderreview-alt` завершился с exit code 0 и валидными
7/7 пакетами, invocation `ceab9800-6c1d-4feb-98c8-2b58233dc96a`.
Его исходный verdict — **needs_changes: 1 blocking, 4 non_blocking**.
Это статус исходного отчёта, а не утверждение о чистом результате инструмента.
Полные неизменённые локальные артефакты сохранены в
`reports/yandex-cloud/wunderreview-diagnostics-pr12/report.md` и `report.json`.
Ручная проверка ниже рассматривает все пять замечаний по фактическому коду
с окружающим контекстом и действующим инструкциям проекта.

## Решения по замечаниям

1. **CONFIRMED — `py-code-style/type-hints`, blocking в исходном отчёте.**
   [scripts/yandex_deploy.py](../../scripts/yandex_deploy.py), строки 159–175:
   новая функция `cli_failure_reason` действительно не имела аннотаций.
   Полный текст встроенного правила, прочитанный командой
   `wunderreview-alt rules show py-code-style/type-hints --no-user-config`,
   содержит «Все функции, методы, атрибуты классов и поля dataclass имеют
   аннотации типов», `applies_to: **/*.py`, `grouping: always`.
   Соседние функции без аннотаций и отсутствие mypy/pyright в pyproject
   ограничивают утверждение инструмента о конвенциях всего проекта, но
   не устраняют это точечное замечание к новой функции. Добавлены
   `stderr: bytes` и `-> str`: `subprocess.run(capture_output=True)`
   возвращает байты, поиск выполняется по фиксированным байтовым образцам,
   результатом всегда служит строковая метка. Это исправление оформления
   контракта; поведение, набор меток и обработка ошибок не изменены.

2. **FALSE POSITIVE — `py-tests/parametrize-table-tests`, non_blocking.**
   [tests/test_yandex_deploy.py](../../tests/test_yandex_deploy.py), строки
   413–430: цикл находится внутри `unittest.TestCase` и каждый сценарий
   выполняется в `self.subTest(reason=reason)` с отдельным контекстом mock.
   При падении unittest показывает параметр reason и продолжает остальные
   сценарии. Это не цикл без идентификаторов, описанный в правиле.
   Проверка точного сообщения исключает произвольный stderr целиком.

3. **FALSE POSITIVE — то же правило, non_blocking.**
   `tests/test_yandex_deploy.py`, строки 432–463: все четыре комбинации
   upload/HEAD уже именованы через
   `self.subTest(invalid_json=invalid_json, head_fails=head_fails)`.
   В каждой комбинации создаются новые `FakeYC`, CLI и mock subprocess;
   состояния вызовов не разделяются между комбинациями. Проверяются
   безопасная диагностика, ровно один upload, один HEAD и отсутствие create
   после неизвестного исхода upload.

4. **FALSE POSITIVE — то же правило, non_blocking.**
   `tests/test_yandex_deploy.py`, строки 467–477: каждая HTTP-граница имеет
   имя `self.subTest(stage=stage)` и собственный mock opener. Точное сравнение
   ошибки проверяет одновременно название границы и сокрытие тела/URL ошибки.

5. **FALSE POSITIVE — то же правило, non_blocking.**
   `tests/test_yandex_deploy.py`, строки 479–494: две разные стадии обмена
   OIDC уже выделены `self.subTest(stage=stage)`; mock и side effects
   пересоздаются для каждой стадии. Отчёт unittest различает GitHub OIDC
   и Yandex IAM exchange, а точное сообщение не содержит provider error.

Полный текст `py-tests/parametrize-table-tests`, прочитанный через
`rules show ... --no-user-config`, рекомендует pytest вместо «самодельного
цикла по списку словарей без идентификаторов». Здесь используются именованные
subTest стандартной библиотеки. [AGENTS.md](../../AGENTS.md), пункт 8,
требует unittest и согласования новых зависимостей;
[development.md](../agents/development.md), строки 39–42, закрепляет
`make test — unittest`, а [Makefile](../../Makefile) запускает unittest
discover. Поэтому переписывание этих четырёх тестов на pytest не соответствует
выбранному тестовому контуру. Их отклонение основано на фактической диагностике
и изоляции сценариев, а не только на существовании похожих старых тестов.

## Проверка исправления и границы

Исправление исходного кода — только аннотация сигнатуры. Новые зависимости,
повторы команд, изменения fail-closed поведения, вывод приватных ошибок,
облачные запросы, повтор deploy, ZONT, таймер, теги и слияние не добавлялись.
Пакетный состав не меняется. После исправления `make check` прошёл:
lint и 434 теста unittest, 6 существующих installed-only пропусков.
Существующие тесты проверяют реальный байтовый stderr и безопасные строковые
метки, поэтому отдельный тест на наличие аннотации не нужен.
