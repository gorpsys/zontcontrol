# Холодное ревью реализации Yandex Cloud, Tasks 1–4

Дата: 20.10.2026. Проверенный SHA:
`3c536159eed9f873200e0467761082a7a79a1aab`.
База: master `c09d3c672c927013c976468087c221485911dc6f`.
Ветка: `release/yandex-cloud-boiler-reset`.
Область: код Tasks 1–4 согласованного
[плана](../plans/20261020165020-yandex-cloud-boiler-reset-implementation-plan.md).

Пять независимых читателей проверили качество, реализацию, тестирование,
упрощение и конвенции. Fixer повторно проверил каждое замечание по актуальному
коду с окружающими 20–30 строками и затем выполнил офлайн-проверки.
В quality и implementation подтверждённых замечаний нет.

## Подтверждённые замечания и исправления

| Статус | Важность | Место в reviewed SHA | Проверка и исправление |
| --- | --- | --- | --- |
| CONFIRMED, исправлено | MAJOR | tests/test_yandex_deploy.py:97 | FakeYC формирует ожидаемый readback независимо от переданных параметров. Тесты теперь проверяют фактические argv: runtime, entrypoint, memory, timeout, concurrency, runtime SA, log folder, environment, pinned Lockbox bindings, tmpfs, network, named SA, metadata endpoints и min log level. CSV разбирается через stdlib csv.reader: значение `a,b=c with spaces` сохраняется целиком. Проверки окружения и bindings больше не опираются на yc.after. |
| CONFIRMED, исправлено | MINOR | .github/workflows/deploy-prod.yml:102 | Промежуточный deploy_gate --check-once повторял authorize(environment) внутри helper. Удалены только этот step и привязанные assertions. Начальный gate job и authorize после локальной валидации непосредственно перед exchange_token сохранены; их проверки проходят. |
| CONFIRMED, исправлено | MINOR | tests/test_yandex_deploy.py:5 | Неиспользуемый import hashlib удалён. |

## Отклонённые замечания и отдельная гипотеза

FALSE POSITIVE среди трёх предъявленных дефектов нет: каждый подтверждён.

Гипотеза implementation reader о bootstrap metadataOptions не подтверждена
как дефект. scripts/yandex_deploy.py сравнивает отсутствующие options как `{}`;
если сервер материализует defaults, readback может отличаться. Описанные
провайдером defaults сами по себе не доказывают формат ответа первой версии.
Подставной ответ с такими полями показывает лишь условную ветку сравнения,
а не фактическое поведение облака. Без наблюдаемого readback менять runtime
или объявлять finding было бы необоснованно. Гипотеза остаётся открытой для
проверки при разрешённой первой публикации и не считается исправленным дефектом.

## Проверки

Mutation-пробы изменяли результат version_arguments перед FakeYC.call;
production source при этом не менялся. Использованы временный скрипт,
синтетические конфиги/ZIP, Python -B и запрет socket.connect.

| Мутация фактических argv | До исправления, 19 DeployTests | После исправления, 19 DeployTests |
| --- | --- | --- |
| runtime=python311, entrypoint=missing.handler, memory=128MB | 0 failures, 0 errors: регрессия не обнаруживалась | 1 failure, 0 errors |
| Повреждены environment, ключи secret bindings и metadata endpoints | 0 failures, 0 errors: регрессия не обнаруживалась | 3 failures, 0 errors |

Без мутаций тематический набор deploy/gate/workflow: 30 тестов, OK.
Полные проверки: make check (428 тестов, 6 прежних installed-only skips),
make cloud-package-check и make package-check (8 installed-wheel тестов) — OK.
Номер и SHA подписанного исправления записываются в локальный progress после
коммита, чтобы отчёт не ссылался на собственный ещё не существующий SHA.

## Границы

Облачных и ZONT-запросов, включая yc --help, при проверке и исправлении не было.
Алгоритм, пороги, 40-секундный импульс, finally, отсутствие синхронизации,
retry_attempts=1/10s таймера и отсутствие --async-max-retries не менялись.
Значений credentials и сертификатов отчёт не содержит.

