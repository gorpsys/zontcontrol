# Ревью реализации issue #5

Проверенная база: `9f157f8e4ca15891dd9bdd7c80eb6d2880031714`.
Проверенный SHA PR #10: `f37d0e7180a30425a2a35292a1909efca1a94947`.
Дата: 20.10.2026. План: [предметный вход отопления](../plans/20261020120717-typed-heating-input-implementation-plan.md).

## Источники и границы проверки

Холодное ревью выполнено пятью независимыми специалистами: quality,
implementation, testing, simplification, smells. Первые четыре не нашли
подтверждённых замечаний; smells нашёл один MAJOR, перечисленный ниже.
Они проверили исходники, тесты, план, контракты и документацию статически;
архивный replay, тесты и сеть эти агенты не запускали.

Первый WunderReview завершился с exit 0 и сообщил 25 findings
(1 blocking, 24 non-blocking), но его артефакты в `/tmp` потеряны при
перезапуске. Все исходные 25 замечаний индивидуально не разобраны;
настоящий отчёт не утверждает обратного. Их заменяет повторный прогон
на том же SHA, а не реконструкция потерянного результата.

Повторный WunderReview: invocation `18d76b8d-29ed-46f7-8952-2fbb2517d685`,
`reviewed_at=2026-10-20T12:25:24Z`, model `Qwen-Coder`, exit 0,
`command_complete/pass`; verdict отчёта `needs_changes`.
Полные локальные артефакты: `reports/issue-5/wunderreview-recovered/report.json`
и `report.md`. Отчёт содержит 5 findings (1 blocking, 4 non-blocking),
40 not-applicable и 21 compliant. Все пять findings прочитаны целиком;
для каждого проверен код с окружающими 20–30 строками и точное правило
с исключениями через `wunderreview-alt rules show`.

Ограничения контекста WunderReview: `token_estimate=221395`,
`budget_tokens=235000`, safety margin 6%. Файл
`tests/fixtures/heating-assessment-cases.json` превышает потолок 20000 токенов
и был усечён до hunks (`truncation_strategies=json`). Из пакета исключены
план и три его прежних отчёта ревью. Поэтому этот прогон не является
полным чтением fixture или самостоятельной проверкой архивного корпуса.
Численный охват прежней проверки описан отдельно в
[отчёте регрессий](../reports/2026-10-20-typed-heating-regressions.md).

## Полный реестр замечаний

Все номера строк ниже относятся к проверенному SHA `f37d0e7`, до исправлений.
CONFIRMED означает подтверждённое и исправленное замечание;
REJECTED — отклонённое по проверенным фактам.

| ID / источник | Правило / уровень | Место | Проблема | Статус |
| --- | --- | --- | --- | --- |
| C1, cold smells | Работоспособность CLI, MAJOR | scripts/backtest_profile.py:354 | `main()` обращается к удалённому `client_boundary_crosschecks` и падает после записи файлов. | CONFIRMED, совпадает с W1 |
| W1, повторный WunderReview | py-code-style/no-debug-prints, non_blocking | scripts/backtest_profile.py:353–354 | Вывод старого ключа вызывает KeyError; предложены актуальные ключи либо logging. | CONFIRMED для KeyError; logging не требуется |
| W2, повторный WunderReview | py-error-handling/exception-chaining, non_blocking | scripts/heating_archive.py:84–85 | `raise ValueError(...) from None` скрывает причину ошибки локального архива. | CONFIRMED |
| W3, повторный WunderReview | py-git/ci-green, blocking | MR:0 | В контексте ревью нет структурированного CI-блока; требуется успешный CI на актуальном SHA. | REJECTED как дефект проверенного SHA |
| W4, повторный WunderReview | py-tests/assert-whole-objects, non_blocking | tests/test_heating_archive.py:44–50 | Поля HeatingSeries сравниваются по отдельности вместо полного dataclass. | CONFIRMED |
| W5, повторный WunderReview | py-tests/assert-whole-objects, non_blocking | tests/test_heating_input.py:128–136 | Controller-адаптер проверяется отдельными полями HeatingSeries. | CONFIRMED |

