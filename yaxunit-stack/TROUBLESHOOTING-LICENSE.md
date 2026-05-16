# Troubleshooting лицензии 1С в server-mode

## TL;DR

Если в `04_yax.log` видишь `License not found on the 1C:Enterprise server`
или smoke server-mode падает на step 2/3/4 — **скорее всего, у тебя
КЛИЕНТСКАЯ community-лицензия вместо серверной**. Перевыпустить через
`license-helper` строго по процедуре из раздела "Перевыпуск лицензии".

Главное: **`ragent` должен быть запущен в `license-helper` ДО того, как
ты откроешь 1cestart**. Иначе 1С отдаст клиентскую лицензию.

## Корневой механизм

Платформа 1С определяет тип community-лицензии (клиентская vs серверная)
по тому, **присутствует ли ragent в окружении** в момент выписки:

- ragent НЕ запущен → выдаётся **клиентская** лицензия (работает только
  для DESIGNER/ENTERPRISE на файловой ИБ)
- ragent запущен → выдаётся **серверная** лицензия (работает и для
  rphost при подключении клиентов к серверной ИБ)

И это второе в нашем стенде — единственная рабочая лицензия для
`mode="server"`.

При этом **`.lic` файл выглядит одинаково** в обоих случаях. Тип
«зашит» внутри подписанной части. Поэтому отличить клиентскую от
серверной можно только по поведению: если шаги 1-3 пайплайна падают
с `License not found on the 1C:Enterprise server` — лицензия
клиентская, нужна перевыпуска.

## Структура файлов лицензии

```
hostfs:
  yaxunit-stack/licenses/license-backup.lic   ← bind-mount RW
  yaxunit-stack/license-backup.lic.GOLD       ← бэкап вне licenses/

container app  (usr1cv8):
  /var/1C/licenses/license-backup.lic         ← через bind RW
  /home/usr1cv8/.1cv8/1C/1cv8/conf/*.lic      ← копия от entrypoint
  /home/usr1cv8/.1cv8/1C/1cv8/1cv8conn.pfl    ← активационный контекст

container client (onec):
  /var/1C/licenses/license-backup.lic         ← через bind RW
  /home/onec/.1cv8/1C/1cv8/conf/*.lic         ← копия от entrypoint
  /home/onec/.1cv8/1C/1cv8/1cv8conn.pfl       ← активационный контекст
```

`.lic` и `.pfl` хранятся отдельно у каждого пользователя — это
нормально. `.lic` идентичен между контейнерами, `.pfl` каждый
контейнер генерирует свой при первой активации.

## Перевыпуск лицензии (правильная процедура)

### Подготовка

```powershell
# 1. Сохранить старую (на всякий случай)
if (Test-Path yaxunit-stack/licenses/license-backup.lic) {
    Move-Item yaxunit-stack/licenses/license-backup.lic `
              yaxunit-stack/licenses/license-backup.lic.OLD -Force
}

# 2. Остановить app и client (иначе конфликт hostname/MAC и портов
#    5900/6080 с license-helper)
docker compose stop app client
```

### Запуск license-helper с ragent в фоне

```powershell
# 3. Поднять license-helper
docker compose --profile license up -d --build license-helper
Start-Sleep 5

# 4. КРИТИЧНО: запустить ragent ДО открытия 1cestart
docker compose exec -d license-helper `
    /opt/1cv8/x86_64/current/ragent `
    -port 1540 -regport 1541 -range 1560:1591
Start-Sleep 3

# 5. Проверить, что ragent живой и слушает порты
docker compose exec license-helper bash -c `
    "ps -C ragent,rmngr -o pid,cmd; ss -tln | grep -E '1540|1541'"
# Должно показать процессы ragent + rmngr и LISTEN на :1540 и :1541
```

### Выписка через GUI (noVNC в браузере)

Открыть http://localhost:6080/ в браузере. Внутри VNC консоли:

1. **В xterm** (он уже открыт автоматически):
   ```
   /opt/1cv8/x86_64/current/1cestart &
   ```
2. В диалоге «Запуск 1С:Предприятия» нажать **"Добавить"** → **"Создание
   новой информационной базы"** → выбрать **"Создание ИБ без конфигурации"**
   → выбрать **"ФАЙЛОВАЯ"** → указать путь `/tmp/filebase` → выбрать
   **"Толстый клиент"** → нажать **"Готово"**.
3. **Открыть** созданную базу (двойным кликом или кнопкой "1С:Предприятие").
4. Появится диалог получения community-лицензии. **Ввести логин и пароль**
   от учётной записи developer.1c.ru.
5. После сообщения об успехе **закрыть** 1С (вместе с окном базы).

### Перенос на хост и финализация

