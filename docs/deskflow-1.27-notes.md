# Deskflow 1.27: что проверено вживую (2026-10-30, Windows 11, winget Deskflow.Deskflow 1.27.0)

Закрывает пометки «НП» из `monitors-spec.md` §5.3, §6.1, §6.2. Где написано иначе, верно то, что здесь.

## Запуск
- Установка: `C:\Program Files\Deskflow\` — `deskflow-core.exe`, `deskflow.exe` (GUI), `deskflow-daemon.exe`. MSI ставит службу `deskflow-daemon` (запущена, ядро сама не стартует, её не трогаем).
- CLI ядра: только `deskflow-core <server|client> [-s settings.conf] [--new-instance]`. Флагов `--name`, `--address`, `--config` больше нет. Всё задаётся INI-файлом настроек (Qt INI; пути писать с прямыми слешами `C:/...`).
- `--version` → `deskflow-core.exe v1.27.0, protocol v1.8`. `--help` одинаков для всех режимов (фикстуры в `tests/fixtures/`).
- `--new-instance` пропускает проверку «уже запущен».
- Лог идёт в stdout: `[2026-10-30T12:49:58.198] LEVEL: текст`, уровни `DEBUG INFO WARNING ERROR FATAL`. Код выхода 4 — не прочитан конфиг. Ядро до завершения работает бесконечно, остановка — terminate.

## Файл настроек (то, что мы генерируем)
```ini
[core]
coreMode=2                 ; 1 = клиент, 2 = сервер
computerName=laptop-a      ; имя экрана в связях
interface=192.168.0.101    ; сервер: адрес, к которому привязаться (лог: "binding to address: IP:PORT")
port=24800
[client]
remoteHost=192.168.0.101   ; только клиент; можно несколько через запятую
[security]
tlsEnabled=true
certificate=C:/…/tls/deskflow.pem   ; ключ + сертификат в одном PEM
checkPeerFingerprints=true
[server]
externalConfig=true
externalConfigFile=C:/…/links.conf
enableClipboard=true
[log]
level=DEBUG                ; строки подключения клиентов — уровня DEBUG
[computer_laptop-a]
name=laptop-a
[computer_laptop-b]
name=laptop-b
```
**Экраны объявляются только здесь** — секциями `[computer_<имя>]`. Секция `section: screens` во внешнем конфиге игнорируется с предупреждением, а без `[computer_…]` будет `read error: unknown computer name`.

## Внешний конфиг сервера (`externalConfigFile`)
Только `section: links` (и при необходимости `section: options` с `keystroke(...) = действие`). Дробные проценты с двумя знаками принимаются: `right(0,50.57) = laptop-b(0,1)` прочитан успешно (`decimals = 2`). В логе после загрузки: `active sides: R (0x04)`.

## TLS и отпечатки
- Нет файла сертификата → `ERROR: secure socket error: tls certificate doesn't exist: <путь>`; по умолчанию путь `C:/ProgramData/Deskflow/tls/deskflow.pem` (его создаёт только GUI). Мы задаём свой путь через `certificate=` и генерируем сертификат сами (RSA 2048, self-signed; годится `openssl req -x509 -newkey rsa:2048 -nodes -subj /CN=Deskflow`, ключ и сертификат склеить в один файл).
- Проверка взаимная: сервер сверяет клиента, клиент сверяет сервер.
- Отпечаток = SHA-256 от DER сертификата: `hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()` совпал с логом.
- Доверенные (Windows, настройки не из папки установки): `C:/ProgramData/Deskflow/tls/trusted-clients` (читает сервер) и `C:/ProgramData/Deskflow/tls/trusted-servers` (читает клиент). Путь **не** зависит от `-s`. Формат: по строке на отпечаток `v2:sha256:<hex в нижнем регистре>`. Файл перечитывается при каждом подключении — перезапуск не нужен.
- macOS/Linux: путь не проверен (по документации база — папка файла настроек: `~/Library/Deskflow/tls/…`, `~/.config/Deskflow/tls/…`).

## Строки лога (подтверждены)
| Строка | Смысл |
|---|---|
| `binding to address: 127.0.0.1:24899` | сервер слушает |
| `started server, waiting for clients` | сервер готов |
| `peer fingerprint: 65B3…C0C2` | отпечаток собеседника (64 hex, верхний регистр) |
| `fingerprint does not match trusted fingerprint` | отпечатка нет в trusted-* |
| `failed to verify server certificate fingerprint` | клиент не доверяет серверу |
| `read 1 fingerprint(s) from file: <путь>` / `fingerprint matches trusted fingerprint` | доверие есть |
| `accepted client connection`, `client "laptop-b" has connected` | клиент подключён (сервер) |
| `connected to secure socket`, `connected to server` | подключились (клиент) |
| `failed to connect to server: Timed out` + `retry in N seconds` | клиент повторяет сам |
| `scroll lock is on, locking cursor to computer` | Scroll Lock запирает курсор, пока в конфиге нет своего `lockCursorToScreen` |

Не проверено (нужен второй ноут): строка переключения (по синергии `switch from "a" to "b" at X,Y`), отключение клиента, межмашинный буфер обмена.
