# Упрощение absence с сохранением last_pulse

Статус: **выполнен: локальная реализация и офлайн-приёмка A–C завершены 28.10.2026**.
Редакция: **1**. Дата: 28.10.2026, Москва.
Область: личный проект `gorpsys/zontcontrol`, пункт 4
[issue #22](https://github.com/gorpsys/zontcontrol/issues/22).

## 1. Цель и исходные решения

Убрать из расчёта absence избыточный max, сохранив решения, события,
исключения и диагностическое состояние NoHeatDetector. Это принятое
владельцем упрощение текущего алгоритма; заметного ускорения облачного once
оно не обещает. Способ реализации уже выбран в обсуждении, альтернативный
алгоритм или удаление last_pulse не предлагаются.

База — проверенный master и v0.7.8 на
`7a5e31cc514c65413972eca5af558a3cf1faef3e`, включающий span-only и RowCache7.
После fetch origin/master совпадает с HEAD; текущая suite — 540 tests,
OK, skipped=6. В дереве есть локальный context-rowcache.md: его не менять,
не stage/commit/push. Если master или набор тегов изменится до начала
реализации, остановиться и перепроверить базу/план до правок.

Уже выполнено отдельное исследование, без изменения продукта:
`reports/remaining-optimizations-2026-10-28/`. Сохранены README.md,
PROTOCOL.md, recheck.py, provenance.json, replay.json, controls.json,
clone-control.json, edges.json, absence-invariant.json и validation.json.
Это локальные ignored-пути, не ссылки на опубликованные GitHub-файлы.

Для C4 при сохранении last_pulse в двух режимах входа прямо сравнены
137591 полное решение и 6191595 samples на режим: 0 отличий. Дополнительно
179 краевых историй, три смены config и 24000 update со сравнением состояния.
На полном replay CPU меньше на 0.49–0.51%; отдельные дополнительные контроли
колеблются в обе стороны. Цель настоящего плана — эквивалентное упрощение,
CPU фиксируется как свидетельство без обязательного положительного порога.
Старые результаты относятся к исследовательскому варианту; фактический
изменённый продукт требуется проверить заново.

SHA256 исходного исследования для сверки:

| Файл | SHA256 |
| --- | --- |
| recheck.py | `2cee43c95a991bfd35d16ec711deae78378b7afb18adef6ebdb4a00692f399ad` |
| provenance.json | `bae3e9563a37b2469dd09dcdf28426dfd9c37f53dfba119571b6772f2b9cd8a3` |
| replay.json | `c70cda160ec5aa61121c6b2488dfaf6d7f348c9532610425143d2cdba3aa1aa3` |
| validation.json | `f346c61d83239ef3c577fd51dbd60cd0160412292a37315ae8aa176303a8f2d4` |

## 2. Архитектура и точное изменение

Поток данных остаётся прежним: Controller → heating_adapter → HeatingHistory
→ monitor.history_samples → HeatingSample → NoHeatDetector.update.
Детектор остаётся чистым вычислительным модулем без I/O и команд.
Данные, публичные сигнатуры, DTO, CLI, DI и lifecycle не меняются.
Новых моделей, endpoints, workers, конфигов или интеграций нет.
Применяются [границы модулей](../../agents/architecture.md#1-состав-и-границы)
и [правила детектора](../../reference/anomaly-detection.md).

В zontcontrol/anomaly.py заменить только выражение:

```python
# Было:
absence = (sample.time - max(self.last_pulse, self.evidence_since)).total_seconds() / 60
# Станет:
absence = (sample.time - self.evidence_since).total_seconds() / 60
```

Рядом добавить короткий комментарий о зависимости от инварианта clear/evidence.
Не менять вычитание datetime, total_seconds, деление, округление событий,
порядок проверок или момент начала evidence. Runtime assert не добавлять.

Обоснование по текущим переходам:

1. _clear удаляет evidence и записывает текущую точку в last_pulse.
   Присваивание last_pulse в pulse-ветке сразу сопровождается _clear.
2. Новое evidence получает текущее время строго хронологического потока,
   после предыдущей очистки. Если last_pulse ещё None, он инициализируется
   той же текущей точкой.
3. Пока evidence существует, last_pulse независимо вперёд не сдвигается.
   Значит, при расчёте absence last_pulse <= evidence_since и max выбирает evidence_since.

Сохраняются поле last_pulse, его инициализация и **все** присваивания,
включая if last_pulse is None и присваивание перед _clear. Его по-прежнему
читают scripts/tune_anomaly.py:55, scripts/backtest_profile.py:135,
scripts/recalibrate_archive.py:105; эти потребители не меняются.

Вне области: пункт 3 / кэш интерполяции, изменение RowCache, буфера 40,
span-only, профиля/порогов, ветвей отклика, reset/grace, pending-return,
нормализации, сетевых запросов, batch и сокращения истории. Никакой
сопутствующей реорганизации или массовой типизации. Если потребуется изменение
этих границ либо публичного контракта — остановиться и доложить владельцу.

## 3. Подход к разработке и регрессионные тесты

Подход regular, как в предыдущем принятом плане: один атомарный шаг включает
формулу, комментарий и содержательные unit-тесты; следующий шаг начинается
только после зелёных проверок предыдущего. Правки — apply_patch, runtime stdlib.
Существующие fixed-expectation тесты сохраняются; проверку текста исходника
или тест, просто повторяющий новую формулу, не добавлять.

В tests/test_anomaly.py добавить класс AbsenceInvariantTests с четырьмя
тестами ниже. Использовать существующие sample и START. Локальный тестовый
helper после каждого успешного update проверяет: при evidence_since != None
last_pulse не None и last_pulse <= evidence_since. Это защита связи переходов,
а не дополнительная проверка в runtime.

### T1. test_absence_timestamps_survive_request_restart

NoHeatDetector с default DetectionConfig и require_known_reset=True.
Минуты 0…50, air=20, target=23, supply=60, reset=False, work=60 кроме
минут 25/26 с work=0. Вода до 24 включительно — 40−0.2×minute, затем 25.
Ожидания проверены на настоящем baseline 0.7.8:

| Точка | Состояние / событие |
| --- | --- |
| До update | last_pulse=None, evidence_since=None |
| 0 и 3 | evidence=None, last_pulse=текущая минута |
| 4 | evidence=4, last_pulse=3 |
| 10 / 24 | warning / confirmed_pattern, absence=6 / 20 минут |
| 25 | Первый нулевой worktime ещё не прекращает пяти-минутный demand |
| 26 | ended/request_ended_or_target_reached, heat_returned=False; evidence=None, last_pulse=26 |
| 27…29 | demand ещё недостаточен; evidence=None, last_pulse=текущая минута |
| 30 | evidence=30, last_pulse=29 |
| 36 / 50 | warning / confirmed_pattern, absence=6 / 20 минут |

Сравнить полный список событий с явно составленными expected dict, включая
время, evidence_since, minutes_without_response, температуры, effective_target
и reset_state_known; не только event.type. Проверить перечисленные метки
и incident_pending, который очистка запроса не объявляет восстановлением.

### T2. test_clear_reasons_preserve_last_pulse

Для каждой строки создать новый detector, прогреть минутами 0…24 с водой25,
air20/target23/supply60/work60/resetFalse. Затем применить действия таблицы.
У всех ended heat_returned=False, incident_pending=True; evidence=None,
baseline=None, last_pulse=время очистки. Сравнивать весь ended dict.

| Сценарий | Действие после 24 | Причина |
| --- | --- | --- |
| Завершение запроса | work=0 на 25 и 26; очистка на 26 | request_ended_or_target_reached |
| Достигнута цель | supply=26 на 25 | request_ended_or_target_reached |
| Gap >120с | Следующая точка — минута28 | telemetry_gap_or_invalid |
| Пропуск температуры | coolant=None на 25 | telemetry_gap_or_invalid |
| Неверный выбранный датчик | selected=False на 25 | telemetry_gap_or_invalid |
| Непригодный worktime | work=61 на 25 | invalid_worktime |
| Неизвестный reset | reset=None на 25 | reset_state_unknown |
| Reset ON | reset=True на 25 | reset_or_restart_grace |
| Событие restart grace | На 25 reset_changed_at=START+24мин30с | reset_or_restart_grace |

### T3. test_pulse_branches_preserve_diagnostics_and_restart_evidence

То же прогревание 0…24. В независимых subTest на минуте25 вода становится
28 либо30: отклик +3 и +5. В обоих случаях полный ended dict имеет причину
heating_response_returned, evidence=None, last_pulse=25; при +3
heat_returned=False и pending остаётся True, при +5 — True и pending=False.
На 26 вода снова25: evidence=26, last_pulse=25, pending сохраняет свой исход.
Это отдельно покрывает обе pulse-ветки, включая присваивание перед _clear.

### T4. test_rejected_inputs_do_not_advance_absence_state

После такого же прогревания проверить ValueError для повторной минуты24,
более ранней23, naive time на25 и reset_active=1 на25. Эти валидации в
baseline выполняются до изменения состояния. До/после сравнить все поля
vars(detector), заменив mutable deque samples на tuple в снимке.
Не распространять ожидание неизменности на другие ошибочные входы:
например, проверка будущего reset_changed_at сейчас идёт после last_time.

## 4. Проверка фактического кандидата

Новый ignored-каталог: `reports/absence-implementation-2026-10-28/`.
Сохранить verify.py, provenance.json, replay-results.json, detector-state.json,
edges.json, cpu-benchmark.json, validation.json и два полных difference CSV.gz
(assessment и samples). Старые исследования не запускать с перезаписью.

### Независимый baseline и входы

Baseline получать через git show **фиксированной SHA 0.7.8** из §1:
полные anomaly.py и monitor.py выполнить в разных приватных ModuleType с
package=zontcontrol. Зарегистрировать приватные module names в sys.modules
для dataclass; настоящее zontcontrol.anomaly не заменять. После загрузки
monitor явно связать его NoHeatDetector с классом **baseline anomaly**:
иначе относительный import незаметно выберет изменённый detector кандидата.
HeatingSample у baseline monitor оставить из неизменённого публичного модуля,
чтобы обе стороны работали с одним типом входа. Config передавать одинаковый.

Кандидат — настоящий zontcontrol.monitor/NoHeatDetector рабочего дерева,
без замены функций, подготовленных samples, factory wrapper или AST-патчей.
До прогона проверить bytes monitor.py, heating.py, heating_adapter.py и
detection_profile.py против pinned базы. Для anomaly.py разрешены только
замена одной формулы и поясняющий комментарий §2. Остальное — стоп и доклад.

Входы: `reports/boiler-hypothesis-2026-10-26/merged-series.json.gz`,
replay-minutes.csv.gz и исходный analyze.window; сохраняются 46 минут
температуры, 120 минут reset, frozen ON до OFF и OFF до появления переключателя.
Saved unknown — `reports/unknown-replay-2026-10-25/primary-history.json`.
Фиксировать baseline SHA, candidate anomaly SHA256, Python/platform и hashes
входов/исходников/harness до и после. Полный source SHA кандидата появится
при коммите; для измеренного dirty кандидата достаточно fingerprint исходника.
Изменение fingerprints во время прогона означает непригодность результатов.

### Эквивалентность и состояние

На всех **137591 окне** вызывать настоящие публичные assess_history обеих
сторон с одним HeatingHistory/SMART config. Сравнивать весь dict и набор
ключей; отдельно state/anomaly/reason. Baseline state/reason сверить с CSV.
Отдельно сравнить все **6191595 samples** публичных history_samples.
Все различия записывать, не останавливаться на первом. Условие — counts
совпали и ровно 0 отличий каждого вида. Два identity-режима и C3 не повторять:
подготовка рядов и identity-семантика этим изменением не затронуты и bytes проверены.

Для 179 краёв использовать прежние 169 fixture/edge_cases из локального
RowCache verify.py только как генераторы данных, плюс те же10 случаев из
нового исследования: spans120/121, четыре typed/quality-дубля и четыре
возврата к исходной истории. Его старый объект BASELINE не использовать.
Три смены config: SMART → warning=confirm=5 → SMART.
Старый remaining-optimizations/recheck.py не импортировать: его top-level
assert ожидает старую формулу в текущем исходнике. Перенести только генерацию
дополнительных10 историй и последовательностей update в новый harness.

Повторить **24000 update**, seed20261028: шесть потоков по4000, три pulse
window2/5/8 × require_known_reset False/True, шаги10/30/60/120/121с,
gap/invalid/reset и циклы request/response из random_detector_check исследования.
Вместо его compiled ABSENCE_DETECTOR использовать реальный класс кандидата.
Оба detector получают одну HeatingSample; после каждого update или ValueError
сравнить события/категорию ошибки и все поля __dict__, в том числе samples,
last_pulse/evidence_since/reset_until/previous_reset/incident_pending/config.
Config сравнивать по полям dataclass (asdict), а не по class identity приватного
baseline-модуля. Проверять invariant при evidence на каждой стороне;
сохранить число проверок и все несовпадения. При различиях завершить сбор
текущей проверки, затем остановить дальнейшие этапы и доложить владельцу.

### CPU как свидетельство

На том же полном replay измерить CPU process_time_ns и wall perf_counter_ns
вокруг только assess_history. Baseline/candidate менять местами на каждом
окне; построение window, сравнение и запись вне таймера. Каждый вызов создаёт
свой detector и RowCache7. Сохранить sum/median/p95 и CPU пяти состояний.
Контроль: первое/среднее/последнее окно каждого состояния (индекс len//2 с нуля),
плюс saved unknown, три последовательных повтора,30warmup+1050timed блоков.

В контроль добавить candidate_clone: полные текущие anomaly/monitor, загруженные
в такие же приватные namespaces, с явной связью detector. Формула этой копии
та же, что у кандидата. Это no-op контроль стоимости исследовательской загрузки;
он не подменяет baseline и не включается в основной полный replay.
Порядок трёх вариантов вращать по номеру блока modulo3, на нечётном блоке
обращать; все полные результаты должны совпасть. Для15 окон показать медиану
оконных отношений median CPU baseline/candidate и candidate/candidate_clone;
saved unknown — отдельно. Также показать все регрессии окон и состояний.

CPU-порог ускорения не является условием функциональной приёмки этого
упрощения. Малые/отрицательные результаты и различия no-op контроля сохранять;
не повторять benchmark ради положительного числа и не расширять изменение
до кэшей/буфера. Suite запускать после CPU, не параллельно.

## 5. Шаги реализации и приёмки

При разрешении реализации создать от проверенного master ветку
`refactor/absence-evidence` по конвенциям проекта. Каждый шаг полностью
завершается до следующего; ошибку проверки не заменять отчётом одного
изолированного теста. Изменение области требует доклада и правки плана.

### Шаг A. Формула и защита переходов

**Файлы:** Modify zontcontrol/anomaly.py, tests/test_anomaly.py.

- [x] Выполнить только замену и комментарий §2; сохранить все записи last_pulse.
- [x] Добавить четыре теста T1–T4 из §3 с invariant helper, полными событиями и состоянием ошибок.
- [x] Выполнить python3 -m unittest tests.test_anomaly tests.test_monitor tests.test_heating_regressions.
- [x] Доложить результат шага A до архивной приёмки.

### Шаг B. Проверка продукта против 0.7.8

**Файлы:** Create новые ignored-артефакты §4; продуктовые файлы не расширять.

- [x] Реализовать отдельный harness §4 с настоящим pinned baseline и проверкой fingerprints.
- [x] Получить 0 отличий полных результатов/samples, краёв и состояния на24000update.
- [x] Записать полный CPU и три контроля, включая no-op и все отрицательные разрезы.
- [x] После замеров выполнить make check, make package-check, make cloud-package-check, git diff --check.
- [x] Доложить counts, fingerprints, CPU и результаты всех проверок владельцу.

### Шаг C. Документация и подготовленные metadata

**Файлы:** Modify docs/reference/anomaly-detection.md (§5 «Проверка отклика»),
docs/README.md, zontcontrol/__init__.py, CHANGELOG.md; Create
docs/reports/2026-10-28-absence-simplification-acceptance.md; этот план.

- [x] В справочнике кратко описать invariant и вычисление от evidence_since; пороги не менять.
- [x] Сохранить в acceptance-отчёте доказательство, fingerprints/counts, фактический CPU и его ограничения.
- [x] Подготовить PATCH0.7.9 в __version__/CHANGELOG по текущей базе; это не публикация. Если номер занят новой базой/тегом — стоп и пересмотр плана.
- [x] Выполнить python3 -m unittest tests.test_version tests.test_project_checks tests.test_release_version, затем make check, make package-check, make cloud-package-check и git diff --check. Повтор packaging оправдан новыми metadata0.7.9; новые тесты, повторяющие номер версии, не нужны.
- [x] После выполнения A–C перенести план в completed, исправить все входящие/исходящие ссылки, выполнить make lint и git diff --check; доложить владельцу.

План хранит только выполненность локальной реализации/приёмки A–C;
публикация и внешние действия ниже не объявляются состоявшимися при переносе.
Итоговые counts, fingerprints, CPU и проверки —
[приёмка 28.10.2026](../../reports/2026-10-28-absence-simplification-acceptance.md).

## 6. Следующие внешние этапы

После проверки владельцем: codereview полного диффа (холодный читатель,
WunderReview и перепроверка всех замечаний), коммит и PR в master. В описание
внести фактические counts, ссылку на acceptance и честный небольшой CPU-эффект.
Перед коммитом проверить весь staged diff на секреты, исключить context-rowcache.md
и сырые reports. Commit trailer и PR attribution — по инструкциям сессии.
После коммита release_version check против актуальной SHA origin/master и тегов.

CI на актуальной SHA: Lint и Offline checks, включая Python3.11/3.12.
Дополнение 28.10.2026: владелец явно разрешил после готовности PR и зелёного CI
выполнить merge, штатный Release tag и deploy-prod для 0.7.9 без нового запроса.
Ранее эти действия не были разрешены; технические критерии плана не меняются.
После разрешённого выпуска подтвердить tagged/merged SHA, успешный deploy
и первый завершённый плановый once по безопасным логам, без искусственного
сброса/аварии. Ручные yc требуют отдельного подтверждения по AGENTS.md;
штатный согласованный workflow не требует подтверждения каждой своей команды.
Внешние результаты оформить отдельным свидетельством, без обещания физической
исправности котла или измеренного облачного ускорения.
