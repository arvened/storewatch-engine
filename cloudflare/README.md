# Закрытые ссылки на XML-фиды (Cloudflare Worker)

Фиды клиентов лежат в приватном `storewatch-data` (`feeds/<код>.xml`). Worker отдаёт их по ссылке
`https://<имя>.workers.dev/feeds/<код>.xml`. Код — 32 случайных символа на клиента (`clients/<slug>/feed-link.txt`).
Бесплатный тариф Cloudflare (100 000 запросов в день) покрывает с запасом.

## Настройка (один раз, ~10 минут, с телефона)

**1. GitHub — токен только на чтение** (github.com → фото профиля → Settings → Developer settings →
Personal access tokens → Fine-grained tokens → Generate new token)
- Token name: `eupriceindex-feeds-read`, Expiration: 1 год
- Repository access: **Only select repositories** → `storewatch-data`
- Permissions → Repository → **Contents: Read-only** (больше ничего)
- Generate → скопировать токен (показывается один раз)

**2. Cloudflare — Worker** (dash.cloudflare.com → Workers & Pages → Create → Create Worker)
- Name: `eupriceindex-feeds` → Deploy
- Edit code → удалить всё → вставить весь файл `cloudflare/feed-worker.js` → Deploy

**3. Cloudflare — секрет** (Worker → Settings → Variables and Secrets → Add)
- Type: **Secret**, Name: `GITHUB_TOKEN`, Value: токен из шага 1 → Deploy

**4. Прислать Claude адрес Worker** вида `https://eupriceindex-feeds.<ваш-поддомен>.workers.dev`.

Проверка: `https://…workers.dev/feeds/000000000000000000000000.xml` → «Not found» (сервис жив).

## Обслуживание
- Сменить ссылку клиенту: удалить `clients/<slug>/feed-link.txt` — на следующий день будет новый код (старый файл в `feeds/` удалить вручную).
- Токен истекает через год — выпустить новый и заменить секрет `GITHUB_TOKEN`.
