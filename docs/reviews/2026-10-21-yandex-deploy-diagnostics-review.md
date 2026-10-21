# Ревью диагностики Yandex Cloud-деплоя

> Датированное ревью от 21.10.2026: диагностика облачного деплоя, PR #12, в указанной ниже редакции.
> Текущий [контракт деплоя](../agents/deployment.md) хранится отдельно.
> Первоначальные вердикты и принятые риски сохранены; это не список нынешних нерешённых проблем.

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

## Повторный запуск на dafd02ca

После первого исправления два независимых critical recheck проверили SHA
`dafd02ca75d7494603648430aecf7a3dc054a4a3` и завершились с 0 major/critical.
Отдельный повторный `wunderreview-alt`, invocation
`3cfb4e97-9e14-4e3f-aac9-2c0474ffc2d2`, проверил тот же SHA:
exit code 0, валидные 7/7 пакетов, исходный verdict **needs_changes:
1 blocking, 28 non_blocking**. Полный неизменённый второй отчёт сохранён
локально в `reports/yandex-cloud/wunderreview-diagnostics-pr12-final/report.md`
и `report.json`. Ни успешный exit code, ни ручные решения ниже не меняют исходный
вердикт инструмента.

Все 29 замечаний прочитаны полностью и проверены по окружающему коду.
Ни одно не исключалось только из-за того, что функция существовала до PR.
Из них 25 относятся к тому же generic правилу аннотаций; это подтверждённые
замечания к оформлению сигнатур, а не обнаруженные ошибки выполнения.
В AGENTS/development нет общего требования типизировать весь проект,
и обещанная встроенным правилом конфигурация mypy/pyright отсутствует.
В пределах трёх небольших deploy scripts принято применить правило цельно:
указать реальные входные границы и результаты, без новой системы типов,
сторонних зависимостей, checker, cast, Protocol и TypedDict.

В таблице номера и строки соответствуют второму исходному отчёту на dafd02ca;
после добавления импортов и переноса длинных сигнатур строки могут сместиться.

