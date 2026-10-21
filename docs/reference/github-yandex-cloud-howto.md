# Как настроить деплой из GitHub Actions в Yandex Cloud Functions

Пошаговая инструкция для Python-функции: GitHub проверяет `master`, собирает
ZIP, получает краткоживущий IAM-токен через OIDC, загружает архив в Object
Storage и публикует новую версию функции. После первого успешного деплоя
можно подключить запуск по таймеру.

Инструкция составлена 21.10.2026 по рабочим реализациям
[zontcontrol](https://github.com/gorpsys/zontcontrol/tree/6dc71121cbc2feb290dbc5403bb1ef450bf379ea)
и [autorepeater](https://github.com/gorpsys/autorepeater/tree/5bfeeeb865f6c3fdb381b27419309b41b1f5b26a).
Создание ресурсов ниже описано как отдельная первоначальная настройка.
Повседневный workflow создаёт только версии функции и объекты с архивами.

Это инструкция, а не скрипт для запуска целиком. Выполняйте команды по этапам,
проверяйте результат и подставляйте ID созданных ресурсов. Повторно создавать
уже работающие ресурсы не нужно. В zontcontrol каждый ручной запрос `yc`,
включая чтение и повтор, отдельно подтверждает владелец; согласованный
автоматический workflow работает без подтверждения каждой команды.

## 1. Подготовьте инструменты и параметры

Понадобятся Bash, Git, Python 3.12, Make, GitHub CLI `gh` и Yandex Cloud CLI
`yc`. В проверенных workflow используется **yc 1.40.0**. Устанавливайте CLI
по [официальной инструкции](https://yandex.cloud/ru/docs/cli/quickstart).
Для локальных административных команд авторизуйтесь через `yc init`,
для GitHub — через `gh auth login`.

В Yandex Cloud должны быть подключён биллинг и выбран каталог. Администратору
нужны права создавать функции, bucket, сервисные аккаунты, секреты и
OIDC-федерацию, а также назначать перечисленные ниже роли. Создание федерации
требует `iam.workloadIdentityFederations.editor` или более широкой роли.
Runtime SA и deploy SA таких административных прав на федерацию не требуют.
В GitHub нужны права редактировать Actions, repository variables и защиту ветки.

Заполните переменные в своей Bash-сессии; значения `YOUR_...` — заглушки:

```bash
export REPOSITORY='YOUR_OWNER/YOUR_REPOSITORY'
export CLOUD_ID='YOUR_CLOUD_ID'
export FOLDER_ID='YOUR_FOLDER_ID'
export FUNCTION_NAME='my-function'
export DEPLOY_SA_NAME='ci-cd-my-function'
export BUCKET='YOUR_GLOBALLY_UNIQUE_BUCKET'
export OIDC_AUDIENCE='https://github.com/YOUR_OWNER'
export SECRET_NAME='my-function-token'
export LOCKBOX_KEY='api_token'
export TOKEN_ENVIRONMENT_VARIABLE='API_TOKEN'
export TRIGGER_NAME='my-function-15m'

YC_SCOPE=(--endpoint api.cloud.yandex.net:443
          --cloud-id "$CLOUD_ID" --folder-id "$FOLDER_ID" --retry 0)
```

`--retry 0` исключает автоматический повтор ручной записи при неясном исходе.
Если ответ создания потерян, сначала найдите ресурс чтением по имени.
Отсутствие ответа не означает, что ресурс не создался.

Для настройки именно zontcontrol возьмите имена, ключи, адреса отдельных
и общих ресурсов из [датированного реестра](yandex-cloud-resources.md).
Сверьте его с workflow и подтверждённым состоянием своего окружения.
Для существующих ресурсов не выполняйте повторное создание.

**Результат этапа:** выбран один каталог и подготовлена таблица параметров;
секретных значений в ней нет.

## 2. Разделите deploy, runtime и вызов функции

| Участник | Для чего нужен | Права в проверенной схеме |
| --- | --- | --- |
| Deploy SA | GitHub публикует ZIP и версии | `functions.admin`, `storage.editor` на каталог; `iam.serviceAccounts.user` на runtime SA |
| Runtime SA | Код функции обращается к разрешённым ресурсам | `lockbox.payloadViewer` на свой секрет; для записи в журнал — `logging.writer` на каталог журнала |
| Invoker SA | Таймер вызывает закрытую функцию | `functions.functionInvoker` на функцию |

В наших проектах runtime SA и invoker — один существующий аккаунт.
Deploy SA отдельный для каждого репозитория. Имена переменных:
`DEPLOY_SA_ID`, `FUNCTION_SA_ID`, `INVOKER_SA_ID`.

Роли `functions.admin` и `storage.editor` на каталог широкие: они разрешают
работу с другими функциями и bucket этого каталога. OIDC-привязка ограничивает,
какой GitHub-репозиторий получает эти права; префикс ключа объекта сам по себе
IAM-права не ограничивает. Если такая область доступа не подходит, сначала
спроектируйте отдельный каталог или более узкие права и проверьте helper с ними.

Если runtime/invoker ещё нет, создайте аккаунт:

```bash
yc "${YC_SCOPE[@]}" iam service-account create --name my-function-runtime \
  --format json --jq '{id,name,folder_id}'
```

Сохраните полученный ID:

```bash
export FUNCTION_SA_ID='YOUR_RUNTIME_SA_ID'
export INVOKER_SA_ID="$FUNCTION_SA_ID"
```

Для общего стандартного журнала:

```bash
yc "${YC_SCOPE[@]}" resource-manager folder add-access-binding \
  --id "$FOLDER_ID" --role logging.writer \
  --subject "serviceAccount:${FUNCTION_SA_ID}"
```

Если существующий аккаунт уже имеет это разрешение, достаточно проверить
binding; повторное назначение не нужно. Право читать журнал нужно отдельно
выдать оператору (`logging.reader`), а не deploy SA.

## 3. Подготовьте bucket, функцию и пустой секрет

Перед созданием проверьте существующие ресурсы:

```bash
yc "${YC_SCOPE[@]}" serverless function list --format json \
  --jq 'map({id,name,folder_id})'
yc "${YC_SCOPE[@]}" iam service-account list --format json --jq 'map({id,name})'
yc "${YC_SCOPE[@]}" lockbox secret list --format json --jq 'map({id,name})'
yc "${YC_SCOPE[@]}" storage bucket list --format json
```

Если отдельного bucket для архивов ещё нет:

```bash
yc "${YC_SCOPE[@]}" storage bucket create --name "$BUCKET" \
  --default-storage-class STANDARD --format json
```

Bucket не должен быть публичным. В одной схеме можно использовать общий
bucket, сохраняя архивы разных приложений под разными префиксами.

Создайте закрытую функцию без версии и пустой секрет без payload:

```bash
yc "${YC_SCOPE[@]}" serverless function create --name "$FUNCTION_NAME" \
  --format json --jq '{id,name,folder_id,status}'
yc "${YC_SCOPE[@]}" lockbox secret create --name "$SECRET_NAME" \
  --format json --jq '{id,name,folder_id,kms_key_id,current_version}'
```

Сохраните ID:

```bash
export FUNCTION_ID='YOUR_FUNCTION_ID'
export LOCKBOX_SECRET_ID='YOUR_SECRET_ID'
```

Разрешите runtime читать только этот секрет и invoker вызывать эту функцию:

```bash
yc "${YC_SCOPE[@]}" lockbox secret add-access-binding \
  --id "$LOCKBOX_SECRET_ID" --role lockbox.payloadViewer \
  --subject "serviceAccount:${FUNCTION_SA_ID}"
yc "${YC_SCOPE[@]}" serverless function add-access-binding \
  --id "$FUNCTION_ID" --role functions.functionInvoker \
  --subject "serviceAccount:${INVOKER_SA_ID}"
```

В дальнейшем адресуйте функцию по **ID**. Переименование функции в консоли
не меняет ID, но ломает helper, который ищет её по старому имени.
В zontcontrol уже используется `FUNCTION_ID`; helper autorepeater исторически
использовал `FUNCTION_NAME` — этот фрагмент нельзя переносить без проверки.

**Результат этапа:** bucket, пустая закрытая функция и отдельный пустой секрет;
таймера пока нет, код не запускался.

## 4. Узнайте фактический GitHub OIDC subject

Есть два разных audience:

| Где используется | Значение |
| --- | --- |
| GitHub ID token и настройка федерации YC | Например, `https://github.com/YOUR_OWNER` |
| Обмен ID token на IAM-токен YC | ID **deploy SA**, не URL и не ID федерации |

Сначала задайте несекретную repository variable:

```bash
gh variable set YC_OIDC_AUDIENCE --repo "$REPOSITORY" --body "$OIDC_AUDIENCE"
```

Не угадывайте `sub` по названию репозитория. Добавьте временный диагностический
workflow `.github/workflows/oidc-claims.yml`, проведите его через PR в `master`
и запустите на `master`:

```yaml
name: OIDC claims
on:
  workflow_dispatch:
permissions: {}
jobs:
  claims:
    if: github.ref == 'refs/heads/master'
    runs-on: ubuntu-latest
    timeout-minutes: 3
    permissions:
      id-token: write
    steps:
      - name: Show only non-secret claims
        env:
          OIDC_AUDIENCE: ${{ vars.YC_OIDC_AUDIENCE }}
        shell: bash
        run: |
          python3 - <<'PY'
          import base64
          import json
          import os
          from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
          from urllib.request import Request, urlopen

          try:
              audience = os.environ['OIDC_AUDIENCE']
              if not audience:
                  raise ValueError()
              url = urlsplit(os.environ['ACTIONS_ID_TOKEN_REQUEST_URL'])
              query = [(k, v) for k, v in parse_qsl(url.query) if k != 'audience']
              endpoint = urlunsplit(url._replace(
                  query=urlencode([*query, ('audience', audience)])))
              request = Request(endpoint, headers={
                  'Authorization': 'Bearer ' + os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']})
              with urlopen(request, timeout=30) as response:
                  token = json.load(response)['value']
              payload = token.split('.')[1]
              claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
              keys = ('iss', 'aud', 'sub', 'repository', 'repository_id',
                      'repository_owner_id', 'ref')
              print(json.dumps({key: claims.get(key) for key in keys}))
          except Exception:
              raise SystemExit('Cannot obtain OIDC claims') from None
          PY
```

```bash
gh workflow run oidc-claims.yml --repo "$REPOSITORY" --ref master
gh run list --repo "$REPOSITORY" --workflow oidc-claims.yml --limit 3
gh run view YOUR_RUN_ID --repo "$REPOSITORY" --log
```

В лог выводятся только выбранные claims, сам токен не выводится. Локальная
расшифровка payload нужна для диагностики; подпись и срок токена при обмене
проверяет Yandex IAM.

Встречаются, например, такие форматы `sub`:

```text
repo:OWNER/REPOSITORY:ref:refs/heads/master
repo:OWNER@OWNER_ID/REPOSITORY@REPOSITORY_ID:ref:refs/heads/master
```

Пример второго формата, полученный в zontcontrol при проверке 21.10.2026:
`repo:gorpsys@79201964/zontcontrol@1420696677:ref:refs/heads/master`.
Это непереносимый пример; привязка проекта с источником — в
[реестре](yandex-cloud-resources.md#oidc-и-iam).
Не копируйте его в другой репозиторий. Добавление GitHub Environment,
смена ветки или настройки subject могут изменить `sub`; диагностический
и deploy job должны иметь одинаковые условия для формирования subject.
Не настраивайте доверие к `pull_request` вместо production-ветки.

Сохраните точный `sub` в обычную локальную переменную:

```bash
export OIDC_SUBJECT='YOUR_EXACT_SUB_FROM_WORKFLOW'
```

## 5. Создайте федерацию, deploy SA и доверие к репозиторию

Если общей федерации GitHub ещё нет, создайте её:

```bash
yc "${YC_SCOPE[@]}" iam workload-identity oidc federation create \
  --name github-actions \
  --issuer https://token.actions.githubusercontent.com \
  --jwks-url https://token.actions.githubusercontent.com/.well-known/jwks \
  --audiences "$OIDC_AUDIENCE" --format json
```

Если федерация существует, прочитайте её и проверьте issuer, JWKS и audience:

```bash
export FEDERATION_ID='YOUR_FEDERATION_ID'
yc "${YC_SCOPE[@]}" iam workload-identity oidc federation get \
  --id "$FEDERATION_ID" --format json
```

Создайте deploy SA и назначьте права:

```bash
yc "${YC_SCOPE[@]}" iam service-account create --name "$DEPLOY_SA_NAME" \
  --format json --jq '{id,name,folder_id}'
```

```bash
export DEPLOY_SA_ID='YOUR_DEPLOY_SA_ID'
yc "${YC_SCOPE[@]}" resource-manager folder add-access-binding \
  --id "$FOLDER_ID" --role functions.admin \
  --subject "serviceAccount:${DEPLOY_SA_ID}"
yc "${YC_SCOPE[@]}" resource-manager folder add-access-binding \
  --id "$FOLDER_ID" --role storage.editor \
  --subject "serviceAccount:${DEPLOY_SA_ID}"
yc "${YC_SCOPE[@]}" iam service-account add-access-binding \
  --id "$FUNCTION_SA_ID" --role iam.serviceAccounts.user \
  --subject "serviceAccount:${DEPLOY_SA_ID}"
```

Последний binding назначается **на runtime SA**, а его получатель — deploy SA.
Обратное направление не разрешает привязать runtime SA к версии функции.

Создайте federated credential:

```bash
yc "${YC_SCOPE[@]}" iam workload-identity federated-credential create \
  --service-account-id "$DEPLOY_SA_ID" --federation-id "$FEDERATION_ID" \
  --external-subject-id "$OIDC_SUBJECT" \
  --format json --jq '{id,service_account_id,federation_id,external_subject_id}'
```

Проверьте фактические bindings:

```bash
yc "${YC_SCOPE[@]}" resource-manager folder list-access-bindings \
  --id "$FOLDER_ID" --format json
yc "${YC_SCOPE[@]}" iam service-account list-access-bindings \
  --id "$FUNCTION_SA_ID" --format json
yc "${YC_SCOPE[@]}" iam workload-identity federated-credential list \
  --service-account-id "$DEPLOY_SA_ID" --format json
```

Постоянный IAM-ключ для GitHub создавать не нужно. Federation ID используется
при создании credential; workflow получает ID deploy SA и запрашивает токены
во время запуска.

## 6. Заполните секрет и закрепите его версию

В консоли Yandex Cloud откройте Lockbox → созданный секрет → добавление версии.
Добавьте строковую пару с ключом из `LOCKBOX_KEY` и реальным токеном.
Значение вводится владельцем непосредственно в Lockbox. Не помещайте его
в YAML workflow, repository variables, командную строку или файл в Git.

Проверьте только metadata:

```bash
yc "${YC_SCOPE[@]}" lockbox secret get --id "$LOCKBOX_SECRET_ID" \
  --format json \
  --jq '{id,name,status,current_version:(.current_version | {id,status,payload_entry_keys})}'
yc "${YC_SCOPE[@]}" lockbox secret list-access-bindings \
  --id "$LOCKBOX_SECRET_ID" --format json
```

Должны быть ACTIVE версия и секрет, нужный ключ и binding runtime SA.
Сохраните ID версии:

```bash
export LOCKBOX_VERSION_ID='YOUR_ACTIVE_SECRET_VERSION_ID'
```

ID секрета и версии — разные значения. В публикации используется закреплённый
`version-id`; добавление новой версии в Lockbox само по себе не переключит
уже опубликованную функцию.

## 7. Подготовьте Python-вход и облачный ZIP

В корне ZIP должен быть `handler.py`, экспортирующий `handler(event, context)`;
entrypoint функции — `handler.handler`. Импорт не должен запускать приложение,
читать токен или обращаться в сеть. Каждый вызов создаёт ресурсы, выполняет
один сценарий и завершает cleanup до возврата. Платформенный timeout должен
оставлять время на внутренние таймауты и cleanup.

Составляйте ZIP по явному списку необходимых файлов. Не архивируйте весь
checkout: туда попадут рабочие конфиги, исследования и локальные артефакты.

| Основа | Команда сборки | Что учитывать |
| --- | --- | --- |
| zontcontrol | `make yandex-archive` | stdlib, минимальный once-пакет, облачный профиль, `build-manifest.json` |
| autorepeater | `make claude-yandex-archive` | Сторонние зависимости, runtime lock и подготовка SDK; не переносить SDK-часть в stdlib-приложение |

В обоих случаях результат — `build/yandex-function.zip`. Зависимости
приложения должны быть доступны в runtime или подготовлены поддерживаемым
облачной сборкой способом; локальная `.venv` не является частью поставки.

В zontcontrol облачные конфиги берутся из `deploy/cloud-config`, контакт
подставляется из `ZONT_CONTACT`, токен в ZIP не записывается:

```bash
export ZONT_CONTACT='YOUR_CONTACT'
make check
make package-check
make cloud-package-check
make yandex-archive
```

`make yandex-archive` проверяет состав, manifest и конфиги ZIP перед сохранением
и повторно проверяет готовый архив; при успехе выводит JSON со `status: ok`.

Для production архив собирается в CI из чистого checkout точного SHA.
Manifest содержит версию, `source_sha`, `dirty` и хеши файлов. Helper требует
совпадения SHA и `dirty=false`. Локальный dirty-архив пригоден для разработки,
но не для обхода gate. Отдельно вычисляется SHA-256 байтов ZIP.

**Результат этапа:** офлайн-проверки прошли; архив импортируется вне checkout
и без сети, содержит только нужный сценарий и ни одного credential.

## 8. Подключите CI и deploy workflow

В zontcontrol связка уже реализована:

| Файл | Назначение |
| --- | --- |
| [checks.yml](../../.github/workflows/checks.yml) | `Project checks`: Lint и Offline checks для SHA |
| [deploy-prod.yml](../../.github/workflows/deploy-prod.yml) | Gate, сборка архива, установка yc, публикация |
| [deploy_gate.py](../../scripts/deploy_gate.py) | Проверка текущего master и происхождения обязательных checks |
| [yandex_deploy.py](../../scripts/yandex_deploy.py) | Настройки, OIDC, команды yc, проверка опубликованной версии |
| [deploy_support.py](../../scripts/deploy_support.py) | Общие безопасные проверки и обработка ошибок |
| [build_yandex_archive.py](../../scripts/build_yandex_archive.py) | Сборка и проверка состава ZIP/manifest |

Для другого проекта переносите эту связку с её проверками. Helper zontcontrol
намеренно не является универсальным deploy-клиентом. При адаптации измените:

1. `scripts/deploy_gate.py`: `REPOSITORY`, имена обязательных checks и доверенный
   workflow; сохраните проверку producer, run, event, ветки и точного SHA.
2. `.github/workflows/deploy-prod.yml`: repository в условии job, имя CI workflow
   в `workflow_run`, build target, env и фиксированные параметры функции.
3. `scripts/yandex_deploy.py`: `shared`/`fixed` в `settings_from_environment`,
   проверки repository/ref/audience, `Settings`, префикс Object Storage,
   обработку secret bindings и валидацию параметров приложения. В zontcontrol
   есть специальные запреты ID ресурсов образца и проверка `ZONT_CONTACT`.
4. Builder и verifier: список модулей, облачные конфиги, зависимые файлы,
   проверку manifest и офлайн-smoke именно вашего entrypoint.
5. Тесты helper: bootstrap без версий, обновление существующей версии,
   сохранение настроек, неверный SHA/subject/target, неоднозначный исход записи.

Не отключайте проверки ради переноса. Простая замена `FUNCTION_ID` без
адаптации проверок закономерно даёт `missing or unexpected deployment settings`.

Обязательные свойства workflow:

- `workflow_run` после успешного CI на push в master и/или ручной
  `workflow_dispatch` на master. Завершившийся PR workflow не является разрешением
  на production-деплой. В autorepeater входной workflow — `Sandbox live E2E`,
  в zontcontrol — `Project checks`: это требования разных проектов.
- Gate проверяет текущий master, конкретный SHA и доверенные CI-проверки,
  а не только зелёный статус с подходящим названием. При устаревшем SHA публикации нет.
- Checkout закреплён на SHA gate, `persist-credentials: false`.
- Production job имеет `id-token: write`; gate не требует облачного токена.
  Для zontcontrol используются также `contents: read`, `checks: read`, `actions: read`.
  `statuses: read` образца требуется только при использовании его gate со statuses.
- Перед запросом OIDC выполняется повторная проверка SHA/checks и локальных
  настроек. Все HTTP и subprocess имеют конечные timeout.
- Деплои сериализованы: `concurrency.group: deploy-prod`,
  `cancel-in-progress: false`; в проверенных workflow также `queue: max`.
- yc устанавливается из официального источника с закреплённой версией.
  Runtime, entrypoint, memory, timeout, concurrency заданы явно.

Включите требуемые проверки в защите master и проводите изменения через PR.
Release tag создаётся отдельным workflow на SHA слияния и сам по себе
не является проверкой или запуском деплоя. В этих репозиториях версия
`__version__` должна совпадать с первой записью CHANGELOG и проходить release gate.

## 9. Настройте GitHub repository variables

GitHub → Settings → Secrets and variables → Actions → **Variables**.
Для zontcontrol полный набор такой:

```bash
gh variable set CLOUD_ID --repo "$REPOSITORY" --body "$CLOUD_ID"
gh variable set FOLDER_ID --repo "$REPOSITORY" --body "$FOLDER_ID"
gh variable set SA_ID --repo "$REPOSITORY" --body "$DEPLOY_SA_ID"
gh variable set FUNCTION_ID --repo "$REPOSITORY" --body "$FUNCTION_ID"
gh variable set FUNCTION_SA_ID --repo "$REPOSITORY" --body "$FUNCTION_SA_ID"
gh variable set BUCKET --repo "$REPOSITORY" --body "$BUCKET"
gh variable set LOCKBOX_SECRET_ID --repo "$REPOSITORY" --body "$LOCKBOX_SECRET_ID"
gh variable set LOCKBOX_VERSION_ID --repo "$REPOSITORY" --body "$LOCKBOX_VERSION_ID"
gh variable set YC_OIDC_AUDIENCE --repo "$REPOSITORY" --body "$OIDC_AUDIENCE"
gh variable set ZONT_CONTACT --repo "$REPOSITORY" --body "$ZONT_CONTACT"
gh variable list --repo "$REPOSITORY" --json name --jq '.[].name'
```

У другого приложения вместо `ZONT_CONTACT` могут быть свои несекретные
параметры. GitHub не разрешает repository variables с префиксом `GITHUB_`.
Поэтому workflow передаёт `${{ vars.YC_OIDC_AUDIENCE }}` во внутренний env
`GITHUB_OIDC_AUDIENCE` helper.

Токены внешнего API не нужны ни в repository variables, ни в GitHub Secrets:
они приходят в функцию из Lockbox. `GITHUB_TOKEN` для GitHub API создаётся
самой платформой, облачный IAM-токен получается во время job.

## 10. Проведите первый автоматический деплой

Слейте проверенный PR в master. После успешного CI найдите deploy run:

```bash
gh run list --repo "$REPOSITORY" --branch master \
  --workflow deploy-prod.yml --limit 5
gh run watch YOUR_DEPLOY_RUN_ID --repo "$REPOSITORY" --exit-status
```

Для повторной публикации уже проверенного **текущего** master:

```bash
gh workflow run deploy-prod.yml --repo "$REPOSITORY" --ref master
```

Проверки gate применяются и к ручному запуску. Это не способ задеплоить старый
коммит или обойти упавший CI.

В проверенном helper последовательность следующая:

1. Проверить настройки и manifest, затем актуальный master/checks.
2. Получить GitHub ID token с настроенным audience.
3. Обменять его POST-запросом к `https://auth.yandex.cloud/oauth/token`:
   `grant_type=urn:ietf:params:oauth:grant-type:token-exchange`,
   `requested_token_type=urn:ietf:params:oauth:token-type:access_token`,
   `audience=DEPLOY_SA_ID`, `subject_token=<ID token>`,
   `subject_token_type=urn:ietf:params:oauth:token-type:id_token`.
4. Передать IAM-токен только дочернему yc через `YC_IAM_TOKEN`, удалить
   у дочернего процесса конфликтующий `YC_TOKEN`. `HOME` сохранить.
   Использовать явный временный config/profile с endpoint/cloud/folder,
   без токена в файле и без чтения пользовательских настроек.
5. Прочитать функцию и текущую версию. Только успешный пустой `version list`
   разрешает первую публикацию. Ошибка чтения не означает отсутствие версии.
6. Загрузить ZIP и создать версию с закреплённым Lockbox binding.
7. Прочитать `$latest` и проверить ID созданной версии, ACTIVE, SHA,
   SHA-256 архива, параметры, runtime SA, журнал и secret binding.

Для загрузки используется рабочий метод образца:

```bash
yc "${YC_SCOPE[@]}" storage s3api put-object \
  --bucket "$BUCKET" --key "$OBJECT_KEY" --body build/yandex-function.zip \
  --format json-rest
```

Это иллюстрация команды внутри автоматического helper, не ручной обход gate.
`OBJECT_KEY` в zontcontrol имеет вид
`functions/zontcontrol/<SHA>/<RUN_ID>-<ATTEMPT>.zip`.
В `--body` передаётся **обычный путь без `@`**. У `--action` таймера ниже другая
семантика — там `@file.json` обязателен для чтения JSON из файла.

Параметры создания версии включают `--function-id`, `--runtime`, `--entrypoint`,
`--memory`, `--execution-timeout`, `--service-account-id`, `--log-folder-id`,
`--package-bucket-name`, `--package-object-name`, `--package-sha256`,
`--description` с provenance и binding:

```text
--secret environment-variable=API_TOKEN,id=SECRET_ID,version-id=VERSION_ID,key=api_token
```

При обновлении helper сохраняет env, остальные secret bindings и поддерживаемые
настройки текущей версии. Если настройки неизвестны helper, он останавливается
до upload. Не заменяйте его короткой командой `version create`, которая
случайно сбросит имеющуюся конфигурацию.

`--format json` возвращает snake_case; `--format json-rest` — REST/camelCase.
Смешивание этих форматов ломает разбор `function_id`/`functionId`,
`service_account_id`/`serviceAccountId` и других настроек.

## 11. Проверьте опубликованную версию

Получите `$latest`, не раскрывая значения env:

```bash
yc "${YC_SCOPE[@]}" serverless function version get-by-tag \
  --function-id "$FUNCTION_ID" --tag '$latest' --format json \
  --jq '{id,function_id,status,runtime,entrypoint,resources,execution_timeout,concurrency,service_account_id,log_options,secrets,description,environment_keys:(.environment // {} | keys)}'
```

Проверьте ID функции и версии, ACTIVE, entrypoint, runtime, timeout, memory,
concurrency, runtime SA, журнал, secret ID/version/key/env и SHA из description.
Сохраните ссылку GitHub run, SHA, ID версии и результат проверки в отчёт.

ACTIVE доказывает публикацию, но не успешный импорт и запуск приложения.
До таймера проведите согласованный ручной вызов через консоль Functions →
Тестирование. Проверьте ответ, результат и завершение cleanup. В zontcontrol
это настоящий алгоритм, который может выполнить сброс; вызов требует осознанного
разрешения владельца. Сам деплой функцию не вызывает.

## 12. Подключите таймер после успешной публикации

Сначала убедитесь, что одноимённого таймера нет:

```bash
yc "${YC_SCOPE[@]}" serverless trigger v2 list --format json \
  --jq 'map({id,name,status})'
```

Подготовьте локальный `trigger-action.json` без секретов:

```bash
python3 - <<'PY'
import json
import os
from pathlib import Path

action = {
    'invoke_function': {
        'function_id': os.environ['FUNCTION_ID'],
        'function_tag': '$latest',
        'service_account_id': os.environ['INVOKER_SA_ID'],
    },
    'retry_policy': {'interval': '10s', 'retry_attempts': '1'},
}
Path('trigger-action.json').write_text(json.dumps(action) + '\n', encoding='utf-8')
PY
```

После отдельного разрешения на включение расписания создайте таймер:

```bash
yc "${YC_SCOPE[@]}" serverless trigger v2 create timer \
  --name "$TRIGGER_NAME" --cron-expression '0/15 * ? * * *' \
  --action @trigger-action.json --format json --jq '{id,name,status}'
```

Создание включает работу таймера: к этому моменту версия, секрет и права должны
быть готовы. Cron содержит шесть полей: минуты, часы, день месяца, месяц,
день недели, год; время UTC. Это не пятичастный Linux cron. Фактический старт
может быть позже отметки расписания: в наших наблюдениях примерно на минуту.

```bash
export TRIGGER_ID='YOUR_TRIGGER_ID'
yc "${YC_SCOPE[@]}" serverless trigger v2 get --id "$TRIGGER_ID" \
  --format json --jq '{id,name,status,source,action}'
```

Проверьте ACTIVE, `source.timer.cron_expression`, единственный элемент массива
`action`, ID функции, `$latest`, invoker и retry policy. У **v2** поля называются
`source` и `action`, а не `rule` и `actions`. Старый `serverless trigger list`
может возвращать пустой список, хотя v2-таймер существует.

При ошибке настроен один повтор вызова через 10 секунд, как у образца.
Повтор запускает весь handler заново. Это отдельный уровень от повторов
асинхронного выполнения самой Functions; отсутствие `--async-max-retries`
не означает отключения всех платформенных повторов. Прикладные эффекты
должны учитывать принятые проектом условия повторного запуска.

## 13. Настройте наблюдаемость и проведите приёмку

Пишите структурированные логи в stdout, настройте штатный Python logger один
раз на вызов без добавления повторяющихся handlers. Полезные поля:
UTC timestamp, level, request ID, версия/SHA, начало и конец запуска,
результат и длительность. Не выводите токены, HTTP-заголовки авторизации,
полный входной event или произвольные upstream-ошибки.

В консоли Functions проверьте назначенный журнал. Для CLI сначала получите
его ID и затем прочитайте записи нужного периода:

```bash
yc "${YC_SCOPE[@]}" logging group list --format json
yc "${YC_SCOPE[@]}" logging read --group-id YOUR_LOG_GROUP_ID \
  --since 1h --limit 100 --format json
```

Ответ вызова по таймеру не заменяет журнал: его нельзя просматривать так же,
как ответ ручного теста. Если Cloud Logging технически недоступен, используйте
заранее предусмотренный прикладной канал наблюдаемости. В zontcontrol handler
возвращает полный log ручному вызову и сохраняет те же строки в notes
устройства: текущий запуск и два предыдущих. Такое хранение — особенность
приложения, не функция GitHub или Yandex Cloud.

Дождитесь минимум двух последовательных запусков по расписанию. Запишите:

1. Время начала/конца, request ID, результат и длительность каждого once.
2. Примерный интервал между вызовами; возможные повторы и ошибки.
3. Завершение cleanup и отсутствие дублированных сообщений.
4. Сохранность и ротацию логов, если используется прикладная история.

Техническое завершение отделяйте от бизнесового результата: `unknown` не
равен `normal`, принятие команды API не доказывает физический результат.
Длительность между начальной и конечной записью лога не включает операции,
выполненные уже после конечной записи, например публикацию notes.

Для zontcontrol [реальная приёмка](../reports/2026-10-21-yandex-cloud-acceptance.md)
подтвердила интервалы около 15 минут, завершённые вызовы примерно за 0,76–1,07 с
и ротацию трёх полных логов. Это измерения конкретного приложения и сети,
а не норматив времени для других функций.

## 14. Повседневные операции

**Пауза и возобновление таймера:**

```bash
yc "${YC_SCOPE[@]}" serverless trigger v2 pause --id "$TRIGGER_ID"
yc "${YC_SCOPE[@]}" serverless trigger v2 resume --id "$TRIGGER_ID"
```

Пауза препятствует новым запускам таймера, но не отменяет уже выполняющийся
или принятый вызов функции. После изменения снова прочитайте `v2 get`.
Публикация версии не является разрешением включить ранее отключённый таймер.

**Ротация токена:** владелец добавляет версию в Lockbox; оператор проверяет
metadata и обновляет `LOCKBOX_VERSION_ID` в repository variables. Запустите
deploy текущего проверенного master, проверьте новый secret binding и работу
функции. Старую версию секрета отключайте после проверки переключения.

**Откат кода:** создайте ветку от актуального master, выполните `git revert`
проблемного коммита, назначьте следующий свободный номер версии и оформите PR.
После проверок и merge штатный workflow публикует исправленный текущий master.
Не переносите release tag и не пытайтесь передать старый SHA через dispatch:
gate это запрещает. При опасных эффектах сначала остановите расписание.

## 15. Ошибки, которые уже встречались

| Симптом | Что проверить и сделать |
| --- | --- |
| Gate отклоняет SHA или checks | Текущий master, event=push, head repository, обязательные checks и их workflow. Дождаться проверок точного SHA; не обходить gate |
| `missing or unexpected deployment settings` | Все repository variables, фиксированные настройки и project-specific проверки helper; передаётся ли `YC_OIDC_AUDIENCE` под нужным внутренним именем |
| OIDC exchange отклонён | Сравнить фактические `iss`/`aud`/`sub` с федерацией и credential; проверить deploy SA audience обмена, immutable IDs и влияние GitHub Environment |
| Запрос OIDC недоступен | `id-token: write` именно в deploy job, условия запуска и наличие служебных `ACTIONS_ID_TOKEN_REQUEST_*`; значения токенов не печатать |
| yc не знает endpoint или использует чужую авторизацию | Явные endpoint/cloud/folder/config/profile; IAM в `YC_IAM_TOKEN`, удалён конфликтующий `YC_TOKEN`, `HOME` не переопределён |
| `local_file_unavailable` при upload | Путь существует в рабочем каталоге процесса; для `put-object --body` нет `@`; файл ещё не удалён; дочерний yc наследует нужное окружение |
| Upload завершился без однозначного успеха | Остановить публикацию до `version create`; не выдавать неизвестный исход за успех и не повторять запись автоматически |
| Неясный исход `version create` | Читать `$latest`, сверять уникальный object key, SHA, digest и параметры; не создавать ещё одну версию вслепую |
| Не удаётся привязать runtime SA | Binding `iam.serviceAccounts.user` выдан deploy SA на правильном runtime SA |
| В функции нет токена | ACTIVE версия Lockbox, правильный key и env, закреплённый version-id, `lockbox.payloadViewer` у runtime SA; не читать payload в диагностический лог |
| ACTIVE есть, запуск падает | Корень ZIP, entrypoint, зависимости/runtime, конфиги и журнал; ACTIVE не является проверкой Python-кода |
| Новый секрет не используется | Опубликованная версия по-прежнему с прежним `version-id`; обновить variable и выполнить deploy |
| Функция переименована, deploy не находит её | Helper использует имя вместо ID; сверить текущую цель, перейти на проверяемый ID |
| Таймер не найден или config=null | Использовать `trigger v2`, смотреть `source`/`action`; сверить ID и каталог |
| После отметки cron сразу нет лога | Дать время доставке; затем проверить ACTIVE/action/invoker и логи. Отсутствие notes не различает отсутствие вызова и сбой сохранения |
| После запуска таймера не виден ответ | Читать Cloud Logging или прикладной журнал; response scheduled invocation не является доступным отчётом |

## Проверка готовности

- Опубликован точный проверенный SHA; версия ACTIVE и соответствует manifest.
- Deploy и runtime права разделены; GitHub получает только краткоживущий токен.
- ZIP и Git не содержат credentials; runtime читает свой pinned Lockbox binding.
- До включения расписания выполнен согласованный ручной тест и проверен cleanup.
- Таймер указывает нужную функцию/тег/invoker, cron и retry policy прочитаны обратно.
- Наблюдены минимум два плановых once; результаты, времена и границы измерений
  записаны в отчёт. Возможные unknown/ошибки не скрыты успешным статусом деплоя.

## Источники и рабочие реализации

- [Контракт деплоя zontcontrol](../agents/deployment.md),
  [реестр ресурсов с датой проверки](yandex-cloud-resources.md),
  [исторический справочник bootstrap](../history/2026-10-20-yandex-cloud-bootstrap-commands.md).
- [Helper autorepeater](https://github.com/gorpsys/autorepeater/blob/5bfeeeb865f6c3fdb381b27419309b41b1f5b26a/scripts/yandex_deploy.py),
  [workflow образца](https://github.com/gorpsys/autorepeater/blob/5bfeeeb865f6c3fdb381b27419309b41b1f5b26a/.github/workflows/deploy-prod.yml).
- [GitHub OIDC reference](https://docs.github.com/en/actions/reference/security/oidc),
  [discovery и JWKS](https://token.actions.githubusercontent.com/.well-known/openid-configuration).
- [YC Workload Identity](https://yandex.cloud/ru/docs/iam/concepts/workload-identity),
  [создание OIDC-федерации](https://yandex.cloud/ru/docs/cli/cli-ref/iam/cli-ref/workload-identity/oidc/federation/create),
  [federated credential](https://yandex.cloud/ru/docs/cli/cli-ref/iam/cli-ref/workload-identity/federated-credential/create).
- [Создание bucket](https://yandex.cloud/ru/docs/cli/cli-ref/storage/cli-ref/bucket/create),
  [загрузка объекта](https://yandex.cloud/ru/docs/cli/cli-ref/storage/cli-ref/s3api/put-object),
  [версия функции](https://yandex.cloud/ru/docs/cli/cli-ref/serverless/cli-ref/function/version/create).
- [Права вызова функции](https://yandex.cloud/ru/docs/cli/cli-ref/serverless/cli-ref/function/add-access-binding),
  [Lockbox](https://yandex.cloud/ru/docs/lockbox/),
  [чтение логов](https://yandex.cloud/ru/docs/cli/cli-ref/logging/cli-ref/read).
- [Таймер v2](https://yandex.cloud/ru/docs/cli/cli-ref/serverless/cli-ref/trigger/v2/create/timer),
  [cron](https://yandex.cloud/ru/docs/functions/concepts/trigger/timer).