Tasks 5–6 остаются **незавершёнными**, без skipped/manual-not-automatable.
Первые три ручных yc-чтения ожидают отдельных подтверждений владельца;
ресурсы, первый deploy, активация таймера и облачная приёмка не выполнялись.
Push, PR, merge, выпуск и публикация тегов не входят в эти исправления.
Исторические plan-review reports сохранены без изменений.

## Последующая критическая перепроверка

После подписанного fix commit b73969e6e1048f3f5b7be51929b14f8ba62b3d61
независимые quality и implementation readers не нашли critical/major замечаний,
включая проверки фактических argv и gate перед OIDC. Результат записан в
progress 20.10.2026 в 19:16:58 Europe/Moscow. Это дополнение не меняет
историю findings и исправлений выше. Последующее создание ресурсов Task 5
описано в [отчёте provisioning](../reports/2026-10-20-yandex-cloud-provisioning.md);
первый deploy и облачная приёмка остаются Task 6.

## Premerge WunderReview и проверка каждого замечания

Инструмент wunderreview-alt проверил PR
[gorpsys/zontcontrol#11](https://github.com/gorpsys/zontcontrol/pull/11),
SHA `cdee5a979b7e665c1b9f341cb9880b91e132c048`, 7/7 batches.
Исходный verdict — **needs_changes**, blocking=1, non_blocking=1.
Локальные артефакты: reports/yandex-cloud/wunderreview-premerge/report.md
и report.json. Исходный verdict и findings не переписывались;
этот разбор не объявляет результат инструмента clean.

Каждое замечание и полный текст его правила сверены с реальным кодом
catalog.py:1–38, потребительскими контрактами и правилами SemVer.

| Finding | Проверенный результат |
| --- | --- |
| py-code-style/no-mutable-defaults, catalog.py:31 | FALSE POSITIVE. Значение specs=() — пустой неизменяемый tuple, не содержащий изменяемых элементов. Конструктор сохраняет tuple(specs) и ничего не добавляет в default. Текст правила запрещает изменяемые []/{}/поля dataclass без default_factory, а не tuple. Указание reviewer «включая кортежи» отсутствует в самом правиле. Замена на None не устраняет реального дефекта и не внесена. |
| py-infra/backward-compat, catalog.py:1–38 | CONFIRMED в части отсутствующей инструкции перехода: в базе 0.4.0 BUILTIN_SPECS существовал и мог импортироваться напрямую. Публичный потребительский контракт — Controller API v2; каталог является внутренней композицией. Правило допускает миграционный план, но прежнего объяснения ленивого builtin_specs() было недостаточно для прямых импортов. В CHANGELOG и архитектуру добавлен явный переход BUILTIN_SPECS → builtin_specs() для local либо (BOILER_RESET_SPEC,) для once. Eager-алиас импортировал бы BoilerCLI, которого нет в облачном ZIP. |

Основания: [текущий каталог](../../zontcontrol/autonomy/catalog.py),
[потребительский контракт](../agents/contracts.md#управляющий-api),
[инструкция перехода](../agents/architecture.md#3-регистратор-и-запуск),
[SemVer](../../scripts/README.md#версии-и-выпуск).
Правила проекта явно не требуют MAJOR для переноса внутренних импортов;
подготовленный единый выпуск остаётся 0.5.0. Controller API v2 и поведение
алгоритмов этим исправлением не меняются. Новых runtime-тестов для
документационного перехода не добавлено; существующие проверки композиции
и облачного ZIP проверяют отсутствие eager CLI-импорта.

Настройка repository variables Task 6 завершена: readback всех десяти
значений точно совпал с ожидаемым; контакт и credentials не выводились.
Версии функции, таймера и облачной приёмки по-прежнему нет. Офлайн-проверки
и SHA подписанного документационного исправления фиксируются в progress
после их фактического завершения.
