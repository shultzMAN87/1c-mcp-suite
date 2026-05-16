Папка для статей ИТС (its.1c.ru)
================================

Как добавить статьи:
1. Откройте статью на its.1c.ru в браузере
2. Ctrl+P → Сохранить как PDF
3. Положите PDF-файл в эту папку
4. Перезапустите индексатор:
   curl -X DELETE http://localhost:6333/collections/its_articles
   docker compose restart its-indexer

Поддерживаемые форматы: .pdf, .txt, .md
