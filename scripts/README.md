# Технические команды

Инструменты разработки и исследований zontcontrol. Команды выполняются
из каталога проекта. Использование приложения — [README](../README.md).
Runtime Python >=3.11, только stdlib; сборка использует setuptools.

## Разработка и проверки

```sh
make setup
make check
make package
make package-check
make cloud-package-check
```

make setup создаёт .venv и устанавливает пакет в editable-режиме.
PYTHON выбирается из .venv/bin/python, иначе python3; можно задать явно:

```sh
make check PYTHON=python3
python3 -m unittest discover -s tests -v
```

| Цель | Результат |
| --- | --- |
| setup | .venv и editable-установка |
| lint | Синтаксис Python, пробелы, локальные Markdown-ссылки, метаданные |
| test | Офлайн unittest; реальные ZONT-команды не отправляются |
| check | lint + test |
| package | Wheel в dist/ |
| package-check | Smoke установленного wheel вне checkout, без ZONT |
| yandex-archive | Минимальный once ZIP в build/yandex-function.zip; нужен ZONT_CONTACT |
| cloud-package-check | Проверка состава/manifest и offline ZIP smoke вне checkout |
| clean | Удаление только build/, dist/, zontcontrol.egg-info/ |

Полноценный сторонний линтер и coverage gate пока не подключены.
make package использует --no-deps --no-build-isolation. В выбранном Python
заранее должны быть setuptools>=68 и wheel; цель не скачивает build tools.
Для подготовленного офлайн-окружения прямая эквивалентная команда:

```sh
python3 -m pip wheel --no-deps --no-build-isolation --wheel-dir dist .
```

