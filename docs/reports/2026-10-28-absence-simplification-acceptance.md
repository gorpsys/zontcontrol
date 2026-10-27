# Приёмка упрощения absence с сохранением last_pulse

Дата: 28.10.2026. Объект — фактический кандидат PATCH **0.7.9** рабочего
дерева `refactor/absence-evidence` относительно 0.7.8 на
`7a5e31cc514c65413972eca5af558a3cf1faef3e`.
Локальная реализация и офлайн-приёмка выполнены по
[плану A–C](../plans/completed/20261028012917-absence-simplification-implementation-plan.md).
Это подготовленные исходники: отчёт не подтверждает тег, CI, деплой,
плановый once или физическое восстановление котла.

## Изменение и доказательство

Из формулы absence удалён избыточный `max(last_pulse, evidence_since)`;
сохранены вычитание datetime, `total_seconds() / 60`, округление событий,
порядок проверок, поле last_pulse и все его присваивания. Каноническое
вычисление — [§5 справочника](../reference/anomaly-detection.md#5-проверка-отклика).

При вычислении `last_pulse <= evidence_since`: `_clear` удаляет evidence
и записывает текущее время в last_pulse; новое evidence получает текущее
время строго хронологического потока после предыдущей очистки. При первой
инициализации last_pulse получает ту же точку, если ещё не задан.
Пока evidence существует, last_pulse независимо не сдвигается; pulse-ветка
с присваиванием сразу вызывает `_clear`. Поэтому прежний max выбирал
evidence_since. Runtime assert не добавлен. Буфер 40, RowCache7, профиль,
пороги, reset/grace и публичные контракты сохранены.

Четыре новых unit-теста проверяют полный список событий и метки при
возобновлении запроса, девять причин очистки, обе pulse-ветки (+3/+5 °C),
а также неизменность всех полей при четырёх ранних ошибках входа.
Invariant helper проверяет переходы при наличии evidence. Ожидание неизменности
не распространяется на все ValueError: будущий reset_changed_at в прежнем
детекторе проверяется после обновления last_time.

## Источники и воспроизводимость

Локальные ignored-артефакты находятся в
`reports/absence-implementation-2026-10-28/`: `verify.py`, `provenance.json`,
`replay-results.json`, `detector-state.json`, `edges.json`, `cpu-benchmark.json`,
`validation.json` и два полных difference CSV.gz (assessment/samples).
Это локальные пути, не опубликованные ссылки GitHub. Исходные архивы
и старые результаты не перезаписывались. Новых обращений к ZONT/облаку — 0.
Среда измерения: Python 3.12.3,
Linux-7.0.0-38-generic-x86_64-with-glibc2.39.

Baseline anomaly/monitor получены через git show фиксированной SHA выше,
исполнены в приватных ModuleType и зарегистрированы в sys.modules.
Baseline monitor явно связан с baseline NoHeatDetector; HeatingSample общий.
Кандидат использует настоящие публичные monitor/NoHeatDetector рабочего дерева,
без подмены функций и подготовленных samples. Проверка bytes допускает
в anomaly только формулу и комментарий; остальные четыре модуля совпадают
с базой. Полный SHA кандидата появится после коммита; измеренный dirty-кандидат
однозначно задаётся fingerprint anomaly ниже.

SHA256 исходников, входов и harness до/после всех прогонов совпали
(`fingerprints_unchanged=true`, также после suite):

| Объект | SHA256 |
| --- | --- |
| Baseline anomaly.py | `14c233d9977d5b6ba1e9eed875b46d20472f8cb2885fa9f6851cbad040aa2639` |
| Candidate anomaly.py | `13c6f57621520caaccdc1dfed02f4d62334931db409e23a92689708e7b360056` |
| monitor.py, baseline и candidate | `be08167e5489d67aa61d56901e0e51311249786ec7d03cab80d5ff780ae6aca5` |
| heating.py | `f7075a6cd5a95b67f127c569105201d1584d31a2a4db1454078dfcb92e326f78` |
| heating_adapter.py | `0697f73817e32a1a03ff8d2f6c7f88045459bdd4abdf5f7f91d0272eb4093a10` |
| detection_profile.py | `22e35d70f6c2bafaa58fe42448b2d506e5a0ed4423347274d67daa6e295bae4d` |
| boiler-hypothesis-2026-10-26/merged-series.json.gz | `ce2485b08d00066691eea68f8a5fc4ccd239fb3dd6763f840b19d6b0e35e09c8` |
| boiler-hypothesis-2026-10-26/replay-minutes.csv.gz | `e1d991dfcd0589fc8d326016f27a92cd161fedb0489a556f4569ea723164fdff` |
| boiler-hypothesis-2026-10-26/analyze.py (window) | `53c2d0d1c469520a60331099b1e98fa667a19cd2a09b500c947b192e982f2552` |
| unknown-replay-2026-10-25/primary-history.json | `b9ec98dc0ca091d391d2fe53da93dbf9f70dfff976e205c48c18966552057897` |
| rowcache-implementation-2026-10-26/verify.py (генераторы) | `80a62c9574d66061fe2c0edb7286519a37ef792b5a9394956f530dc47b6cbf97` |
| remaining-optimizations-2026-10-28/recheck.py (не импортирован) | `2cee43c95a991bfd35d16ec711deae78378b7afb18adef6ebdb4a00692f399ad` |
| absence-implementation-2026-10-28/verify.py | `c77fcbddba3a0735623301c0803e38a4cc49ae1bee933e1348ab9063b9cdb2bf` |

## Эквивалентность

На каждое окно обе стороны получили один HeatingHistory/SMART config:
46 минут телеметрии, 120 минут reset, frozen ON до OFF и OFF до появления
переключателя. Сравнивались весь dict с набором ключей, отдельно state/anomaly/reason;
baseline state/reason сверены с CSV. Все различия собирались до конца.

| Проверка | Объём | Отличия |
| --- | --- | --- |
| Полные публичные assess_history | 137591 окно | 0 |
| Все публичные history_samples | 6191595 samples | 0 |
| Краевые истории (169 прежних генераторов + 10 новых случаев) | 179 | 0 |
| Смена config SMART → warning=confirm=5 → SMART | 3 | 0 |
| События/ошибки и все поля состояния после update | 24000 | 0 |
| Проверка invariant при evidence, на каждой стороне | 7627 | 0 |

Два difference CSV.gz содержат 0 строк различий. Проверка состояния:
seed 20261028, шесть потоков по 4000 update, pulse window 2/5/8 ×
require_known_reset False/True, шаги 10/30/60/120/121 с, gap/invalid/reset
и циклы запроса/отклика. Получено 198 ValueError; категории ошибок и все поля
`__dict__`, включая samples, last_pulse, evidence_since, reset_until,
previous_reset, incident_pending и config, совпали. Config сравнивался
по полям dataclass. Два identity-режима и C3 не повторялись: их подготовка
не менялась, bytes соответствующих модулей проверены.

## CPU и ограничения измерения

Основной replay измеряет process_time_ns и perf_counter_ns вокруг полного
публичного assess_history. Порядок baseline/candidate меняется на каждом
окне; построение окна, сравнение и I/O вне таймера. Каждый вызов создаёт
свой детектор и RowCache7. Suite запущена после CPU, без параллельной нагрузки
от тестов. Ниже отношение B/C означает baseline CPU / candidate CPU:
значение меньше 1 означает регрессию кандидата.

| Полный replay | Baseline | Candidate |
| --- | --- | --- |
| Суммарный CPU, с | 92.326518350 | 92.011435601 |
| Медиана CPU, мс | 0.661836 | 0.658994 |
| p95 CPU, мс | 0.779250 | 0.775357 |
| Суммарный wall, с | 92.528618015 | 92.222725335 |
| Медиана wall, мс | 0.663145 | 0.660395 |
| p95 wall, мс | 0.782942 | 0.779290 |

Итого B/C=1.0034243868: CPU кандидата меньше на 0.3413%.
Разрез всех пяти состояний, включая обе регрессии:

| Состояние | Окон | Baseline CPU, с | Candidate CPU, с | B/C |
| --- | --- | --- | --- | --- |
| normal | 128849 | 87.916372161 | 87.616350443 | 1.0034242663 |
| unknown | 5231 | 1.961732209 | 1.966802236 | 0.9974221979 |
| suppressed | 136 | 0.087609501 | 0.087665230 | 0.9993642976 |
| warning | 173 | 0.120833276 | 0.119363449 | 1.0123138784 |
| confirmed_pattern | 3202 | 2.239971203 | 2.221254243 | 1.0084263024 |

Контроль включает первое/среднее/последнее окно каждого состояния (индекс
len//2 с нуля) и saved unknown отдельно. Три последовательных повтора,
в каждом 30 warmup + 1050 timed блоков на окно. Третий вариант candidate_clone —
копия текущих полных anomaly/monitor в таких же приватных namespaces, с явной
связью detector. Это no-op контроль способа загрузки с той же формулой.
Три варианта вращаются по номеру блока modulo 3, нечётные блоки обращены.
Полные результаты во всех контролях совпали, failures=0.

| Повтор | Медиана B/C по 15 окнам | Медиана C/clone по 15 окнам | Saved unknown B/C | Saved unknown C/clone |
| --- | --- | --- | --- | --- |
| 1 | 1.0032348601 | 1.0023788225 | 1.0050956570 | 1.0012009337 |
| 2 | 1.0043313843 | 1.0021531097 | 1.0050553825 | 1.0038850831 |
| 3 | 1.0053505906 | 1.0023479618 | 1.0131933623 | 0.9994147273 |

Все контрольные строки, где B/C либо C/clone меньше 1, сохранены ниже.
Имена означают состояние и индекс внутри его выборки; saved unknown —
отдельная сохранённая история.

| Повтор | Окно | B/C | C/clone |
| --- | --- | --- | --- |
| 1 | normal:0 | 0.9974022648 | 1.0008872095 |
| 1 | normal:64424 | 0.9941437752 | 1.0022131274 |
| 1 | normal:128848 | 0.9988250666 | 1.0054886762 |
| 1 | unknown:2615 | 0.9941163815 | 1.0029166287 |
| 1 | suppressed:0 | 0.9926523058 | 1.0088812265 |
| 2 | normal:0 | 0.9993053679 | 1.0018917240 |
| 2 | unknown:0 | 0.9997575106 | 1.0011110883 |
| 2 | unknown:2615 | 0.9910073226 | 1.0034798679 |
| 2 | suppressed:0 | 0.9996381454 | 1.0041508143 |
| 2 | confirmed_pattern:3201 | 1.0043822013 | 0.9994842052 |
| 3 | normal:0 | 0.9991466690 | 0.9995241048 |
| 3 | normal:64424 | 0.9964614353 | 1.0034678140 |
| 3 | unknown:0 | 0.9973028483 | 0.9997339475 |
| 3 | unknown:2615 | 0.9957844682 | 1.0026248828 |
| 3 | suppressed:0 | 0.9977122149 | 1.0023479618 |
| 3 | saved_unknown | 1.0131933623 | 0.9994147273 |

Полные sum/median/p95 CPU и wall для каждого окна и варианта, все оконные
отношения и регрессии сохранены в cpu-benchmark.json; основные состояния —
в replay-results.json. No-op контроль сам показывает различия порядка 0.2%.
Малый суммарный эффект и отрицательные разрезы не позволяют приписать
весь эффект одной формуле или обещать заметное облачное ускорение.
Положительный CPU-порог не был условием приёмки; замеры ради лучшего числа
не повторялись. Сеть, cold start, облачный runtime и физический результат
здесь не измерялись. Эквивалентность проверена на указанном архиве и
синтетических переходах, а не на всех возможных входах.

## Офлайн-проверки и версия

После шага A: 73 targeted-теста — OK. После CPU шага B:
make check — 544 tests, OK (skipped=6); make package-check — 8 tests, OK;
make cloud-package-check — OK, smoke OK; git diff --check — OK.
Fingerprints после suite сохранены в validation.json.

28.10.2026 перед metadata сверены удалённый master и полный список тегов
через read-only git ls-remote HTTPS: master и v0.7.8 остаются на pinned SHA,
максимальный SemVer — 0.7.8, номер 0.7.9 свободен. PATCH выбран для
совместимого упрощения с прежним контрактом; миграция не требуется.
__version__ и первый выпуск CHANGELOG подготовлены как 0.7.9.
После metadata: `python3 -m unittest tests.test_version tests.test_project_checks
tests.test_release_version` — 19 tests, OK; `make check` — 544 tests,
OK (skipped=6); `make package-check` — 8 tests, OK;
`make cloud-package-check` — app_version=0.7.9, status=ok, smoke=ok,
dirty=true; `git diff --check` — OK. После переноса плана в completed
проверены `make lint` и `git diff --check`. Сборки офлайн и не публикуют версию.

Публикация идёт по [процедуре выпуска](../../scripts/README.md#версии-и-выпуск).
Владелец 28.10.2026 разрешил после готовности PR и зелёного CI merge,
штатный Release tag и deploy-prod 0.7.9 без нового запроса. Эти внешние
этапы требуют отдельного свидетельства и данным отчётом не подтверждаются.
