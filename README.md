# VegaAI — Telegram-бот на GigaChat

Отдельный бот на Python, который принимает текстовые сообщения в Telegram и передаёт их GigaChat API. История хранится в памяти отдельно для каждого чата и сбрасывается после перезапуска или команды `/reset`.

## Веб-интерфейс Vega

Статический сайт находится в `site/`. Терминальная консоль на странице работает как локальная демонстрация: она не подключается к Telegram или GigaChat и не использует ключи. Для просмотра из корня проекта запустите:

```bash
python3 -m http.server 4173
```

Затем откройте `http://localhost:4173/site/`. Сторонние пакеты для сайта не нужны.

## Подключение

1. Создайте Telegram-бота через [@BotFather](https://t.me/BotFather) командой `/newbot` и скопируйте токен.
2. Получите ключ авторизации GigaChat API в кабинете разработчика GigaChat. В `.env` указывается Base64-ключ авторизации; если сервис выдал значение с префиксом `Basic `, вставьте его целиком.
3. В каталоге проекта создайте `.env` на основе `.env.example` и заполните `BOT_TOKEN`, `GIGACHAT_AUTH_KEY` и `ADMIN_IDS`. В `ADMIN_IDS` укажите свой числовой Telegram ID; несколько ID разделяются запятыми. Без настроенного ID команда `/admin` закрыта для всех. Не публикуйте `.env` и не отправляйте секреты в чат.
4. Установите зависимости и запустите бота:

   ```bash
   cd "VegaAI/python-bot"
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install -r requirements.txt
   python main.py
   ```

   В Windows активация окружения: `.venv\Scripts\activate`.
5. Откройте бота в Telegram и отправьте `/start`. Режим можно выбрать кнопкой или командой `/mode`: LLM отвечает в обычном формате, AGI — экспериментальный режим планирования и оценки рисков, а не автономный исполнитель. Переключатель действует отдельно для чата до перезапуска. Личный кабинет доступен командой `/account` и кнопками под приветствием. `/admin` доступен только ID из `ADMIN_IDS`. Запросы от одного пользователя ограничены интервалом в две секунды; при занятом единственном запросном слоте бот сообщает позицию в очереди и показывает статус «Думаю…».

Имя и описание профиля бот выставляет через Telegram API при запуске. Username и фотографию профиля нужно задать вручную в `@BotFather` командами `/setusername` и `/setuserpic`.

`GIGACHAT_SCOPE` по умолчанию равен `GIGACHAT_API_PERS`; укажите scope, соответствующий вашему типу доступа. Модель и системную инструкцию можно изменить через `GIGACHAT_MODEL` и `SYSTEM_PROMPT`.

Внутренний лимит токенов бота временно отключён. Он продолжает записывать фактический расход в `usage.json` и показывает его в `/admin` и ЛК. Ограничения тарифа и квоты аккаунта GigaChat по-прежнему применяются самим сервисом.

При проблеме с проверкой сертификата GigaChat установите доверенный сертификат НУЦ Минцифры в системное хранилище сертификатов. Проверка TLS в боте намеренно не отключается.

## Публикация в GitHub организации

1. Создайте в нужной GitHub-организации пустой **Private**-репозиторий, например `vegaai-python-bot`. Не добавляйте к нему README или лицензию: они уже находятся в проекте.
2. Проверьте `.env.example`: там должны быть только названия-подсказки, без настоящих ключей. Рабочие значения храните локально в `.env`; `.gitignore` исключает этот файл.
3. В терминале из каталога проекта выполните команды, подставив адрес репозитория своей организации:

   ```bash
   git init -b main
   git add .
   git status --short
   git commit -m "Initial VegaAI Python bot"
   git remote add origin https://github.com/ORGANIZATION/vegaai-python-bot.git
   git push -u origin main
   ```

   Перед `git commit` проверьте `git status --short`: файлов `.env`, `.venv`, `usage.json` и прочих секретов в списке быть не должно. Если секреты когда-либо попадали в Git, удаление файла из последнего коммита не делает их безопасными: сначала отзовите/замените ключи.

В организации настройте доступ для участников по принципу минимально необходимых прав. Секреты для будущего CI/CD добавляйте в GitHub Actions Secrets, а не в файлы репозитория.

## Развёртывание на VPS (Ubuntu/Debian)

Скопируйте каталог проекта на VPS в `/opt/gigachat-bot`, не копируя локальные `.venv` и `__pycache__`. Создайте отдельного системного пользователя и установите проект:

```bash
sudo useradd --system --create-home --home-dir /opt/gigachat-bot --shell /usr/sbin/nologin gigachat-bot
sudo chown -R gigachat-bot:gigachat-bot /opt/gigachat-bot
sudo -u gigachat-bot python3 -m venv /opt/gigachat-bot/.venv
sudo -u gigachat-bot /opt/gigachat-bot/.venv/bin/pip install -r /opt/gigachat-bot/requirements.txt
sudo -u gigachat-bot cp /opt/gigachat-bot/.env.example /opt/gigachat-bot/.env
sudo chmod 600 /opt/gigachat-bot/.env
```

Заполните `/opt/gigachat-bot/.env` токенами на сервере, затем установите и запустите службу:

```bash
sudo install -m 644 /opt/gigachat-bot/gigachat-bot.service /etc/systemd/system/gigachat-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now gigachat-bot
sudo systemctl status gigachat-bot
sudo journalctl -u gigachat-bot -f
```

Не передавайте токены в командной строке или сообщениях. При обновлении кода перезапустите службу: `sudo systemctl restart gigachat-bot`.