В wheel включается только zontcontrol и package metadata; scripts, tests,
docs, config-примеры, рабочие конфиги, локальные архивы и cloud-шаблоны
не входят в исполняемый пакет. Для запуска wheel подготовьте cwd/config
отдельно — [инструкция](../README.md#установка-и-конфиги).
Технические зависимости сборки не являются зависимостями runtime.
make package сначала удаляет генерируемые build/dist/egg-info, чтобы ранее
удалённые модули не попадали в новый wheel из старого build/lib. Для прямой
команды pip wheel перед повторной сборкой выполнить make clean.

## CI

[Project checks](../.github/workflows/checks.yml) запускается для PR,
push master и вручную. Обязательные имена checks: Lint и Offline checks.
Последний сначала проверяет номер выпуска относительно базы PR/тегов, затем
Python 3.11 и 3.12, минимальный облачный ZIP, сборку/установку wheel и CLI
вне checkout. ZIP проверяется внутри прежнего Offline checks, без нового
обязательного имени check.
[Release tag](../.github/workflows/release-tag.yml) отдельно публикует
аннотированный тег после push master, не обращаясь к ZONT и не выполняя деплой.
Токены ZONT для CI не нужны, тесты не обращаются к реальному устройству.
Защита master должна требовать checks текущего SHA и PR, включая администраторов.
Удалённый репозиторий — [gorpsys/zontcontrol](https://github.com/gorpsys/zontcontrol).
Фактический run Python 3.11/3.12 и ограничения записаны в
[истории реализации](../docs/history/2026-10-16-architecture-implementation.md).
Пройденный CI относится к указанному SHA; новый коммит требует своих checks.

## Удалённая координация

Скрипты калибровки/статистики монополии удалены вместе с runtime.
Исторические измерения и причины —
[решение](../docs/history/2026-10-20-remove-synchronization.md).
Текущие исследования детектора и истории приведены ниже.

## Исследования и калибровка

Архивы и сгенерированные результаты сохраняются локально в reports/.
Пути оставлены совместимыми со старыми скриптами. Полные архивы не
публикуются по умолчанию. Выбранные итоговые отчёты — docs/reports/.

Сбор истории требует ZONT_TOKEN и ZONT_CONTACT и выполняет чтение API:

```sh
python3 scripts/collect_history.py --days 21 --output reports/history-3w
```

Суточные ответы дробятся до часа, если сервер сокращает диапазон.
Контрольные точки записываются в manifest.json; повторный запуск продолжает
тот же период, а не сдвигает его к текущему времени. Новый период/прибор —
новый output. Сборщик повторяет read-only запрос при 429 с ограниченной
задержкой; управляющие запросы так не повторяются.

Фиксированный период:

```sh
python3 scripts/collect_history.py --from '2026-07-22T08:38:56+00:00' --to '2026-09-22T09:23:56+00:00' --output reports/history-prior-2months
```

Параметры устройства по умолчанию привязаны к текущему SMART 2.0:
700877 и контур 20496. Для другого прибора задавайте --device-id/--circuit-id.
Остальные исследовательские скрипты также используют профиль текущей системы;
они не являются универсальным cloud-конфигом.

Статистика суток (читает API):

```sh
python3 scripts/analyze_history.py --device-id 700877 --circuit-id 20496 --hours 24
```

Добавьте --plot reports/heating-24h.svg: analyze_history создаёт три SVG
heating-24h-air.svg, heating-24h-coolant.svg и heating-24h-worktime.svg
через renderer приложения на stdlib. Никакого matplotlib для этого пути нет.
Отдельный исторический plot_anomalies.py остаётся исследовательским
matplotlib-скриптом и не является runtime приложения.

Разбор локального трёхнедельного архива:

```sh
python3 scripts/analyze_3weeks.py --help
python3 scripts/validate_one_shot.py --help
python3 scripts/backtest_profile.py --help
```

Сначала смотреть --help: вывод и имена отчётов разных поколений отличаются.
backtest_profile использует текущий профиль; его новый результат нужно
сохранять отдельно от исторической проверки профиля 4 минуты.

validate_one_shot, tune_anomaly, backtest_profile и validate_recalibration
передают monitor предметную HeatingHistory. heating_window строит её напрямую
из семи объединённых load_series рядов за [T-46 минут,T], сохраняя исходную
точку до start; расчётную 45-минутную сетку выбирает monitor. Старые потери
quality и дробных секунд в load_series сохранены. Пустой срез reset здесь
означает None; явно существующий пустой reset в archive packet сохраняется
как пустой HeatingSeries. Архивные packet разбирает scripts/heating_archive,
а runtime использует heating_adapter для Controller DTO.

Повторная калибровка требует уже имеющихся reports/history-prior-2months,
reports/history-3w и исходных локальных отчётов. Эти команды работают
офлайн, но не входят в обычный CI без приватного архива:

```sh
python3 scripts/recalibrate_archive.py
python3 scripts/validate_recalibration.py
```

Первый скрипт подбирает конфигурацию и сохраняет выбранную в JSON; он не
переписывает detection_profile.py. Второй проверяет установленный профиль
и его совпадение с выбранным: каждая минута replay оценивается новым детектором.
FastTraces и replay по frames используют прежнюю входную политику; их
совпадение не доказывает равенство новому нормализованному monitor. Проверки
полного monitor используют HeatingHistory и показывают различия этих политик.
validate_recalibration перезаписывает reports/recalibration-83days.json;
это не безобидная smoke-проверка. Переход на новую нормализацию проверялся
разовыми локальными скриптами без добавления постоянного CLI —
[отчёт](../docs/reports/2026-10-20-typed-heating-regressions.md).
Не перезаписывать исходные отчёты/архивы эксперимента без сохранения новой
версии результата. Итог — [калибровка](../docs/reports/2026-10-13-anomaly-calibration.md),
критерии — [алгоритм](../docs/reference/anomaly-detection.md).

## Облачный запуск и деплой

Реализованы handler и минимальный stdlib ZIP для boiler_reset once.
Сборка не обращается к ZONT или облаку и не читает токен. Контакт задаётся
через ZONT_CONTACT; это не секрет, но рабочий email владельца в Git не хранится.
Для офлайн-примера используйте синтетический контакт:

```sh
ZONT_CONTACT=cloud-package@example.invalid make yandex-archive
make cloud-package-check
```

Архив build/yandex-function.zip содержит явный allowlist Python-модулей
once-сценария, корневой handler.py, три config/*.json и build-manifest.json.
CLI, charts, get_token, __main__, тесты, scripts и локальные данные исключены.
Исходные конфиги берутся только из deploy/cloud-config/*.json. Сборщик
подставляет контакт, затем использует существующие валидаторы контроллера,
регистратора и boiler_reset. Симлинки разрешённых исходников/профиля запрещены.
Рабочие config/*.json не читаются. Пустой/некорректный контакт, конфиг или
неполный набор исходников прерывают сборку до замены предыдущего ZIP.

Manifest schema_version=1 содержит app_version, source_sha из HEAD, dirty
и files — SHA256 каждого member, кроме manifest. CLOUD_PACKAGE_SHA, если
задан, должен совпадать с полным HEAD. Локальная сборка допускает dirty=true;
для деплоя нужен dirty=false и SHA выбранного коммита. Временный
архив проверяется на состав, типы файлов, CRC, хеши и точные байты, затем
атомарно заменяет результат. build/ вне Git; make package/clean удаляет его.

make cloud-package-check сам собирает временный ZIP с синтетическим
контактом, сверяет его с checkout и запускает импорт/один настоящий once
с подставным Controller вне checkout. Дочерний Python работает с -I -S,
без ZONT_TOKEN, site-packages и сети. Облачный handler только импортируется;
его production-вызов для диагностики не выполняется.

Workflow [deploy-prod](../.github/workflows/deploy-prod.yml) и stdlib helpers
реализованы. Отдельные function/deploy SA/секрет созданы, metadata активной
версии секрета проверены: [отчёт](../docs/reports/2026-10-20-yandex-cloud-provisioning.md).
Repository variables проверены; выпуск 0.5.0 опубликован. Первый deploy
остановился на OIDC subject, после исправления credential — на upload ZIP
с неизвестным исходом, до создания версии. Позднее рабочий метод загрузки
образца восстановлен; 0.7.0 опубликована, таймер ACTIVE, плановые вызовы
и ротация логов подтверждены: [приёмка](../docs/reports/2026-10-21-yandex-cloud-acceptance.md).
Полная инструкция настройки —
[GitHub Actions → Yandex Cloud](../docs/reference/github-yandex-cloud-howto.md).
Workflow принимает успешный Project checks push-master либо workflow_dispatch
на master. Gate проверяет актуальный SHA gorpsys/zontcontrol, Lint/Offline checks,
GitHub Actions producer и workflow run с точным checks.yml/push/master/repo/SHA.
Commit statuses не заменяют checks. Перед OIDC проверка повторяется в workflow
и внутри helper; checkout и manifest закреплены на этом SHA.

Обязательные repository variables: CLOUD_ID, FOLDER_ID, SA_ID, FUNCTION_ID,
FUNCTION_SA_ID, BUCKET, LOCKBOX_SECRET_ID, LOCKBOX_VERSION_ID,
YC_OIDC_AUDIENCE, ZONT_CONTACT. [Пример](../deploy/yandex.example.json)
содержит подтверждённые ID ресурсов и версии секрета; контакт остаётся null,
helper пример не читает. GitHub запрещает префикс GITHUB_ в repository variables:
workflow передаёт YC_OIDC_AUDIENCE во внутренний env GITHUB_OIDC_AUDIENCE helper.
Runtime/handler/256MB/420s/concurrency1 и binding
z_token → ZONT_TOKEN фиксированы в workflow; версия Lockbox закреплена ID.
Пустые/некорректные настройки отклоняются до OIDC. GitHub AUD и новый deployment
SA_ID для IAM exchange различаются. yc 1.40.0 получает IAM только через child
env, временный явный config без credentials удаляется на всех выходах,
HOME не меняется, команды имеют конечный timeout и --retry 0.
HTTP-диагностика указывает фиксированную стадию, yc — exit-код и категорию
ошибки. При ошибке upload деплой сразу останавливается без дополнительного
HEAD, повторов или создания версии; произвольные ответы провайдера не выводятся.

Helper читает функцию по FUNCTION_ID и сверяет folder. Только успешный пустой
version list разрешает первую версию; иначе требуется валидный ACTIVE latest.
Env, прочие секреты и поддерживаемые operational settings сохраняются;
неизвестные поля/неподдерживаемые настройки запрещают upload. ZIP проверяется
существующим verifier, включая dirty=false/source_sha=DEPLOY_SHA, и загружается
через --body path под functions/zontcontrol/<sha>/<run-id>-<attempt>.zip —
готовым методом scripts/yandex_deploy.py образца на SHA
eb3fdffbb4ed9918c07b936e379a9c84ecf32c91. Окружение yc наследуется, YC_IAM_TOKEN
заменяется, YC_TOKEN удаляется только у дочернего процесса; HOME сохраняется.
Endpoint/cloud/folder находятся в явном временном профиле, без лишних флагов.
После create проверяются latest ID/functionId/ACTIVE/settings/secrets и
SHA/hash/object в description. Неопределённая запись не повторяется: результат
проверяется чтением; непроверяемый исход завершает deploy ошибкой. Function,
IAM и таймер этот workflow не создаёт и не меняет.

[Согласованный план](../docs/plans/completed/20261020165020-yandex-cloud-boiler-reset-implementation-plan.md)
и [контракт](../docs/agents/deployment.md) задают отдельные подтверждённые
ручные шаги создания ресурсов. Таймер создаётся и включается после успешного
первого автоматического деплоя и отдельного подтверждения владельца.
Сборка/CI не означают публикации или разрешения плановых сбросов.

## Версии и выпуск

Версия X.Y.Z — SemVer, объявлена литералом __version__ в
zontcontrol/__init__.py. Setuptools получает её через dynamic attr;
pyproject.toml не содержит второго номера. CLI, исходники, sdist и wheel
используют один номер без запуска Git/авторизации/клиента ZONT.
Первый раздел выпуска CHANGELOG должен содержать тот же номер, ссылку,
дату и непустой список изменений. Пример формы:

```markdown
## [0.1.0](../docs/history/versioning.md) - 2026-10-15

- Описание изменения.
```

Пустой раздел «Не выпущено» перед первым выпуском допустим. Подготовленный
номер сам по себе не подтверждает публикацию тега или деплой.
.dev/dirty-версии из Git не вычисляются.

| Уровень | Изменения |
| --- | --- |
| MAJOR | Несовместимые параметры CLI/облака, обязательная новая конфигурация, смена смыслов результата или правил управления |
| MINOR | Совместимая возможность приложения или необязательный параметр |
| PATCH | Совместимое исправление, тонкая настройка с прежним контрактом, рефакторинг, документация, тесты и CI/сборка/деплой |

Сам по себе перенос файла, импортов или внутренней сигнатуры не требует MAJOR.
При MAJOR сбрасываются MINOR/PATCH, при MINOR — PATCH. Смешанный выпуск
получает наиболее значимый уровень изменений приложения. Новые технические
скрипты и обвязка относятся к PATCH. Несовместимость требует инструкции перехода.
Текущие интерфейсы описаны в [контрактах](../docs/agents/contracts.md),
схемы конфигурации — в [config/README](../config/README.md).
Обоснования уровней прежних выпусков — в
[CHANGELOG](../CHANGELOG.md) и [истории версий](../docs/history/versioning.md).

Все изменения одного нового PR образуют один выпуск с наиболее значимым
уровнем. Промежуточные коммиты не тегируются. Номер строго выше всех SemVer
тегов и первого выпуска CHANGELOG базы PR: задержка публикации предыдущего
тега не позволяет повторно использовать его номер. Старые исторические
объединения PR описываются в истории; текущий gate не пропускает PR с тем же
номером, что уже в базе. Историю Git не переписывать.

1. В PR выбрать номер, обновить __version__ и первый раздел
   [CHANGELOG](../CHANGELOG.md) с датой, изменениями и необходимой инструкцией перехода.
2. Закоммитить эти файлы. Локально получить теги и проверить относительно
   полной SHA актуальной базы PR. Изменённые незакоммиченные файлы версии
   не могут породить тег, не соответствующий содержимому коммита.
3. Дождаться Lint и Offline checks на актуальном SHA и актуальной базе PR;
   смержить через PR. Версия проверяется до сборки/установки runtime.
4. Проверить workflow [Release tag](../.github/workflows/release-tag.yml).
   Он создаёт аннотированный vX.Y.Z на точном SHA мержа с текстом из CHANGELOG
   и отправляет только этот тег. Ручное создание/публикация тегов не требуется.
5. При ошибке повторить исходное задание через Re-run failed jobs: сохраняется
   SHA прежнего мержа. Run workflow на master проверяет текущий master,
   а не прежний неуспешный выпуск. Тег на том же SHA можно отправить повторно;
   конфликтующий или lightweight тег автоматически не заменяется.
6. Результат и SHA записать в [историю версий](../docs/history/versioning.md).
   Успех выпуска не подтверждает production; будущий деплой проверяется отдельно.

```sh
git fetch origin master --tags
python3 -m scripts.release_version check --base "$(git rev-parse origin/master)"
# Для кандидата без сравнения с базой (всё равно проверяются существующие теги):
make release-check PYTHON=python3
```

Git-проверки требуют настоящего checkout с коммитом и полными тегами.
Перед слиянием CI повторяет gate на актуальной базе/тегах.
Исторические выпуски — [история версий](../docs/history/versioning.md).
В офлайн-тестах создаются временные репозитории и bare remotes; реальные
теги из них не публикуются.

Проверка PR имеет contents:read. Release tag работает только для master с
contents:write у одного job и очередью release-tags, cancel-in-progress=false,
queue=max. Поздний повтор исходного запуска не теряет свой тег из-за более
нового выпуска на потомке. Force push, перенос и удаление тегов запрещены.
Push тега не запускает деплой; workflows выпуска не обращаются к ZONT.
Слияние PR, выпуск тега и деплой не выполняются завершением локального плана.
