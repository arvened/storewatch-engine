# Закрытые ссылки на XML-фиды (Cloudflare Worker)

Фиды клиентов лежат в приватном `storewatch-data` (`feeds/<код>.xml`). Worker отдаёт их по ссылке
`https://<имя>.workers.dev/feeds/<код>.xml`. Код — 32 случайных символа на клиента (`clients/<slug>/feed-link.txt`).
Бесплатный тариф Cloudflare (100 000 запросов в день) покрывает с запасом.

## Настройка (один раз, ~10 минут, с телефона; код вставлять не нужно)

**1. Cloudflare — API-токен** (dash.cloudflare.com → значок профиля справа вверху → Profile → API Tokens → Create Token)
- Шаблон **Edit Cloudflare Workers** → Use template
- Account Resources: ваш аккаунт; Zone Resources: All zones → Continue to summary → Create Token → скопировать

**2. GitHub — токен только на чтение** (github.com → фото профиля → Settings → Developer settings →
Personal access tokens → Fine-grained tokens → Generate new token)
- Name: `eupriceindex-feeds-read`, Expiration: 1 год
- Repository access: **Only select repositories** → `storewatch-data`; Permissions → **Contents: Read-only**
- Generate → скопировать

**3. GitHub — два секрета** (github.com/arvened/storewatch-engine → Settings → Secrets and variables → Actions → New repository secret)
- `CLOUDFLARE_API_TOKEN` = токен из шага 1
- `FEEDS_READ_TOKEN` = токен из шага 2

**4. Запуск** (storewatch-engine → Actions → «Feeds worker (Cloudflare deploy)» → Run workflow) — или сказать Claude.

Проверка: `https://…workers.dev/feeds/000000000000000000000000.xml` → «Not found» (сервис жив).

## Обслуживание
- Сменить ссылку клиенту: удалить `clients/<slug>/feed-link.txt` — на следующий день будет новый код (старый файл в `feeds/` удалить вручную).
- Токен истекает через год — выпустить новый и заменить секрет `GITHUB_TOKEN`.