| № | Место | Решение и проверенный контракт |
| --- | --- | --- |
| 1 | `deploy_gate.py:21 validate_run` | CONFIRMED: `value: object`, `sha: str`, результат `None`; JSON проверяется внутри через record. |
| 2 | `deploy_gate.py:32 select_sha` | CONFIRMED: событие `object` до проверки; окружение `Mapping[str, str]`; результат `str`. Предложенный готовый dict для события не описывал границу JSON. |
| 3 | `deploy_gate.py:50 GitHub.__init__` | CONFIRMED: токен `str`, результат `None`; существующая проверка text сохранена. |
| 4 | `deploy_gate.py:53 GitHub.read` | CONFIRMED: route `str`, результат непроверенного JSON `object`. |
| 5 | `deploy_gate.py:59 GitHub.master_sha` | CONFIRMED: результат `str`, проверенный commit_sha. |
| 6 | `deploy_gate.py:62 GitHub.workflow_run` | CONFIRMED: run_id `str`, результат `object`. Вызов получает строковую группу regex из details_url, а не числовой id из JSON. |
| 7 | `deploy_gate.py:67 GitHub.checks` | CONFIRMED: sha `str`, результат `list[object]`. Элементы страницы ещё не прошли record, поэтому предложенный `list[dict]` преждевременен. |
| 8 | `deploy_gate.py:83 pending_checks` | CONFIRMED: клиент GitHub, `Sequence[object]`, sha `str`, результат `tuple[str, ...]`. Список повторно обходится для каждого required check, поэтому произвольный одноразовый iterator не обещается. |
| 9 | `deploy_gate.py:117 wait_ready` | CONFIRMED: клиент GitHub, sha `str`, время `float`, clock `Callable[[], float]`, sleep `Callable[[float], None]`, результат `bool`. Числовые интервалы и внедряемые clock/sleep сохранены. |
| 10 | `deploy_gate.py:131 authorize` | CONFIRMED: окружение `Mapping[str, str]`, wait_seconds `float`, результат `str`. |
| 11 | `deploy_gate.py:142 main` | CONFIRMED: argv `Sequence[str]` либо `None`, результат `int`; argparse принимает последовательность аргументов. |
| 12 | `deploy_support.py:25 request_json` | CONFIRMED, blocking в исходном отчёте: добавлен `stage: str`; request и результат уже были аннотированы. |
| 13 | `yandex_deploy.py:178 package_digest` | CONFIRMED: Path, sha `str`, результат `str` SHA256. |
| 14 | `yandex_deploy.py:186 known_fields` | CONFIRMED: вход `object`, разрешённые ключи `Iterable[str]`, результат `dict[str, object]` после record. |
| 15 | `yandex_deploy.py:193 environment_variables` | CONFIRMED: текущий JSON `dict[str, object]`; результат `dict[str, str]` после проверки всех значений. |
| 16 | `yandex_deploy.py:202 read_secret_bindings` | CONFIRMED: текущий JSON `dict[str, object]`; результат `list[dict[str, str]]` после проверки каждого поля identifier. |
| 17 | `yandex_deploy.py:218 secret_bindings` | CONFIRMED: Settings и текущий JSON; результат `list[dict[str, str]]`, включая заданную Settings привязку. |
| 18 | `yandex_deploy.py:223 logging_arguments` | CONFIRMED: Settings и вход `object` до known_fields, результат `list[str]`. Предложенный готовый dict для value не описывал его проверку внутри функции. |
| 19 | `yandex_deploy.py:238 preserved_arguments` | CONFIRMED: Settings и текущий JSON `dict[str, object]`, результат `list[str]`. |
| 20 | `yandex_deploy.py:272 description` | CONFIRMED: sha/digest/object_key `str`, результат `str`. |
| 21 | `yandex_deploy.py:276 version_arguments` | CONFIRMED: Settings, текущий JSON и три строки provenance; результат `list[str]`. |
| 22 | `yandex_deploy.py:304 verify_version` | CONFIRMED: Settings, два JSON-объекта, строковые sha/digest/object_key, результат `None`. |
| 23 | `yandex_deploy.py:329 run_identity` | CONFIRMED: run_id и attempt принимают `object`, результат `None`. Функция сама отклоняет None, числа и неверные строки; обещать уже проверенные str на этой границе неверно. |
| 24 | `yandex_deploy.py:335 publish` | CONFIRMED: Settings, YandexCLI, sha `str`, run_id/attempt `object` до run_identity, результат `str`: id проверен verify_version до возврата. |
| 25 | `yandex_deploy.py:390 main` | CONFIRMED: результат `int`, существующие коды 0/1 сохранены. |
| 26 | `test_yandex_deploy.py:413–430` | FALSE POSITIVE: утверждение «без self.subTest» фактически неверно; строка 423 уже содержит `self.subTest(reason=reason)`. |
| 27 | `test_yandex_deploy.py:432–463` | FALSE POSITIVE: строка 436 уже содержит предложенный `self.subTest(invalid_json=invalid_json, head_fails=head_fails)`. |
| 28 | `test_yandex_deploy.py:467–477` | FALSE POSITIVE: строка 473 уже содержит предложенный `self.subTest(stage=stage)` с отдельным mock. |
| 29 | `test_yandex_deploy.py:479–494` | FALSE POSITIVE: строка 485 уже содержит предложенный `self.subTest(stage=stage)` с отдельным mock и side effects. |

Первый инструментальный отчёт признавал subTest в этих тестах и предлагал
перейти на pytest; второй ошибочно утверждает, что subTest отсутствует.
Фактические четыре теста между запусками не менялись. Отдельные подслучаи
unittest продолжают выполняться после assertion failure и показывают
названные параметры. Оснований исправлять эти четыре теста нет.

Заодно проверены все остальные сигнатуры в этих трёх файлах, чтобы одинаковые
пропуски не исправлять по одному. Аннотированы identifier/resource_id,
Settings.secret, settings_from_environment, exchange_token, YandexCLI.__init__
и call; record уточняет только проверенные строковые ключи dict. У callback
NoRedirect.redirect_request явно указаны Request, int/str и None; fp/headers
остаются object, поскольку callback их не читает и безусловно запрещает redirect.
YandexCLI.call возвращает только dict/list, но их значения остаются object
до последующих проверок. Внешние невалидные данные не объявлены проверенными
ради более узкой аннотации.

После этих правок `make check` снова прошёл: 434 теста, 6 прежних пропусков.
Дополнительно сравнение AST с dafd02ca после удаления аннотаций и двух новых
импортов collections.abc подтвердило идентичность тел, значений по умолчанию
и ветвлений всех трёх scripts. Во всех их функциях проверено наличие типов
параметров (кроме self/cls) и результата. Статический checker не запускался
и не добавлялся. Состав пакета и версия 0.5.1 сохраняются.
