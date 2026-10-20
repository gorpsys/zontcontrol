# Yandex Cloud: автоматический деплой и boiler_reset раз в 15 минут

Статус: Tasks 1–5 выполнены; код проверен офлайн, отдельные ресурсы и IAM/OIDC
подтверждены. [Отчёт provisioning](../reports/2026-10-20-yandex-cloud-provisioning.md).
Metadata активной версии секрета также подтверждены. Task 6 (repository
variables, выпуск, первый deploy, таймер и облачная приёмка) ещё не выполнен.
Облачная активация ожидает следующих согласованных шагов.
Основание: [постановка и уточнения](20261020165020-yandex-cloud-boiler-reset-request.md).
Команды: [проверенный справочник yc](20261020165020-yandex-cloud-yc-commands.md).
База zontcontrol: master c09d3c672c927013c976468087c221485911dc6f, 0.4.0.
Образец: gorpsys/autorepeater, актуальный origin/master
eb3fdffbb4ed9918c07b936e379a9c84ecf32c91. Его рабочее дерево не менялось.
Подход выбран владельцем: Cloud Functions, ZIP, GitHub OIDC и существующие
общие ресурсы; альтернативный runtime или Terraform не нужны.

## Цель и границы

После успешных проверок актуального master GitHub Actions автоматически
публикует его ZIP в отдельную функцию zontcontrol. Триггер каждые 15 минут
запускает только boiler_reset в режиме once: история → существующий детектор
→ при текущем confirmed_pattern настоящий импульс сброса → завершение.
CLI, continuous, получение токена и исследовательские программы в ZIP не входят.

Не менять Controller API v2, профиль/пороги, нормализацию температур,
40-секундный импульс и finally. Не добавлять координацию приборов, watchdog,
внешнее хранилище, SDK, runtime-зависимости или резервный workflow деплоя.
Численные регрессии из 0.4.0 сохраняются; облачный перенос не перекалибровка.

Новые требования заменяют прежние предложения в AGENTS/deployment/yandex-cloud
про отдельные folder/bucket/runtime SA/federation, функцию только для чтения
и таймаут 60 секунд. Общие ресурсы теперь переиспользуются явно по решению
владельца. Облачная функция выполняет реальный boiler_reset; тестовый
контур ZONT отсутствует. На этапе плана — только документация и согласованные чтения.

## Что проверено в образце и облаке

| Ресурс | Подтверждённое значение | Действие для zontcontrol |
| --- | --- | --- |
| Cloud | b1gbga988v36vq19lmb7, workflow образца | Использовать тот же |
| Folder | b1gdgrp5bnth11phova8, yc function get | Использовать тот же |
| Функция образца | d4e6d0l53hkivgrmfano, при раннем чтении python-function; по сообщению владельца переименована в t-invest | Не менять; переименование не перепроверялось новым запросом |
| Функция ZONT | d4e9enfbuhk2h59a0fj0, zontcontrol, ACTIVE ресурса | Создана отдельно, закрытая; версии кода ещё нет |
| Deploy SA образца | ajela8pd8l3bjeupa04s, ci-cd-github-sa | Не менять |
| Deploy SA ZONT | aje715nu0u832o93e0hd, ci-cd-zontcontrol-sa | Создан отдельно, три роли подтверждены |
| Runtime SA и invoker | ajelpbq7as5bpe499962, имя invoker | Переиспользовать; добавлен iam.serviceAccounts.user для нового deploy SA |
| OIDC federation | ajej9j37pt8j7ns962vr, federated credential образца | Использовать ту же |
| OIDC credential ZONT | aje80bckog9ld78ondm9, SA aje715nu0u832o93e0hd | Создан и проверен; subject repo:gorpsys/zontcontrol:ref:refs/heads/master |
| GitHub OIDC AUD | https://github.com/gorpsys, workflow образца | Использовать тот же |
| Bucket | autorepeater-deploy, workflow образца | Использовать тот же с префиксом zontcontrol |
| Логи | Default log group каталога b1gdgrp5bnth11phova8 | Использовать тот же журнал |
| Lockbox образца | e6qc2ghhip6925lllhm7, имя/ключ t_token | Не менять и не читать payload |
| Новый Lockbox | e6q7k3r798m1od3jc647, имя z_token; при создании current_version=null | После заполнения владельцем metadata версии e6qi0n3ucqk2stmtdalb ACTIVE / ключ z_token проверены; payload не читался, payloadViewer подтверждён |
| KMS секрета образца | kms_key_id отсутствует | Новый секрет тоже без собственного KMS-ключа |
| Таймер образца | v2 a1sb17vl05nregkjh2si, tinkoff | Не менять |
| Таймер ZONT | Имя zontcontrol-15m, ID ещё не выдан | Создать v2 после первого успешного деплоя и подтверждения владельца |

