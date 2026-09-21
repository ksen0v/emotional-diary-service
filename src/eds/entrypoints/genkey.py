"""Сгенерировать мастер-ключ шифрования.

Отдельная команда, а не автогенерация при старте: ключ, созданный заново при
каждом запуске, сделал бы уже сохранённые ключи источников нерасшифровываемыми.
Запуск: docker compose run --rm --no-deps api python -m eds.entrypoints.genkey
"""

from eds.platform.crypto import generate_master_key

if __name__ == "__main__":
    print(generate_master_key())