### C1 и W1: настоящий сбой CLI

`run()` уже возвращает `input_policy_comparisons` и
`input_policy_differences` (строки 184–186), а обычный `main()` после
`enrich_report()` и `write_report()` всё ещё читает удалённый ключ.
Это не ветка `--summarize-only`: поддержка чтения старого сохранённого
отчёта в `write_report()` намеренно остаётся отдельной.
Правило `no-debug-prints` явно исключает stdout-контракт CLI;
штатный `print` не заменяется logging.

Исправлена только завершающая сводка обычного `main()` на текущие два ключа.
Тест выполняет реальный `main()` на малом синтетическом gzip-архиве во
временном каталоге, без подмены `run`, загрузчика или формирования отчёта.
До исправления он падал с `KeyError: client_boundary_crosschecks`, после —
проверяет JSON-сводку, сохранённый JSON, Markdown и минутный CSV.

### W2: локальная причина ошибки архива

`from_archive_packet` оборачивает структурные KeyError/TypeError/ValueError/
OverflowError в один публичный ValueError. Это чистый локальный парсер
без HTTP и без вывода ошибок. Отдельного security-контракта скрытия его
traceback в плане и контрактах нет. Исключение правила для намеренного
сокрытия внутренностей от внешнего клиента здесь не применяется.

Публичный тип и текст ValueError сохранены, исходная причина передаётся
через `from error`. Транспортная санитизация не меняется. Новый тест
удаляет `requested_period`: до исправления `__cause__` отсутствует,
после содержит KeyError с этим именем поля. Произвольные API-ошибки
не печатаются; сетевых обращений парсер не выполняет.

### W3: CI проверен в GitHub

Отсутствие CI в контексте WunderReview действительно не доказывает успех.
Фактический статус проверен отдельно через `gh pr view 10` с
`headRefOid/statusCheckRollup` и `gh run view 39419038535`.
Обе проверки относятся к полному SHA `f37d0e7180a30425a2a35292a1909efca1a94947`.
[Запуск GitHub Actions](https://github.com/gorpsys/zontcontrol/actions/runs/39419038535)
завершён с `success`; Lint, Offline checks (Python 3.11), Offline checks
(Python 3.12) и общий Offline checks имеют SUCCESS.
Поэтому это ограничение данных ревью, а не красный CI проверенной версии.
Исправления создают новый SHA: до слияния требуется CI именно этого SHA;
локальные проверки не объявляются заменой CI.

### W4 и W5: целые предметные ряды

HeatingSeries — frozen dataclass с points, source, unit, value_type, kind
и incomplete_reasons. В указанных тестах перечислялись текущие поля,
но не сравнивался весь объект. Недетерминированных полей здесь нет,
поэтому исключение правила для них не требуется. Сравнения заменены
на `assertEqual(actual, HeatingSeries(...))` с полным ожидаемым рядом.
Сохраняются проверки явных привязок, исходных значений и неизменности входа.

## Результат и проверки исправлений

Всего 6 упоминаний замечаний: один cold-дефект совпадает с W1.
Уникальных замечаний 5: 4 подтверждены и исправлены, 1 отклонено.
Полный исходный потерянный набор из 25 в эти числа не входит.

Офлайн-проверки: 31 тематический тест; `make check PYTHON=python3` —
351 тест, 4 прежних пропуска; `make package-check PYTHON=python3` —
6 проверок установленного wheel. Версия остаётся 0.4.0.
Численные пути monitor, детектор, профиль и исходные архивы не менялись,
поэтому архивное сравнение заново не запускалось. План ещё не переносится;
повторное критическое ревью и CI нового SHA выполняются следующим этапом.