Роли deploy SA образца подтверждены ранним чтением IAM: functions.admin и
storage.editor на общем folder; iam.serviceAccounts.user на invoker. Такие же
bindings выданы новому deploy SA и проверены readback 20.10.2026.
Это те же области прав, что у образца; ограничение
входа из GitHub задаёт отдельный federated credential, а не имя аккаунта.
На новый секрет invoker получил lockbox.payloadViewer, что проверено readback.
Роль functions.functionInvoker на folder у invoker уже есть; дублировать её
или делать новую функцию публичной не требуется.

У образца python312, handler.handler, 256MB, timeout 60s, concurrency 1.
Версия d4ecfq4knfggm5fgp0a9 получена read-only; секрет подключён ссылкой.
Существующий таймер v2 хранит source.timer и action, а не rule.timer:
cron 0 * ? * * *, action.invoke_function с invoker и retry_attempts=1.
Новый cron — 0/15 * ? * * *: 00/15/30/45 минут по UTC.
Для ZONT политика таймера такая же, как у образца: retry_attempts=1,
interval=10s — один повтор после неуспешного вызова. Следующее обычное
событие — через 15 минут. Как у образца, при публикации версии функции
--async-max-retries явно не задаётся; отсутствие флага не называть
отключением повторов платформы. Повтор таймером заново выполняет весь once
и может снова привести к сбросу при confirmed_pattern. Это выбранное
владельцем поведение; однократное исполнение события не гарантируется.
Автоматических повторов отдельных команд котлу нет.

## Облачный вход, конфиги и жизненный цикл

Добавить handler.py, который только экспортирует zontcontrol.cloud.handler.
Импорт ничего не читает из ZONT и не создаёт приложение. Вызов handler:

1. Настроить облачный logger и безопасный контекст request_id.
2. Прочитать фиксированные config/controller.json, registry.json,
   boiler_reset.json. Проверить конфиги и ZONT_TOKEN до сетевой работы.
3. Создать обычное Application с каталогом из единственного BOILER_RESET_SPEC,
   у которого через dataclasses.replace оставлен только Mode.ONCE.
4. Вызвать app.run_once('boiler_reset', timeout=300) ровно один раз.
5. В finally закрыть приложение с ожиданием завершения worker/finally и
   только потом транспорта. Возвращать результат после закрытия приложения.

Для упаковки без CLI преобразовать внутренний каталог в BOILER_RESET_SPEC
и функцию builtin_specs(): импорт BoilerCLI находится внутри builtin_specs.
create_application получает catalog=None и при None вызывает builtin_specs;
явный каталог облака этого не делает. Локальный каталог по умолчанию по-прежнему
содержит оба алгоритма. Старую внутреннюю константу BUILTIN_SPECS и её
репозиторные usages заменить, совместимую заглушку не оставлять.
Регистратор/DI/потоки и сам BoilerReset используются существующие.

В event не принимать device_id, algorithm_id, mode или разрешение управления:
параметры задаёт облачный профиль, а не произвольный вызывающий. Никакого
host/start/continuous и циклического ожидания в handler. Каждый вызов создаёт
и закрывает своё Application; между вызовами не хранится состояние детекции.

Минимальный ответ handler: version, algorithm_id='boiler_reset', status,
state_at_period_end, reset_performed и безопасный reason из результата модуля.
Unknown/suppressed возвращаются явно, не как normal. Сбой/таймаут логируются
фиксированной категорией и после cleanup приводят к исключению без текста
провайдера и без chained traceback наружу. Ошибка off не превращается в успех.
Не возвращать токен, HTTP-заголовки, исходный event или произвольный repr(error).

