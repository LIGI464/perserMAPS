# Парсер компаний на Ubuntu

Сервис запускает [interlark/parser-2gis](https://github.com/interlark/parser-2gis) и использует разборщик карточек из [artemsteshenko/parser_maps](https://github.com/artemsteshenko/parser_maps) с Chrome. Устанавливаются зафиксированные коммиты. Публичные карты открываются в обычных браузерных сессиях. Ключ `PARSER_API_KEY` используется для связи вашего сайта с этим сервисом.

## Установка на VPS

Сначала сохраните старый скрипт и сверьте резервную ветку. Для установки потребуется Ubuntu с systemd, свободный порт `8765` на loopback и доступ к GitHub, PyPI и Chrome. Исполняйте команды из каталога распакованного архива под `root`.

```bash
bash install-vps.sh
```

Сервис работает под пользователем `persermaps-github`. Файлы лежат в `/opt/persermaps-github`, ключ хранится в `/etc/persermaps-github.env`, а SQLite в `/var/lib/persermaps-github/called.sqlite3`.

## Проверка на VPS

```bash
curl -fsS http://127.0.0.1:8765/health
python3 /opt/persermaps-github/check-api.py 2gis Таганрог Стоматология 2
python3 /opt/persermaps-github/check-api.py yandex Таганрог Стоматология 2
python3 /opt/persermaps-github/check-api.py both Таганрог Стоматология 20
python3 /opt/persermaps-github/check-api.py both все все 10
```

Команда печатает число карточек с адресом, телефоном и соцсетями. `--show` выводит карточки. Список меток `issues` сообщает о недоступности карты, несовместимой разметке и превышении времени поиска. При поиске `все` сервис перебирает российские города и рубрики из каталога установленного `parser-2gis`. Издатель карты определяет контакты, доступные в каждой карточке.

## Подключение к сайту

Направьте `parser.example.com` на адрес VPS и добавьте отдельный HTTPS-хост в действующий прокси. Пример блока Caddy приведён ниже.

```caddyfile
parser.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

Передайте `PARSER_API_KEY` в секреты серверной части `example.com` и задайте `PARSER_API_BASE_URL=https://parser.example.com`. Описание контрактов и интерфейса находится в [промте для интеграции](PROMPT-INTEGRATION.md).
