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