Облачные конфиги исходно хранятся в deploy/cloud-config/*.json:

- boiler_reset.json: device_id=700877, circuit_id=20496, air_sensor_id=8207,
  coolant_sensor_id=8429, reset_control_id=8832, check_interval_seconds=60,
  detection_profile='smart_2_heating'. Поле интервала требуется существующей
  схеме; continuous в облаке не запускается.
- registry.json: disabled_modules=[], autostart_dual_mode=false;
  boiler_reset minimum_seconds=170, recommended_seconds=300. Это текущие
  рабочие значения владельца. Облачный каталог содержит только once.
- controller.json: http_timeout_seconds=15, contact заполняется сборщиком
  из непустого ZONT_CONTACT. Значение контакта — GitHub repository variable,
  локально его передаёт разработчик; в Git не переносить рабочий email.

Сборщик формирует в ZIP полноценные config/*.json, использует существующие
валидаторы и отклоняет неизвестные поля/недостающие параметры. Никакого
чтения config/boiler_cli.json, токенов или рабочих локальных конфигов при сборке.
ZONT_TOKEN приходит только из Lockbox: ключ z_token → env ZONT_TOKEN.

## Таймаут и завершение

Плановые параметры: python312, 256MB, concurrency=1, platform timeout=420s,
once timeout=300s, HTTP timeout=15s. Запас 120s до жёсткого timeout платформы
предназначен для завершения начатого HTTP и finally/off. Он не является
доказанной верхней границей задержек ZONT: длительность cloud runs проверяется
при приёмке. timeout 60s образца здесь непригоден даже для текущего минимума once.

При таймауте регистратора остаётся существующая кооперативная отмена;
handler не возвращает управление до завершения cleanup. SIGKILL/жёсткий
таймаут платформы не гарантируют off, как и смерть локального процесса.
Дубли доставки и одновременный локальный клиент не устраняются блокировкой.
concurrency=1 относится к экземпляру функции и не означает глобальную монополию.
Новый алгоритм синхронизации или защитный исполнитель не добавляется.

## Логгер по образцу

Добавить zontcontrol/logging_config.py: именованный logger zontcontrol,
configure_local_logging(), configure_yc_logging(), stdlib JSON formatter.
В облаке одна JSON-строка в stdout: message, level, logger; безопасные поля
event, request_id, algorithm_id, device_id, state, reset_performed, elapsed_ms.
WARNING → WARN, CRITICAL → FATAL, прочие стандартные уровни сохраняются.
Пользовательский IMPORTANT торгового приложения не переносится.
Облачный уровень INFO, propagate=False, один собственный handler при повторной
настройке/warm start. Не переопределять чужие handlers и корневой logger.
configure_local_logging() вызывается в application.main, куда входят и
установленная команда zontcontrol, и python -m zontcontrol через __main__.
Импорт библиотеки и create_application не настраивают логирование;
облачный handler выбирает configure_yc_logging() самостоятельно.
Вывод интерактивного CLI остаётся UI, не превращается в JSON-логи.
Python-json-logger не добавляется.

Логировать начало/завершение once, итог состояния, отправку on и попытку off,
успех API для off и безопасную категорию ошибки. API-успех не называть
физическим восстановлением котла. Сырые API-ответы, токены, payload event,
конфиги и произвольные exception messages в логи не включать. Это обычные
диагностические события, не журнал восстановления и не механизм координации.

## ZIP и Makefile

Цель make yandex-archive создаёт build/yandex-function.zip через
scripts/build_yandex_archive.py. В CI тот же таргет с CLOUD_PACKAGE_SHA
проверенного коммита и ZONT_CONTACT; без контакта сборка завершается понятной
ошибкой. Runtime-зависимости отсутствуют, requirements/PyPI/vendor не нужны.

Явный allowlist архива:

- handler.py, build-manifest.json и три сгенерированных config/*.json;
- zontcontrol/{__init__,application,cloud,config,logging_config,anomaly,
  detection_profile,heating,heating_adapter,monitor,commands,history,inventory}.py;
- algorithms/{__init__,boiler_reset}.py;
- autonomy/{__init__,catalog,contracts,registry,runtime}.py;
- controller/{__init__,auth,contracts,service,transport,zont}.py.

Три последних каталога находятся внутри zontcontrol/. CLI, charts, __main__,
get_token, scripts, tests, docs, reports, .git, окружения, рабочие конфиги и
любые неразрешённые файлы отсутствуют. Не использовать архивирование всего
checkout. Симлинки разрешённых исходников/профиля не разыменовывать.

Manifest содержит schema_version=1, app_version, source_sha и SHA256
каждого исходного/конфигурационного member, кроме самого manifest.
source_sha берётся из HEAD, при CLOUD_PACKAGE_SHA должен совпадать с ним.
Локальная dirty-сборка допустима с явной меткой manifest.dirty=true,
но deploy принимает только dirty=false и source_sha=DEPLOY_SHA.
Секреты не входят даже в manifest. Архив сначала пишется во временный файл,
проверяется на состав/CRC/байты и только затем заменяет конечный ZIP.

Цель make cloud-package-check собирает архив с синтетическим контактом,
проверяет allowlist/manifest и импорт из распакованного ZIP вне checkout
без ZONT_TOKEN и без сетевых вызовов. Smoke с подставным Controller проверяет
один once и отсутствие CLI imports. Сам обработчик реального облака для
этой проверки не вызывается. Добавить проверку ZIP в существующий Offline
checks для Python 3.11/3.12, чтобы она входила в уже обязательный CI gate.

## Автоматический деплой

Адаптировать scripts/deploy_support.py, deploy_gate.py, yandex_deploy.py и
.github/workflows/deploy-prod.yml из актуального master образца.
Не копировать SDK, Sandbox E2E, reserve workflow и торговую бизнес-логику.

Workflow запускается по workflow_run успешного Project checks push-master
либо workflow_dispatch на master. Repo фиксирован gorpsys/zontcontrol,
checkout — проверенный SHA. Gate требует текущий master и успешные Lint,
Offline checks с проверкой workflow path .github/workflows/checks.yml,
SHA и производителя checks/statuses. PR/fork/другой branch/устаревший SHA
не получают облачных credentials. Gate повторяется сразу перед OIDC.
Очередь deploy-prod, cancel-in-progress=false, queue=max.

OIDC subject — repo:gorpsys/zontcontrol:ref:refs/heads/master, без GitHub
environment. AUD запроса GitHub — https://github.com/gorpsys; audience
обмена Yandex IAM — новый SA_ID. IAM передаётся yc только через окружение
дочернего процесса YC_IAM_TOKEN. Явный временный config/profile содержит
endpoint/cloud/folder, без credentials; не переопределять HOME родителя.
Конфиг удаляется при ошибках. yc закреплён на 1.40.0 и скачивается официально.
Все cloud операции helper выполняет через yc; IAM OIDC token exchange —
тот же HTTPS-протокол, что у образца. Статические IAM-ключи не создаются.

Параметры repository variables: CLOUD_ID, FOLDER_ID, SA_ID, FUNCTION_ID,
FUNCTION_SA_ID, BUCKET, LOCKBOX_SECRET_ID, LOCKBOX_VERSION_ID,
GITHUB_OIDC_AUDIENCE, ZONT_CONTACT. В workflow фиксированы runtime/entrypoint,
256MB/420s/concurrency1, ZIP_PATH, LOCKBOX_KEY=z_token и
LOCKBOX_ENVIRONMENT_VARIABLE=ZONT_TOKEN. Пример deploy/yandex.example.json
привести к этому же набору, обозначив его примером, а не вторым источником.
Отсутствующие/неверные обязательные параметры — отказ до OIDC.

Первая публикация отличается от обычной: получить функцию по FUNCTION_ID,
сверить ID/folder, затем version list для того же ID. Если список пуст,
создать первую версию с явными настройками/Lockbox/log folder, не пытаться
читать несуществующий $latest. Ошибка list/get не означает пустой список.
Если версии есть — читать get-by-tag $latest, проверить functionId, сохранить
env/secret bindings/logging и поддерживаемые настройки либо отказать до upload.
Обновляемые поля ZONT задаются явно; чужие bindings не удаляются.
Создание самой функции/IAM/таймера в автоматический deploy не входит.

Проверить manifest и SHA256 ZIP, загрузить в общий bucket по ключу
functions/zontcontrol/<sha>/<run-id>-<attempt>.zip. Создать версию строго
--function-id, проверить functionId/id ответа и итоговый $latest, ACTIVE,
runtime, entrypoint, ресурсы, таймаут, concurrency, runtime SA, log folder,
secret ID/version/key/env и source SHA в description. Переименование ресурса
не меняет цель. Деплой другую функцию по имени не ищет.
CLI вызывается с --retry 0. Команды с неопределённым исходом не повторяются
автоматически: workflow проверяет результат чтением и при невозможности
однозначной проверки завершает deploy ошибкой. В ручном сеансе такое чтение
требует отдельного согласования. Логи helper — только безопасные
категории, проверенные request/trace IDs, SHA/версия/хеш/ключ объекта.

## Порядок создания ресурсов и запуска

Каждая ручная команда yc, включая чтение результата и повтор после ошибки,
предварительно подтверждается владельцем с точным действием и целью.
Подтверждение плана не заменяет этих подтверждений. В автоматическом
согласованном workflow отдельные подтверждения не требуются.

1. На этапе реализации проверить отсутствие одноимённых новых ресурсов,
   создать ci-cd-zontcontrol-sa и отдельный federated credential для master.
   Выдать три подтверждённых права образца; не менять bindings старого SA.
2. Создать закрытую функцию zontcontrol и сохранить выданный FUNCTION_ID.
   Создать секрет z_token без payload, без собственного KMS, выдать invoker
   lockbox.payloadViewer. Старый секрет не менять.
3. Владелец добавляет значение ключа z_token в новую версию Lockbox.
   Агент читает только metadata/current_version.id, не payload. До этого
   нельзя считать облачный этап завершённым или подставлять фиктивный токен.
4. Заполнить repository variables реальными ID и контактом, подготовить
   PR 0.5.0, пройти ревью/CI. Слияние — по отдельному разрешению владельца.
   После master CI автоматический workflow впервые публикует полную версию;
   таймер ещё не создан. При необходимости workflow_dispatch на master
   повторно проходит те же gates.
5. Read-only подтвердить успешный первый deploy: ACTIVE, ожидаемые version ID,
   SHA, параметры и secret binding; проверить отсутствие изменений ресурсов
   образца. До этого таймер не создавать.
6. Отдельно согласовать с владельцем создание и включение timer v2
   zontcontrol-15m: новый FUNCTION_ID, $latest и invoker,
   cron 0/15 * ? * * *, retry_attempts=1, interval=10s. Создание активного таймера сразу
   разрешает плановые реальные сбросы по confirmed_pattern, поэтому это и
   есть шаг активации. Проверить cron/action и активный статус чтением.
   Подготовительные pause/resume и no-op версия не нужны: до первого
   успешного деплоя и подтверждения владельца таймера нет.
7. Зафиксировать два последовательных плановых вызова примерно через 15 минут:
   функция завершается, в логах один once/вызов, корректное состояние/результат,
   нет CLI/continuous, дублированных handlers и утечки credentials. Запись API
   успеха не выдавать за физическое восстановление. Искусственную аварию или
   ручной импульс для приёмки не создавать.

Пока владелец не добавил токен/не согласовал создание и включение таймера,
статус — «код и ресурсы
подготовлены, облачная активация ожидает владельца», а не достигнутая цель.

## Реализация — TDD

Каждый кодовый этап: red → реализация → green; перед следующим зелёные
тематические тесты и make check. Runtime stdlib. Тесты не вызывают ZONT/YC.
Для внешних действий этапов 5–6 нужны реальные доказательства, их нельзя
отмечать выполненными как skipped/manual-not-automatable.

### Task 1: Изолировать once-композицию и настроить логирование

**Files:**
- Create: zontcontrol/logging_config.py, tests/test_logging_config.py.
- Modify: zontcontrol/autonomy/catalog.py, zontcontrol/application.py
  (настройка локального logger в main; __main__.py продолжает делегировать).
- Modify: zontcontrol/algorithms/boiler_reset.py, tests/test_boiler_reset.py.
- Modify: tests/test_application.py, tests/test_boiler_cli.py, tests/test_module_boundaries.py,
  tests/test_installed_package.py (console script).
- Modify: AGENTS.md, docs/agents/deployment.md (зафиксировать новые границы).

- [x] Ввести BOILER_RESET_SPEC и ленивый builtin_specs, изменить default
  create_application, сохранив локальные оба алгоритма и явный DI-каталог.
- [x] Добавить stdlib local/YC loggers и безопасные события BoilerReset,
  не меняя вычисления, команды, finally и lifecycle.
- [x] Вызвать configure_local_logging() в application.main. Проверить оба
  входа CLI (console script и python -m zontcontrol), а также отсутствие
  настройки при импорте/create_application и независимость cloud logger.
- [x] Написать тесты отсутствия CLI import при явном once-каталоге,
  прежних локальных defaults и валидации конфигов до сетевых запросов.
- [x] Проверить формат уровней/stdout/idempotence/Unicode, отсутствие secrets
  и произвольных exception messages, ошибки on/off без ложного success.
- [x] Обновить архитектурную документацию по постановке и выполнить make check.

### Task 2: Добавить handler и облачный профиль

**Files:**
- Create: handler.py, zontcontrol/cloud.py, deploy/cloud-config/*.json.
- Create: tests/test_cloud_handler.py.
- Modify: tests/test_installed_package.py, docs/agents/contracts.md.

- [x] Реализовать ровно один app.run_once('boiler_reset', 300), минимальный
  безопасный ответ и гарантированное ожидание cleanup до выхода handler.
- [x] Добавить профиль с указанными IDs/таймаутами; использовать ZONT_TOKEN
  из env, event не управляет выбором алгоритма/устройства/режима.
- [x] Написать тесты normal/unknown/suppressed/confirmed, already_on,
  недоступного токена/конфига, timeout/cancel и ошибки finally/off с fake API.
- [x] Проверить свежий app на каждом вызове, отсутствие побочных эффектов
  импорта и CLI/continuous, закрытие транспорта после worker/finally.
- [x] Выполнить make check и обновить installed-wheel smoke новых модулей.

### Task 3: Формировать и проверять минимальный облачный ZIP

**Files:**
- Create: scripts/build_yandex_archive.py, scripts/check_cloud_package.py.
- Create: tests/test_yandex_archive.py, tests/test_cloud_package.py.
- Modify: Makefile, .github/workflows/checks.yml, scripts/README.md.

- [x] Добавить yandex-archive/cloud-package-check, allowlist, конфиги из
  deploy/cloud-config, подстановку контакта и manifest/atomic ZIP replacement.
- [x] Проверять expected SHA/dirty, байты/состав/CRC и недопустимые symlinks;
  не упаковывать локальный config, CLI, secrets и посторонние файлы.
- [x] Написать тесты состава/исключений, отсутствия контакта, неверного конфига,
  SHA/manifest drift и сохранения прежнего ZIP при ошибке сборки.
- [x] Добавить offline ZIP import/once smoke вне checkout; проверить отсутствие
  CLI и любых сетевых обращений. Включить smoke в Offline checks 3.11/3.12.
- [x] Выполнить make check, make cloud-package-check, make package-check.

### Task 4: Адаптировать gate и автоматический deploy

**Files:**
- Create: scripts/deploy_support.py, scripts/deploy_gate.py, scripts/yandex_deploy.py.
- Create: .github/workflows/deploy-prod.yml.
- Create: tests/test_deploy_gate.py, tests/test_yandex_deploy.py, tests/test_deploy_workflow.py.
- Modify: deploy/yandex.example.json, scripts/README.md, docs/agents/deployment.md.

- [x] Перенести только нужную stdlib-обвязку образца, адаптировать repo,
  обязательные checks/producer/path и повторный gate перед OIDC.
- [x] Добавить настройки нового приложения, безопасный yc wrapper с временным
  явным config, отдельные OIDC AUD/SA audience и отказ до credentials.
- [x] Реализовать bootstrap пустого списка версий и обычное сохранение настроек,
  загрузку в отдельный префикс bucket, publish/ACTIVE/settings verification.
- [x] Написать тесты fork/PR/старого SHA/подложных checks, смены master перед OIDC,
  пустых параметров, первой версии, ошибки чтения, чужого functionId,
  утраты secret/SA/concurrency/logging, unknown settings и unknown write outcome.
- [x] Проверить через fake subprocess отсутствие token в argv/config/logs и
  удаление временного профиля на всех выходах; отсутствие явного
  --async-max-retries, как у образца; проверить workflow permissions
  и pinned checkout/yc. Выполнить make check и обе проверки пакетов.

### Task 5: Создать облачные ресурсы с отдельными подтверждениями

**Files:**
- Create during implementation: docs/reports/<date>-yandex-cloud-provisioning.md.
- Modify: deploy/yandex.example.json, docs/agents/deployment.md.
- Use: справочник yc рядом с планом; таймер на этом этапе не создаётся.

- [x] Перед каждой ручной командой показать её и цель, получить подтверждение;
  проверить одноимённые ресурсы и не перезаписывать найденные автоматически.
- [x] Создать отдельные deploy SA/federated credential/function/empty z_token;
  выдать подтверждённые роли, сохранив все общие и исходные ресурсы.
- [x] Записать реальные IDs, scopes/roles и время проверки. Создание таймера
  отложить до успешного первого деплоя и отдельного подтверждения в Task 6.
- [x] Сопоставить наблюдаемые metadata с ожидаемыми параметрами; при unknown
  outcome сначала отдельное согласованное чтение, не повтор create вслепую.
- [x] Повторить offline-проверки resource settings/action JSON (один повтор
  таймера через 10s) и make check; payload токена и credentials в
  отчёт/репозиторий не включать.

### Task 6: Довести до автоматического запуска и оформить выпуск

**Files:**
- Modify: README.md, scripts/README.md, docs/agents/architecture.md,
  docs/reference/boiler-reset.md, docs/history/versioning.md.
- Modify: zontcontrol/__init__.py, CHANGELOG.md.
- Create during implementation: docs/reports/<date>-yandex-cloud-acceptance.md.
- Move after completion: этот план в docs/plans/completed/ с исправлением ссылок.

- [ ] После сообщения владельца о заполнении z_token прочитать только metadata
  новой версии; настроить repository variables, не получая значение секрета.
- [ ] Подготовить один MINOR 0.5.0 при базе 0.4.0 либо следующий свободный MINOR;
  подписанные коммиты, PR, холодное ревью/WunderReview и CI. Слияние отдельно.
- [ ] После разрешённого merge дождаться автоматического deploy точного SHA;
  проверить ACTIVE/ID/settings/manifest provenance, исходные ресурсы не изменились.
- [ ] Только после успешного первого deploy отдельно согласовать создание
  и включение v2 timer. Создать его, проверить ID/cron/action/активный статус
  и записать их в отчёт; не создавать предварительную no-op версию.
- [ ] После создания таймера наблюдать два плановых once и
  записать фактические длительности/результаты/logging без утверждений о
  физическом восстановлении и без искусственного сброса для диагностики.
- [ ] Завершить документацию, make check/package-check/cloud-package-check,
  release gate, перенести план только после всех необходимых внешних действий.

## Критерии готовности и внешняя зависимость

Архив содержит только once-сценарий и его зависимости; сборка и импорт
офлайн. Локальное приложение работает по-прежнему. Новый deploy SA принимает
только subject нашего master; workflow ограничен актуальным проверенным SHA.
Функция отдельная, runtime/общие ресурсы прежние, секрет отдельный и доступен
invoker. Автодеплой и два запуска таймера подтверждены реальными фактами.
Триггер создан после первого успешного деплоя и подтверждения владельца,
работает раз в 15 минут с одним повтором после ошибки через 10s, как у
образца. Политика проверена по action JSON и metadata. finally/таймауты
и логи соответствуют плану.

Внешняя зависимость — владелец сам добавляет значение секрета и подтверждает
ручные yc-запросы/включение. Отсутствие этого шага не заменять фиктивной
приёмкой; код может быть подготовлен раньше, но цель ещё не достигнута.
Ограничения жёсткого завершения и одновременного вмешательства остаются прежними.