```powershell
# 6. Проверить, что .lic упала в /var/1C/licenses
docker compose exec license-helper ls -la /var/1C/licenses/
# Должен появиться файл вида 20260504123045.lic — берём его имя

# 7. Скопировать на хост в нужное имя (license-backup.lic)
#    Подставь СВОЁ имя файла из шага 6:
docker compose --profile license cp `
    license-helper:/var/1C/licenses/20260504123045.lic `
    yaxunit-stack/licenses/license-backup.lic

# 8. Сразу сделать .GOLD-бэкап
Copy-Item yaxunit-stack/licenses/license-backup.lic `
          yaxunit-stack/license-backup.lic.GOLD -Force

# 9. Остановить helper, поднять боевые сервисы
docker compose --profile license stop license-helper
docker compose --profile testing up -d
Start-Sleep 30

# 10. Проверка health
curl.exe http://127.0.0.1:8019/health

# 11. Smoke в server-mode
$env:MCP_HOST = "127.0.0.1"
$env:MCP_SHARED_SECRET = (docker exec mcp-testing sh -c 'echo -n $MCP_SHARED_SECRET')
python scripts\smoke_yaxunit.py --mode server
```

Если smoke прошёл с `status=passed` — лицензия серверная, всё работает.

## Восстановление из .GOLD при порче

`.lic` иногда портится при определённых сценариях работы платформы
(например, если внутри контейнера запущено два процесса, спорящих за
ту же лицензию). Признак: размер файла отличается от исходного
(обычно становится больше — 7600+ байт вместо ~6400).

```powershell
# Сравнить размер с бэкапом
Get-Item yaxunit-stack/licenses/license-backup.lic | Format-List Name, Length
Get-Item yaxunit-stack/license-backup.lic.GOLD     | Format-List Name, Length

# Если размеры разные → восстановить из .GOLD
docker compose stop app client
Copy-Item yaxunit-stack/license-backup.lic.GOLD `
          yaxunit-stack/licenses/license-backup.lic -Force

# И на всякий случай очистить активационные контексты — пусть пере-активируется
docker volume rm 27_1c-mcp-suite-full-stack_srvinfo

# Поднять заново
docker compose --profile testing up -d --no-deps app client
Start-Sleep 30
curl.exe http://127.0.0.1:8019/health
```

## Проверка — серверная или клиентская лицензия?

Прямого способа из CLI нет, но можно проверить косвенно:

```powershell
# 1. Запустить smoke в server-mode
python scripts\smoke_yaxunit.py --mode server
```

Возможные результаты:

| Поведение | Диагноз |
|---|---|
| `passed` | Серверная лицензия, всё ок |
| `error` на step 1 (CREATEINFOBASE) с `License not found on the 1C:Enterprise server` | **Клиентская** лицензия — нужно перевыпустить |
| `error` на step 4 (LoadCfg YAxUnit) с `License not found` | См. секцию "Step 4 — особый случай" ниже |

## Step 4 — особый случай

DESIGNER при подключении расширения (`/LoadCfg ... -Extension YAxUnit`)
делает дополнительный round-trip к серверу через rphost. Если ragent
дотянулся до этого момента, но rphost не активировался — это всё та же
проблема серверной vs клиентской лицензии.

Если шаги 1, 2, 3 прошли (создание ИБ + загрузка main конфигурации +
обновление БД), но step 4 валится — это **очень похоже на гонку с
лицензированием rphost** при первом подключении к новой ИБ. Лечится:

```powershell
# Один раз вручную "разогреть" сервер: подключиться к любой ИБ через
# DESIGNER, чтобы rphost активировался. Дальше пайплайн будет проходить.

docker compose exec app /opt/1cv8/x86_64/current/rac `
    cluster list --port=1545 localhost
# Должен показать кластер. Это валидирует, что лицензия rphost рабочая.
```

После этого повторить smoke — если опять step 4, переходить к
перевыпуску лицензии.

## Что НЕ нужно делать (антипаттерны)

- ❌ **`chmod 444`** на `.lic` или mount как `:ro` — мешает платформе
  обновлять активационный контекст. В прошлой сессии мы это пробовали,
  получали побочные эффекты вплоть до невозможности первого запуска.
  Сейчас наш compose монтирует licenses RW, защита — через `.GOLD`-бэкап.
- ❌ **Получать лицензию через серверный диалог "Создать новую ИБ" →
  "Сервер 1С:Предприятия"**. На 8.3.24 диалог зависает в "Сборе
  информации о компьютере". Только через файловую базу.
- ❌ **Запускать 1cestart БЕЗ ragent в фоне**. Получишь клиентскую
  лицензию, server-mode не заработает.
- ❌ **Получать лицензию на хосте Windows и копировать в контейнер**.
  Лицензия привязана к fingerprint (hostname+MAC+dmidecode), у хоста
  и контейнера они разные.

## Дополнительные источники

- Архив рабочего стенда `26_yaxunit-mcp-stack` (от Кости) — реальный
  пример полностью работающей конфигурации, в т.ч. с правильно
  выписанной серверной лицензией.
- Историческая сессия отладки server-mode — много экспериментов по
  защите .lic от порчи (большая часть оказалась ненужной после
  правильной выписки лицензии).
